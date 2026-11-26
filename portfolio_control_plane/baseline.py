"""Read-only portfolio size and dependency baseline collection.

The collector intentionally measures only local checkouts.  It never contacts a
remote service, installs dependencies, runs CI, or changes a repository.  A
missing checkout or an unavailable measurement is represented by ``unknown``
instead of an inferred zero.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable, Mapping


UNKNOWN = "unknown"
SCHEMA_VERSION = "1.0"
DEFAULT_LARGE_FILE_THRESHOLD_BYTES = 1_000_000
GIT_QUERY_TIMEOUT_SECONDS = 15.0
ROLES = frozenset(
    {
        "core",
        "donor-legacy",
        "independent",
        "reference",
        "role-pending",
        "portfolio-evidence",
    }
)

# These directories are machine-local caches, source media, or nested working
# areas.  They are excluded from checkout_bytes and file discovery, but a
# tracked file in one of them is still included in source_bytes and reported as
# generated when its path makes that clear.
EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".worktrees",
        ".pytest_cache",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "raw",
        "wiki",
        ".obsidian",
        "organized_video_sources",
        "packages",
        "tmp",
        "temp",
    }
)
GENERATED_DIRECTORY_NAMES = frozenset(
    {
        "__pycache__",
        "node_modules",
        ".pytest_cache",
        "dist",
        "build",
        "coverage",
        ".venv",
        "venv",
        ".tmp",
    }
)
GENERATED_SUFFIXES = frozenset(
    {".pyc", ".pyo", ".class", ".map", ".coverage", ".lockb"}
)
DEPENDENCY_MANIFEST_NAMES = frozenset(
    {
        "package.json",
        "requirements.txt",
        "pyproject.toml",
        "pipfile",
        "cargo.toml",
        "go.mod",
        "composer.json",
        "gemfile",
    }
)
DEPENDENCY_LOCK_NAMES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pipfile.lock",
        "uv.lock",
        "cargo.lock",
        "go.sum",
        "composer.lock",
        "gemfile.lock",
    }
)
RELEASE_DIRECTORY_NAMES = frozenset({"dist", "build", "release", "artifacts", "releases"})
RELEASE_SUFFIXES = frozenset(
    {".zip", ".tar", ".gz", ".tgz", ".whl", ".ccx", ".crx", ".vsix", ".dmg", ".pkg"}
)


class BaselineError(ValueError):
    """Raised when a baseline manifest or measurement override is malformed."""


REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _run_git(repository: Path, *arguments: str) -> bytes | None:
    """Run a bounded, read-only Git query and discard stderr."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=GIT_QUERY_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _is_excluded_directory(name: str) -> bool:
    normalized = name.casefold()
    return normalized in EXCLUDED_DIRECTORY_NAMES or normalized.startswith(".tmp")


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _iter_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return
    for directory, directory_names, file_names in os.walk(
        root, topdown=True, followlinks=False
    ):
        directory_names[:] = sorted(
            name for name in directory_names if not _is_excluded_directory(name)
        )
        for name in sorted(file_names):
            path = Path(directory) / name
            if path.is_file() and _within(root, path):
                yield path


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _sum_sizes(paths: Iterable[Path]) -> tuple[int | None, list[Path]]:
    total = 0
    unreadable: list[Path] = []
    for path in paths:
        value = _size(path)
        if value is None:
            unreadable.append(path)
        else:
            total += value
    return (None if unreadable else total), unreadable


def _tracked_paths(repository: Path) -> list[Path] | None:
    raw = _run_git(repository, "ls-files", "-z")
    if raw is None:
        return None
    paths: list[Path] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        try:
            relative = item.decode("utf-8")
        except UnicodeDecodeError:
            return None
        path = repository / Path(relative)
        if _within(repository, path):
            paths.append(path)
    return paths


def _git_object_directory(repository: Path) -> Path | None:
    raw = _run_git(repository, "rev-parse", "--git-path", "objects")
    if raw is None:
        return None
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else repository / path


def _file_observations(paths: Iterable[Path], repository: Path) -> tuple[list[dict[str, Any]], bool]:
    observations: list[dict[str, Any]] = []
    complete = True
    for path in paths:
        size = _size(path)
        if size is None:
            complete = False
            continue
        observations.append({"path": _relative(path, repository), "bytes": size})
    observations.sort(key=lambda item: (-item["bytes"], item["path"]))
    return observations, complete


