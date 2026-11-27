"""Filesystem-aware path identity for project matching and collision checks.

Resolved paths are not reliable identities: case-insensitive filesystems (default
macOS and Windows volumes) and Unicode-normalization-insensitive ones (APFS) let two
different spellings name the same directory. Comparing strings would let a cwd
spelled ``.../heldproject`` escape a project registered as ``.../HeldProject``.

Identity therefore uses ``(st_dev, st_ino)`` whenever both paths exist and falls back
to a normalized spelling (``os.path.normcase`` + Unicode NFC, plus ``casefold`` on
macOS and Windows) only when a path does not exist (ENOENT/ENOTDIR). Other stat
errors raise, so callers fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
import unicodedata


_CASE_INSENSITIVE_PLATFORM = sys.platform == "darwin" or os.name == "nt"


def normalized_spelling(path: Path) -> str:
    text = unicodedata.normalize("NFC", os.path.normcase(str(path)))
    if _CASE_INSENSITIVE_PLATFORM:
        text = unicodedata.normalize("NFC", text.casefold())
    return text


def _file_id(path: Path) -> tuple[int, int] | None:
    """Return ``(st_dev, st_ino)``, or ``None`` only when the path does not exist.

    Only ENOENT/ENOTDIR mean "missing" and allow the spelling fallback. Any other
    failure (symlink loop, permission denied, I/O error) propagates so that project
    matching fails closed instead of guessing from the spelling.
    """

    try:
        info = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return (info.st_dev, info.st_ino)


@dataclass(frozen=True)
class PathIdentity:
    """Identity of one resolved absolute path."""

    path: Path
    file_id: tuple[int, int] | None
    spelling: str

    @classmethod
    def of(cls, path: Path) -> "PathIdentity":
        return cls(path=path, file_id=_file_id(path), spelling=normalized_spelling(path))

    def same_location(self, other: "PathIdentity") -> bool:
        """Same directory: by inode when both exist, else by normalized spelling."""

        if self.file_id is not None and other.file_id is not None:
            return self.file_id == other.file_id
        return self.spelling == other.spelling

    def collision_keys(self) -> set[tuple[object, ...]]:
        """Keys for duplicate detection. Any shared key is a collision (fail closed)."""

        keys: set[tuple[object, ...]] = {("spelling", self.spelling)}
        if self.file_id is not None:
            keys.add(("file", *self.file_id))
        return keys


def ancestor_identities(path: Path) -> list[tuple[int, PathIdentity]]:
    """Return ``(depth, identity)`` for ``path`` and each of its parents."""

    return [(len(item.parts), PathIdentity.of(item)) for item in (path, *path.parents)]


def contained_depth(root: PathIdentity, ancestors: list[tuple[int, PathIdentity]]) -> int | None:
    """Depth of the cwd ancestor that is the same directory as ``root``, if any."""

    for depth, ancestor in ancestors:
        if root.same_location(ancestor):
            return depth
    return None
