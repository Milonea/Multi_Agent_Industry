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
    resolved = path.resolve()
    if repo_root is not None:
        try:
            resolved.relative_to(Path(repo_root).resolve())
        except ValueError:
            pass
        else:
            raise error(f"{label} must be stored outside the repository")
    return resolved


def _check_owner_only(info: os.stat_result, *, kind: str, label: str, error: ErrorFactory) -> None:
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise error(f"{label} {kind} permissions must be owner-only")
    if info.st_uid != os.geteuid():
        raise error(f"{label} must be owned by the current user")


def _open_private_directory(
    directory: Path, *, label: str, error: ErrorFactory, check_mode: bool = True
) -> int | None:
    """Open the file's own directory without following a symlink; ``None`` if missing."""

    try:
        descriptor = os.open(directory, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EMLINK}:
            raise error(f"{label} directory must not be a symlink") from exc
        raise error(f"{label} directory could not be opened") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise error(f"{label} directory must not be a symlink")
        if check_mode:
            _check_owner_only(info, kind="directory", label=label, error=error)
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

    On POSIX the file's directory and the file itself are opened with ``O_NOFOLLOW``;
    the file is opened relative to the directory descriptor, both descriptors are
    checked with ``fstat`` (type, owner == euid, no group/other bits) and the content
    is read from the same file descriptor, so a path swapped after the checks is
    never read. Ancestors above the file's own directory may be symlinks (for
    example ``/var`` on macOS); ownership and mode of the final directory and file
    still apply. The repository check uses the fully resolved path. On Windows the
    ACLs are not checked.
    """

    destination = _absolute(path, label=label, error=error)
    resolved = outside_repository(destination, repo_root, label=label, error=error)
    if not _descriptor_checks_supported():
        return _read_without_descriptors(resolved, label=label, error=error)

    # The directory mode is checked only once the file exists, so a missing file is
    # still an empty configuration regardless of the directory's mode.
    directory_fd = _open_private_directory(destination.parent, label=label, error=error, check_mode=False)
    if directory_fd is None:
        return None
    try:
        try:
            file_fd = os.open(
                destination.name,
                os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise error(f"{label} file must not be a symlink") from exc
            raise error(f"{label} file could not be opened") from exc
        with os.fdopen(file_fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise error(f"{label} path must be a regular file")
            _check_owner_only(info, kind="file", label=label, error=error)
            directory_info = os.fstat(directory_fd)
            _check_owner_only(directory_info, kind="directory", label=label, error=error)
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
) -> Path:
    """Create an owner-only JSON file; never overwrite or follow an existing path."""

    destination = _absolute(path, label=label, error=error)
    resolved = outside_repository(destination, repo_root, label=label, error=error)
    if destination.exists() or destination.is_symlink():
        raise error(f"{label} file already exists")
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
    directory_fd = _open_private_directory(destination.parent, label=label, error=error)
    if directory_fd is None:
        raise error(f"{label} file could not be initialized")
    try:
        try:
            descriptor = os.open(destination.name, flags, 0o600, dir_fd=directory_fd)
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
