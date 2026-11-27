"""Shared safety checks for private, user-scoped JSON configuration files.

Private files hold user-specific data (persona details, project overlays). They must
live outside the repository, must not be symlinks and, on POSIX, must be owner-only
(file 0600, directory 0700) and owned by the effective user. Error messages never
echo file contents.
"""

from __future__ import annotations

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


def outside_repository(path: Path, repo_root: Path | None, *, label: str, error: ErrorFactory) -> Path:
    if path.is_symlink():
        raise error(f"{label} file must not be a symlink")
    resolved = path.resolve()
    if repo_root is not None:
        try:
            resolved.relative_to(Path(repo_root).resolve())
        except ValueError:
            pass
        else:
            raise error(f"{label} must be stored outside the repository")
    return resolved


def read_private_json(
    path: Path,
    *,
    repo_root: Path | None,
    label: str,
    error: ErrorFactory,
) -> Any | None:
    """Return parsed JSON, or ``None`` when the file does not exist.

    Raises ``error`` when the location or permissions are unsafe or the JSON cannot
    be decoded. The caller validates the structure.
    """

    destination = Path(path).expanduser()
    if not destination.is_absolute():
        raise error(f"{label} path must be absolute")
    destination = outside_repository(destination, repo_root, label=label, error=error)
    if destination.is_symlink():
        raise error(f"{label} file must not be a symlink")
    if not destination.exists():
        return None
    if not destination.is_file():
        raise error(f"{label} path must be a regular file")
    if os.name == "posix":
        file_info = destination.stat()
        directory_info = destination.parent.stat()
        if stat.S_IMODE(file_info.st_mode) & 0o077:
            raise error(f"{label} file permissions must be owner-only")
        if stat.S_IMODE(directory_info.st_mode) & 0o077:
            raise error(f"{label} directory permissions must be owner-only")
        if file_info.st_uid != os.geteuid() or directory_info.st_uid != os.geteuid():
            raise error(f"{label} must be owned by the current user")
    try:
        return json.loads(destination.read_text(encoding="utf-8"))
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
    """Create an owner-only JSON file; never overwrite an existing path."""

    destination = Path(path).expanduser()
    if not destination.is_absolute():
        raise error(f"{label} path must be absolute")
    destination = outside_repository(destination, repo_root, label=label, error=error)
    if destination.exists() or destination.is_symlink():
        raise error(f"{label} file already exists")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            directory_info = destination.parent.stat()
            if stat.S_IMODE(directory_info.st_mode) & 0o077:
                raise error(f"{label} directory permissions must be owner-only")
            if directory_info.st_uid != os.geteuid():
                raise error(f"{label} directory must be owned by the current user")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(destination, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except OSError as exc:
        raise error(f"{label} file could not be initialized") from exc
    return destination