def _is_generated(path: Path) -> bool:
    parts = {part.casefold() for part in path.parts}
    if parts & GENERATED_DIRECTORY_NAMES:
        return True
    return path.suffix.casefold() in GENERATED_SUFFIXES or path.name.casefold().endswith(
        ".min.js"
    )


def _is_release_artifact(path: Path) -> bool:
    parts = {part.casefold() for part in path.parts}
    return bool(parts & RELEASE_DIRECTORY_NAMES) or path.suffix.casefold() in RELEASE_SUFFIXES


def _repo_relative_named(paths: Iterable[Path], repository: Path, names: frozenset[str]) -> list[str]:
    return sorted(
        _relative(path, repository)
        for path in paths
        if path.name.casefold() in names
    )


def _workflow_paths(repository: Path) -> list[str]:
    workflow_root = repository / ".github" / "workflows"
    if not workflow_root.is_dir():
        return []
    return sorted(
        _relative(path, repository)
        for path in workflow_root.iterdir()
        if path.is_file() and _within(repository, path)
    )


def _metric(value: Any, field: str) -> int | str:
    if value == UNKNOWN:
        return UNKNOWN
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BaselineError(f"{field} must be a non-negative integer or 'unknown'")
    return value


def _validate_manifest(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise BaselineError("portfolio manifest must use schema version 1.0")
    repositories = manifest.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise BaselineError("portfolio manifest.repositories must be a non-empty array")
    ids: set[str] = set()
    for index, entry in enumerate(repositories):
        if not isinstance(entry, Mapping):
            raise BaselineError(f"repositories[{index}] must be an object")
        for field in (
            "repo_id",
            "repository",
            "role",
            "stable_url",
            "canonical_status_url",
        ):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise BaselineError(f"repositories[{index}].{field} must be non-empty")
        repo_id = entry["repo_id"]
        if repo_id in ids:
            raise BaselineError(f"duplicate repository id: {repo_id}")
        ids.add(repo_id)
        if entry["role"] not in ROLES:
            raise BaselineError(f"repositories[{index}].role is invalid")
        if not REPOSITORY_RE.fullmatch(entry["repository"]):
            raise BaselineError(f"repositories[{index}].repository must be an owner/name string")
        evidence = entry.get("evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(item, str) and item.strip() for item in evidence):
            raise BaselineError(f"repositories[{index}].evidence must be a string array")
        local_path = entry.get("local_path")
        if local_path is not None and not isinstance(local_path, str):
            raise BaselineError(f"repositories[{index}].local_path must be a string or null")
    return repositories


def _overrides_for(
    measurements: Mapping[str, Any] | None, repo_id: str
) -> Mapping[str, Any]:
    if measurements is None:
        return {}
    if not isinstance(measurements, Mapping):
        raise BaselineError("measurements must be an object")
    value = measurements.get(repo_id, {})
    if not isinstance(value, Mapping):
        raise BaselineError(f"measurements[{repo_id}] must be an object")
    unexpected = sorted(str(field) for field in value if field not in {"fresh_install", "ci_wall_clock", "artifact_bytes"})
    if unexpected:
        raise BaselineError(f"measurements[{repo_id}] contains unsupported fields: {', '.join(unexpected)}")
    return value


def _unavailable_entry(
    entry: Mapping[str, Any], *, measured_at: str, reason: str, availability: str | None = None
) -> dict[str, Any]:
    return {
        "repo_id": entry["repo_id"],
        "repository": entry["repository"],
        "role": entry["role"],
        "canonical_owner": entry.get("canonical_owner"),
        "registered_agent": entry.get("registered_agent"),
        "stable_url": entry["stable_url"],
        "canonical_status_url": entry["canonical_status_url"],
        "canonical_status_source": entry.get("canonical_status_source"),
        "local_path": entry.get("local_path"),
        "availability": availability or entry.get("availability", "not_checked_out"),
        "source_bytes": UNKNOWN,
        "checkout_bytes": UNKNOWN,
        "git_object_bytes": UNKNOWN,
        "fresh_install": UNKNOWN,
        "ci_wall_clock": UNKNOWN,
        "artifact_bytes": UNKNOWN,
        "measured_at": measured_at,
        "tracked_large_files": [],
        "tracked_generated_files": [],
        "dependency_manifests": [],
        "dependency_locks": [],
        "ci_workflows": [],
        "release_artifacts": [],
        "evidence": sorted(set([*entry.get("evidence", []), reason])),
        "warnings": [reason],
    }


def audit_repository(
    entry: Mapping[str, Any],
    workspace_root: str | Path,
    *,
    measured_at: str | None = None,
    large_file_threshold_bytes: int = DEFAULT_LARGE_FILE_THRESHOLD_BYTES,
    measurements: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect one repository row without network or repository writes."""

    if (
        isinstance(large_file_threshold_bytes, bool)
        or not isinstance(large_file_threshold_bytes, int)
        or large_file_threshold_bytes < 1
    ):
        raise BaselineError("large_file_threshold_bytes must be a positive integer")
    root = Path(workspace_root).resolve()
    timestamp = measured_at or _now()
    local_path = entry.get("local_path")
    if not local_path:
        row = _unavailable_entry(
            entry,
            measured_at=timestamp,
            reason="local checkout is not in the registered baseline scope",
        )
        overrides = _overrides_for(measurements, str(entry["repo_id"]))
        for field in ("fresh_install", "ci_wall_clock", "artifact_bytes"):
            if field in overrides:
                row[field] = _metric(overrides[field], field)
        return row

    candidate = (root / local_path).resolve()
    if not _within(root, candidate):
        return _unavailable_entry(
            entry,
            measured_at=timestamp,
            reason="local_path escapes the workspace root",
            availability="unreadable",
        )
    if not candidate.exists():
        return _unavailable_entry(
            entry,
            measured_at=timestamp,
            reason="local checkout is absent",
            availability="not_checked_out",
        )
    if not candidate.is_dir() or not (candidate / ".git").exists():
        return _unavailable_entry(
            entry,
            measured_at=timestamp,
            reason="local path is not a Git repository",
            availability="not_a_git_repository",
        )

    warnings: list[str] = []
    all_files = list(_iter_files(candidate))
    checkout_bytes, unreadable_checkout = _sum_sizes(all_files)
    if unreadable_checkout:
        warnings.append("one or more checkout files could not be read")

    tracked = _tracked_paths(candidate)
    if tracked is None:
        source_bytes: int | str = UNKNOWN
        warnings.append("git tracked-file inventory is unavailable")
        tracked_large: list[dict[str, Any]] = []
        tracked_generated: list[dict[str, Any]] = []
    else:
        source_total, unreadable_tracked = _sum_sizes(tracked)
        source_bytes = source_total if source_total is not None else UNKNOWN
        if unreadable_tracked:
            warnings.append("one or more tracked files could not be read")
        tracked_observations, observations_complete = _file_observations(tracked, candidate)
        if not observations_complete:
            warnings.append("some tracked file observations are incomplete")
        tracked_large = [
            item for item in tracked_observations if item["bytes"] >= large_file_threshold_bytes
        ]
        tracked_generated = [
            item
            for path, item in (
                (candidate / item["path"], item) for item in tracked_observations
            )
            if _is_generated(path)
        ]

    object_directory = _git_object_directory(candidate)
    if object_directory is None or not object_directory.is_dir():
        git_object_bytes: int | str = UNKNOWN
        warnings.append("git object database is unavailable")
    else:
        object_files = [path for path in _iter_files(object_directory)]
        # _iter_files excludes a directory named .git, which is not present
        # below objects, and keeps the traversal bounded to the object DB.
        object_total, unreadable_objects = _sum_sizes(object_files)
        git_object_bytes = object_total if object_total is not None else UNKNOWN
        if unreadable_objects:
            warnings.append("one or more Git object files could not be read")

    dependency_manifests = _repo_relative_named(
        all_files,
        candidate,
        DEPENDENCY_MANIFEST_NAMES,
    )
    dependency_manifests.extend(
        sorted(
            _relative(path, candidate)
            for path in all_files
            if path.name.casefold().startswith("requirements")
            and path.suffix.casefold() in {".txt", ".in"}
        )
    )
    dependency_manifests = sorted(set(dependency_manifests))
    dependency_locks = _repo_relative_named(all_files, candidate, DEPENDENCY_LOCK_NAMES)
    workflows = _workflow_paths(candidate)
    release_paths = [path for path in all_files if _is_release_artifact(path)]
    release_observations, release_complete = _file_observations(release_paths, candidate)
    artifact_total, unreadable_artifacts = _sum_sizes(release_paths)
    if unreadable_artifacts or not release_complete:
        artifact_bytes: int | str = UNKNOWN
        warnings.append("release artifact measurement is incomplete")
    else:
        artifact_bytes = artifact_total if artifact_total is not None else UNKNOWN

    overrides = _overrides_for(measurements, str(entry["repo_id"]))
    fresh_install: int | str = UNKNOWN
    ci_wall_clock: int | str = UNKNOWN
    if "fresh_install" in overrides:
        fresh_install = _metric(overrides["fresh_install"], "fresh_install")
    if "ci_wall_clock" in overrides:
        ci_wall_clock = _metric(overrides["ci_wall_clock"], "ci_wall_clock")
    if "artifact_bytes" in overrides:
        artifact_bytes = _metric(overrides["artifact_bytes"], "artifact_bytes")

    return {
        "repo_id": entry["repo_id"],
        "repository": entry["repository"],
        "role": entry["role"],
        "canonical_owner": entry.get("canonical_owner"),
        "registered_agent": entry.get("registered_agent"),
        "stable_url": entry["stable_url"],
        "canonical_status_url": entry["canonical_status_url"],
        "canonical_status_source": entry.get("canonical_status_source"),
        "local_path": local_path,
        "availability": "checked_out",
        "source_bytes": source_bytes,
        "checkout_bytes": checkout_bytes if checkout_bytes is not None else UNKNOWN,
        "git_object_bytes": git_object_bytes,
        "fresh_install": fresh_install,
        "ci_wall_clock": ci_wall_clock,
        "artifact_bytes": artifact_bytes,
        "measured_at": timestamp,
        "tracked_large_files": tracked_large,
        "tracked_generated_files": tracked_generated,
        "dependency_manifests": dependency_manifests,
        "dependency_locks": dependency_locks,
        "ci_workflows": workflows,
        "release_artifacts": release_observations,
        "evidence": sorted(
            set(
                [
                    *entry.get("evidence", []),
                    "git ls-files",
                    "git rev-parse --git-path objects",
                    "local checkout traversal",
                ]
            )
        ),
        "warnings": sorted(set(warnings)),
    }


def audit_manifest(
    manifest: Mapping[str, Any],
    workspace_root: str | Path,
    *,
    measured_at: str | None = None,
    large_file_threshold_bytes: int = DEFAULT_LARGE_FILE_THRESHOLD_BYTES,
    measurements: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect a complete manifest report using a shared measurement policy."""

    repositories = _validate_manifest(manifest)
    if measurements is not None:
        if not isinstance(measurements, Mapping):
            raise BaselineError("measurements must be an object")
        known_ids = {str(entry["repo_id"]) for entry in repositories}
        unknown_ids = sorted(
            str(repo_id)
            for repo_id in measurements
            if not isinstance(repo_id, str) or repo_id not in known_ids
        )
        if unknown_ids:
            raise BaselineError("measurements contains unknown repositories: " + ", ".join(unknown_ids))
    timestamp = measured_at or _now()
    rows = [
        audit_repository(
            entry,
            workspace_root,
            measured_at=timestamp,
            large_file_threshold_bytes=large_file_threshold_bytes,
            measurements=measurements,
        )
        for entry in repositories
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": timestamp,
        "measurement_policy": {
            "network": "none",
            "writes": "report_output_only",
            "large_file_threshold_bytes": large_file_threshold_bytes,
            "excluded_directories": sorted(EXCLUDED_DIRECTORY_NAMES),
        },
        "repositories": rows,
    }


def load_json(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineError(f"cannot read JSON input: {path}") from exc


__all__ = [
    "BaselineError",
    "DEFAULT_LARGE_FILE_THRESHOLD_BYTES",
    "EXCLUDED_DIRECTORY_NAMES",
    "SCHEMA_VERSION",
    "UNKNOWN",
    "audit_manifest",
    "audit_repository",
    "load_json",
]
