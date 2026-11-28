"""Shared safety checks for private, user-scoped JSON configuration files.

Private files hold user-specific data (persona details, project overlays). They must
live outside the repository, must not be symlinks and, on POSIX, must be owner-only
(file 0600, directory 0700) and owned by the effective user. Error messages never
echo file contents.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Callable

from .path_identity import is_within


ErrorFactory = Callable[[str], Exception]


def user_config_directory() -> Path:
    """Return the platform directory for private settings without creating it."""

    if os.name == "nt":
        app_data = os.environ.get("APPDATA")
        root = Path(app_data) if app_data else Path.home() / "AppData" / "Roaming"
        return root / "MultiAgentIndustry"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MultiAgentIndustry"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return root / "multi-agent-industry"


def configured_private_path(env_name: str, file_name: str, *, label: str, error: ErrorFactory) -> Path:
    """Return the env override (absolute only) or the platform default path."""

    configured = os.environ.get(env_name)
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise error(f"{label} override must be an absolute path")
        return path
    return user_config_directory() / file_name


_MAX_PRIVATE_FILE_BYTES = 2_000_000
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
# Linux O_PATH lets the walk traverse directories that are searchable but not readable.
_PATH = getattr(os, "O_PATH", 0)
# Captured once at import: whether the platform can open relative to a directory fd.
_DESCRIPTOR_CHECKS = (
    os.name == "posix"
    and bool(_NOFOLLOW and _DIRECTORY)
    and os.open in os.supports_dir_fd
    and hasattr(os, "geteuid")
)


def _descriptor_checks_supported() -> bool:
    return _DESCRIPTOR_CHECKS


def _absolute(path: Path, *, label: str, error: ErrorFactory) -> Path:
    destination = Path(path).expanduser()
    if not destination.is_absolute():
        raise error(f"{label} path must be absolute")
    if not destination.name or destination.name in {".", ".."}:
        raise error(f"{label} path must name a file")
    return destination


def outside_repository(path: Path, repo_root: Path | None, *, label: str, error: ErrorFactory) -> Path:
    """Reject a symlinked file and any fully resolved location inside the repository."""

    if path.is_symlink():
        raise error(f"{label} file must not be a symlink")
    if path.parent.is_symlink():
        raise error(f"{label} directory must not be a symlink")
    resolved = path.parent.resolve() / path.name
    if repo_root is not None:
        # Filesystem-aware containment: a differently spelled repository root (case,
        # Unicode normalization) is still the repository. Stat errors fail closed.
        try:
            inside = is_within(resolved, Path(repo_root).resolve())
        except (OSError, RuntimeError, ValueError) as exc:
            raise error(f"{label} path could not be checked") from exc
        if inside:
            raise error(f"{label} must be stored outside the repository")
    return resolved


def _check_owner_only(info: os.stat_result, *, kind: str, label: str, error: ErrorFactory) -> None:
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise error(f"{label} {kind} permissions must be owner-only")
    if info.st_uid != os.geteuid():
        raise error(f"{label} must be owned by the current user")


def _identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _stat_or_none(path: Path, *, follow: bool, label: str, error: ErrorFactory) -> os.stat_result | None:
    try:
        return os.stat(path) if follow else os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise error(f"{label} path could not be checked") from exc


def _open_resolved_directory(directory: Path, *, label: str, error: ErrorFactory) -> int | None:
    """Open an already resolved directory by walking it from ``/`` with ``O_NOFOLLOW``.

    Every component is opened relative to its parent descriptor without following
    symlinks, so the opened directory is exactly the path that passed the repository
    check even if an alias spelling or an ancestor symlink changes concurrently.
    Returns ``None`` when a component does not exist.
    """

    walk_flags = (_PATH or os.O_RDONLY) | _DIRECTORY | _CLOEXEC
    try:
        descriptor = os.open(directory.anchor or "/", walk_flags)
    except OSError as exc:
        raise error(f"{label} directory could not be opened") from exc
    try:
        for part in directory.parts[1:]:
            try:
                child = os.open(part, walk_flags | _NOFOLLOW, dir_fd=descriptor)
            except (FileNotFoundError,):
                os.close(descriptor)
                return None
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EMLINK}:
                    raise error(f"{label} directory changed or is a symlink") from exc
                raise error(f"{label} directory could not be opened") from exc
            os.close(descriptor)
            descriptor = child
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise error(f"{label} directory changed or is a symlink")
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    return descriptor


def _bound_directory(
    resolved_parent: Path, expected: os.stat_result, *, label: str, error: ErrorFactory
) -> int | None:
    """Open ``resolved_parent`` and require it to be the directory stat-ed during the check."""

    descriptor = _open_resolved_directory(resolved_parent, label=label, error=error)
    if descriptor is None:
        raise error(f"{label} directory changed during the check")
    try:
        if _identity(os.fstat(descriptor)) != _identity(expected):
            raise error(f"{label} directory changed during the check")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def read_private_json(
    path: Path,
    *,
    repo_root: Path | None,
    label: str,
    error: ErrorFactory,
) -> Any | None:
    """Return parsed JSON, or ``None`` when the file (or its directory) does not exist.

    The path is resolved once and the repository check runs on the real path. On
    POSIX the real parent directory is then opened by walking it component by
    component with ``O_NOFOLLOW``; its ``fstat`` identity must equal the identity
    recorded during the check, and the file is opened relative to that descriptor
    with ``O_NOFOLLOW`` and must match the file identity recorded during the check.
    Type, owner (euid) and mode come from ``fstat`` of the opened descriptors and the
    content is read from the same file descriptor, so neither a swapped file nor a
    re-pointed ancestor symlink can redirect the read. The file and its direct parent
    must not be symlinks; higher ancestors may be (for example ``/var`` on macOS).
    Any mismatch fails closed. On Windows the ACLs are not checked.
    """

    destination = _absolute(path, label=label, error=error)
    resolved = outside_repository(destination, repo_root, label=label, error=error)
    if not _descriptor_checks_supported():
        return _read_without_descriptors(resolved, label=label, error=error)

    parent_info = _stat_or_none(resolved.parent, follow=True, label=label, error=error)
    if parent_info is None:
        return None
    file_info_at_check = _stat_or_none(resolved, follow=False, label=label, error=error)
    if file_info_at_check is None:
        return None
    if stat.S_ISLNK(file_info_at_check.st_mode):
        raise error(f"{label} file must not be a symlink")

    directory_fd = _bound_directory(resolved.parent, parent_info, label=label, error=error)
    assert directory_fd is not None
    try:
        try:
            file_fd = os.open(
                resolved.name,
                os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
                dir_fd=directory_fd,
            )
        except FileNotFoundError as exc:
            raise error(f"{label} file changed during the check") from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise error(f"{label} file must not be a symlink") from exc
            raise error(f"{label} file could not be opened") from exc
        with os.fdopen(file_fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if _identity(info) != _identity(file_info_at_check):
                raise error(f"{label} file changed during the check")
            if not stat.S_ISREG(info.st_mode):
                raise error(f"{label} path must be a regular file")
            _check_owner_only(info, kind="file", label=label, error=error)
            # The directory mode is checked only once the file exists, so a missing
            # file is still an empty configuration regardless of the directory mode.
            _check_owner_only(os.fstat(directory_fd), kind="directory", label=label, error=error)
            payload = handle.read(_MAX_PRIVATE_FILE_BYTES + 1)
    finally:
        os.close(directory_fd)
    if len(payload) > _MAX_PRIVATE_FILE_BYTES:
        raise error(f"{label} file exceeds the supported size")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise error(f"{label} could not be read as JSON") from exc


def _read_without_descriptors(path: Path, *, label: str, error: ErrorFactory) -> Any | None:
    """Fallback for platforms without ``O_NOFOLLOW``/``dir_fd`` (Windows): no ACL checks."""

    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise error(f"{label} path must be a regular file")
    try:
        payload = path.read_bytes()
        if len(payload) > _MAX_PRIVATE_FILE_BYTES:
            raise error(f"{label} file exceeds the supported size")
        return json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise error(f"{label} could not be read as JSON") from exc


def create_private_json(
    path: Path,
    payload: Any,
    *,
    repo_root: Path | None,
    label: str,
    error: ErrorFactory,
    _text: str | None = None,
) -> Path:
    """Create an owner-only JSON file; never overwrite or follow an existing path.

    The directory is created at the resolved real path that passed the repository
    check, then opened and identity-checked exactly like ``read_private_json``.
    """

    destination = _absolute(path, label=label, error=error)
    resolved = outside_repository(destination, repo_root, label=label, error=error)
    if destination.exists() or destination.is_symlink() or os.path.lexists(resolved):
        raise error(f"{label} file already exists")
    content = _text if _text is not None else json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise error(f"{label} file could not be initialized") from exc
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC
    if not _descriptor_checks_supported():
        try:
            descriptor = os.open(resolved, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
        except FileExistsError as exc:
            raise error(f"{label} file already exists") from exc
        except OSError as exc:
            raise error(f"{label} file could not be initialized") from exc
        return resolved
    parent_info = _stat_or_none(resolved.parent, follow=False, label=label, error=error)
    if parent_info is None or not stat.S_ISDIR(parent_info.st_mode):
        raise error(f"{label} directory changed or is a symlink")
    directory_fd = _bound_directory(resolved.parent, parent_info, label=label, error=error)
    assert directory_fd is not None
    try:
        _check_owner_only(os.fstat(directory_fd), kind="directory", label=label, error=error)
        try:
            descriptor = os.open(resolved.name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError as exc:
            raise error(f"{label} file already exists") from exc
        except OSError as exc:
            raise error(f"{label} file could not be initialized") from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
    finally:
        os.close(directory_fd)
    return resolved


def create_private_text(path: Path, text: str, *, repo_root: Path | None,
                        label: str, error: ErrorFactory) -> Path:
    """Create text using the same descriptor-bound private file protections."""
    return create_private_json(path, None, repo_root=repo_root, label=label, error=error, _text=text)
