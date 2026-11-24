"""Read-only repository reality scanner with explicit partial/unknown states."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from ipaddress import ip_address
import math
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit


class ReadOnlyRepositoryClient(Protocol):
    """The scanner only depends on these GET-like methods."""

    def get_repository(self, repository: str) -> Mapping[str, Any]: ...

    def list_issues(self, repository: str) -> list[Mapping[str, Any]]: ...

    def list_pull_requests(self, repository: str) -> list[Mapping[str, Any]]: ...

    def list_workflow_runs(self, repository: str, head_sha: str) -> list[Mapping[str, Any]]: ...


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _head_sha(repository: Mapping[str, Any]) -> str | None:
    candidates = (
        repository.get("head_sha"),
        repository.get("default_branch_sha"),
        (repository.get("commit") or {}).get("sha") if isinstance(repository.get("commit"), Mapping) else None,
    )
    for value in candidates:
        if isinstance(value, str) and len(value) == 40 and all(char in "0123456789abcdef" for char in value):
            return value
    return None


@dataclass(frozen=True)
class RealitySnapshot:
    repository: str
    status: str
    default_branch: str | None
    head_sha: str | None
    collected_at: str
    source_urls: tuple[str, ...] = field(default_factory=tuple)
    open_issue_numbers: tuple[int, ...] = field(default_factory=tuple)
    open_pr_numbers: tuple[int, ...] = field(default_factory=tuple)
    workflow_status: str = "unknown"
    blockers: tuple[str, ...] = field(default_factory=tuple)
    manual_gates: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def freshness(self) -> str:
        if self.status == "complete" and self.head_sha:
            return "fresh"
        if self.status == "partial":
            return "partial"
        return "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "status": self.status,
            "default_branch": self.default_branch,
            "head_sha": self.head_sha,
            "collected_at": self.collected_at,
            "freshness": self.freshness,
            "source_urls": list(self.source_urls),
            "open_issue_numbers": list(self.open_issue_numbers),
            "open_pr_numbers": list(self.open_pr_numbers),
            "workflow_status": self.workflow_status,
            "blockers": list(self.blockers),
            "manual_gates": list(self.manual_gates),
            "errors": list(self.errors),
        }


def _mapping_items(value: Any) -> tuple[list[Mapping[str, Any]], bool]:
    """Keep valid API records while classifying malformed collection payloads."""

    if not isinstance(value, list):
        return [], True
    items: list[Mapping[str, Any]] = []
    malformed = False
    for item in value:
        if isinstance(item, Mapping):
            items.append(item)
        else:
            malformed = True
    return items, malformed


def _safe_source_url(value: Any) -> str | None:
    """Retain public HTTP(S) origins without query, fragment, or userinfo data."""

    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        hostname = parsed.hostname
        if not hostname:
            return None
        normalized_hostname = hostname.casefold().rstrip(".")
        if normalized_hostname in {"localhost", "local"} or normalized_hostname.endswith(
            (".localhost", ".local", ".internal", ".home.arpa")
        ):
            return None
        try:
            address = ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            return None
    except ValueError:
        return None
    path = parsed.path or "/"
    return f"{parsed.scheme.lower()}://{parsed.netloc}{path}"


def _source_urls(*items: Mapping[str, Any]) -> tuple[str, ...]:
    urls = {
        url
        for item in items
        for key in ("html_url", "url")
        for url in (_safe_source_url(item.get(key)),)
        if url is not None
    }
    return tuple(sorted(urls))


def _numbers(items: list[Mapping[str, Any]]) -> tuple[int, ...]:
    return tuple(sorted(item["number"] for item in items if type(item.get("number")) is int and item["number"] > 0))


def _markers(items: list[Mapping[str, Any]], marker: str) -> tuple[str, ...]:
    result: list[str] = []
    for item in items:
        labels = item.get("labels")
        label_names: set[str] = set()
        if isinstance(labels, list):
            for label in labels:
                if isinstance(label, str):
                    label_names.add(label)
                elif isinstance(label, Mapping) and isinstance(label.get("name"), str):
                    label_names.add(label["name"])
        title = item.get("title") if isinstance(item.get("title"), str) else ""
        if marker in label_names or marker.lower() in title.lower():
            number = item.get("number")
            if type(number) is int and number > 0:
                result.append(f"issue:{number}")
    return tuple(sorted(set(result)))


class RealityScanner:
    """Collect a bounded read-only snapshot from an injected client."""

    def __init__(
        self,
        client: ReadOnlyRepositoryClient,
        *,
        allowlist: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(allowlist, tuple) or not all(
            isinstance(item, str) and item.strip() for item in allowlist
        ):
            raise ValueError("allowlist must be an explicit tuple of repository names")
        if len(set(allowlist)) != len(allowlist):
            raise ValueError("allowlist must not contain duplicates")
        self.client = client
        self.allowlist = tuple(allowlist)

    def scan(self, repository: str) -> RealitySnapshot:
        collected_at = _now()
        if repository not in self.allowlist:
            return RealitySnapshot(
                repository=repository,
                status="unknown",
                default_branch=None,
                head_sha=None,
                collected_at=collected_at,
                errors=("repository is not in the explicit scanner allowlist",),
            )
        try:
            metadata = self.client.get_repository(repository)
            if not isinstance(metadata, Mapping):
                raise TypeError("repository metadata must be an object")
            default_branch = metadata.get("default_branch") if isinstance(metadata.get("default_branch"), str) else None
            head_sha = _head_sha(metadata)
            if not default_branch or not head_sha:
                return RealitySnapshot(
                    repository=repository,
                    status="partial",
                    default_branch=default_branch,
                    head_sha=head_sha,
                    collected_at=collected_at,
                    source_urls=_source_urls(metadata),
                    errors=("exact default-branch HEAD SHA is unavailable",),
                )
        except Exception as exc:
            return RealitySnapshot(
                repository=repository,
                status="unknown",
                default_branch=None,
                head_sha=None,
                collected_at=collected_at,
                errors=(f"repository metadata unavailable: {type(exc).__name__}",),
            )

        errors: list[str] = []
        source_urls = list(_source_urls(metadata))
        try:
            issues, malformed = _mapping_items(self.client.list_issues(repository))
            if malformed:
                errors.append("issues response malformed")
        except Exception as exc:
            issues = []
            errors.append(f"issues unavailable: {type(exc).__name__}")
        try:
            pull_requests, malformed = _mapping_items(self.client.list_pull_requests(repository))
            if malformed:
                errors.append("pull requests response malformed")
        except Exception as exc:
            pull_requests = []
            errors.append(f"pull requests unavailable: {type(exc).__name__}")
        try:
            workflows, malformed = _mapping_items(self.client.list_workflow_runs(repository, head_sha))
            if malformed:
                errors.append("workflow response malformed")
            workflow_status = "pass" if workflows and all(item.get("conclusion") == "success" for item in workflows) else "unknown"
        except Exception as exc:
            workflows = []
            workflow_status = "unknown"
            errors.append(f"workflow status unavailable: {type(exc).__name__}")

        status = "complete" if not errors else "partial"
        source_urls.extend(_source_urls(*issues, *pull_requests, *workflows))
        return RealitySnapshot(
            repository=repository,
            status=status,
            default_branch=default_branch,
            head_sha=head_sha,
            collected_at=collected_at,
            source_urls=tuple(sorted(set(source_urls))),
            open_issue_numbers=_numbers(issues),
            open_pr_numbers=_numbers(pull_requests),
            workflow_status=workflow_status,
            blockers=_markers(issues + pull_requests, "blocked"),
            manual_gates=_markers(issues + pull_requests, "needs-manual-validation"),
            errors=tuple(errors),
        )

    def scan_all(self) -> tuple[RealitySnapshot, ...]:
        return tuple(self.scan(repository) for repository in self.allowlist)


def compare_snapshot(previous: Mapping[str, Any], current: RealitySnapshot) -> dict[str, Any]:
    """Compare safe metadata and explicitly classify freshness changes."""

    previous_sha = previous.get("head_sha")
    current_dict = current.to_dict()
    if current.status != "complete":
        classification = "stale_recommendation"
    elif previous_sha == current.head_sha:
        classification = "still_valid"
    elif previous_sha and current.head_sha:
        classification = "changed"
    else:
        classification = "new_blocker"
    return {
        "repository": current.repository,
        "classification": classification,
        "previous_head_sha": previous_sha,
        "current": current_dict,
    }


class LocalRealityError(RuntimeError):
    """Raised internally when a read-only local Git query is unavailable."""


@dataclass(frozen=True)
class LocalRepositorySnapshot:
    """Runtime Git truth plus a separately classified status-document observation."""

    repository: str
    path: str
    status: str
    branch: str | None
    head_sha: str | None
    dirty: bool | None
    upstream: str | None
    ahead: int | None
    behind: int | None
    detached: bool | None
    worktree: bool | None
    worktree_path: str | None
    worktree_paths: tuple[str, ...]
    status_document: str | None
    document_observed_at: str | None
    document_declared_branch: str | None
    document_declared_head: str | None
    head_commit_at: str | None
    collected_at: str
    errors: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "path": self.path,
            "status": self.status,
            "branch": self.branch,
            "head_sha": self.head_sha,
            "dirty": self.dirty,
            "upstream": self.upstream,
            "ahead": self.ahead,
            "behind": self.behind,
            "detached": self.detached,
            "worktree": self.worktree,
            "worktree_path": self.worktree_path,
            "worktree_paths": list(self.worktree_paths),
            "status_document": self.status_document,
            "document_observed_at": self.document_observed_at,
            "document_declared_branch": self.document_declared_branch,
            "document_declared_head": self.document_declared_head,
            "head_commit_at": self.head_commit_at,
            "collected_at": self.collected_at,
            "errors": list(self.errors),
        }


GitRunner = Callable[[Path, tuple[str, ...]], str]


_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_DATE_RE = re.compile(r"20\d{2}-\d{2}-\d{2}(?:[T ][0-9:]+(?:Z|[+-][0-9:]+)?)?")


def _find_sha(value: str) -> str | None:
    for match in _SHA_RE.finditer(value):
        candidate = match.group(0)
        # Date-only headings such as 20260903 are not Git object IDs.
        if len(candidate) <= 8 and candidate.isdigit():
            continue
        return candidate
    return None


def _parse_document_metadata(path: Path) -> tuple[str | None, str | None, str | None]:
    """Read only the document lead so historical entries do not become current truth."""

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[:40]
    except OSError:
        return None, None, None
    lead = "\n".join(lines)
    date_match = _DATE_RE.search(lead)
    observed_at = date_match.group(0) if date_match else None
    branch = None
    branch_patterns = (
        r"(?:local checkout|branch at reconciliation start|current branch)\s*:\s*`([^`]+)`",
        r"Historical feature-candidate anchor:\s*branch\s*`([^`]+)`",
    )
    for pattern in branch_patterns:
        match = re.search(pattern, lead, flags=re.IGNORECASE)
        if match:
            branch = match.group(1).strip()
            break
    declared_head = None
    for index, line in enumerate(lines):
        if not re.search(r"\bHEAD\b|local checkout", line, flags=re.IGNORECASE):
            continue
        candidate = _find_sha(line)
        if candidate:
            declared_head = candidate
            break
        if index + 1 < len(lines):
            candidate = _find_sha(lines[index + 1])
            if candidate:
                declared_head = candidate
                break
    return observed_at, branch, declared_head


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)


class LocalGitRealityScanner:
    """Collect local Git truth without changing files, refs, or worktrees."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        freshness_days: int = 14,
        git_runner: GitRunner | None = None,
        now: Callable[[], datetime] | None = None,
        git_timeout_seconds: float = 15.0,
    ) -> None:
        if type(freshness_days) is not int or freshness_days < 0:
            raise ValueError("freshness_days must be non-negative")
        try:
            valid_timeout = (
                not isinstance(git_timeout_seconds, bool)
                and isinstance(git_timeout_seconds, (int, float))
                and math.isfinite(git_timeout_seconds)
                and git_timeout_seconds > 0
            )
        except (OverflowError, TypeError, ValueError):
            valid_timeout = False
        if not valid_timeout:
            raise ValueError("git_timeout_seconds must be positive")
        self.workspace_root = Path(workspace_root).resolve()
        self.freshness_days = freshness_days
        self.git_runner = git_runner or self._run_git
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.git_timeout_seconds = git_timeout_seconds

    def _current_time(self) -> datetime:
        current = self.now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)

    def _collected_at(self) -> str:
        return self._current_time().replace(microsecond=0).isoformat()

    def _run_git(self, repository: Path, args: tuple[str, ...]) -> str:
        try:
            process = subprocess.run(
                ["git", "-C", str(repository), *args],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.git_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise LocalRealityError("git query timed out") from exc
        if process.returncode != 0:
            raise LocalRealityError(f"git query failed: {args[0]}")
        return process.stdout.strip()

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.workspace_root)) or "."
        except ValueError:
            return f"external-worktree:{path.resolve().name or 'root'}"

    def _unavailable(
        self,
        repository: str,
        path: Path,
        reason: str,
        *,
        collected_at: str,
    ) -> LocalRepositorySnapshot:
        return LocalRepositorySnapshot(
            repository=repository,
            path=self._display_path(path),
            status="unavailable",
            branch=None,
            head_sha=None,
            dirty=None,
            upstream=None,
            ahead=None,
            behind=None,
            detached=None,
            worktree=None,
            worktree_path=None,
            worktree_paths=tuple(),
            status_document=None,
            document_observed_at=None,
            document_declared_branch=None,
            document_declared_head=None,
            head_commit_at=None,
            collected_at=collected_at,
            errors=(reason,),
        )

    def scan(
        self,
        repository: str,
        path: str | Path,
        *,
        status_document: str | Path | None = "docs/project/status.md",
    ) -> LocalRepositorySnapshot:
        collected_at = self._collected_at()
        repository_path = Path(path)
        if not repository_path.is_absolute():
            repository_path = self.workspace_root / repository_path
        repository_path = repository_path.resolve()
        try:
            repository_path.relative_to(self.workspace_root)
        except ValueError:
            return self._unavailable(
                repository,
                repository_path,
                "REPOSITORY_OUTSIDE_WORKSPACE",
                collected_at=collected_at,
            )
        if not repository_path.is_dir():
            return self._unavailable(
                repository,
                repository_path,
                "REPOSITORY_PATH_MISSING",
                collected_at=collected_at,
            )

        try:
            porcelain = self.git_runner(repository_path, ("status", "--porcelain=v2", "--branch"))
            head_sha = self.git_runner(repository_path, ("rev-parse", "HEAD")) or None
        except (OSError, LocalRealityError, subprocess.TimeoutExpired):
            return self._unavailable(
                repository,
                repository_path,
                "GIT_STATUS_UNAVAILABLE",
                collected_at=collected_at,
            )

        branch = None
        upstream = None
        ahead = None
        behind = None
        dirty = False
        for line in porcelain.splitlines():
            if line.startswith("# branch.head "):
                value = line.removeprefix("# branch.head ").strip()
                branch = None if value == "(detached)" else value
            elif line.startswith("# branch.upstream "):
                upstream = line.removeprefix("# branch.upstream ").strip() or None
            elif line.startswith("# branch.ab "):
                match = re.fullmatch(r"# branch\.ab ([+-]\d+) ([+-]\d+)", line)
                if match:
                    ahead = int(match.group(1))
                    behind = int(match.group(2))
            elif line and not line.startswith("#"):
                dirty = True
        detached = branch is None
        errors: list[str] = []
        if head_sha and not re.fullmatch(r"[0-9a-f]{40}", head_sha):
            errors.append("HEAD_SHA_INVALID")
            head_sha = None

        if upstream and (ahead is None or behind is None):
            try:
                counts = self.git_runner(
                    repository_path,
                    ("rev-list", "--left-right", "--count", "HEAD...@{upstream}"),
                ).split()
                if len(counts) == 2:
                    ahead, behind = int(counts[0]), int(counts[1])
            except (OSError, LocalRealityError, subprocess.TimeoutExpired, ValueError):
                errors.append("AHEAD_BEHIND_UNAVAILABLE")
        if not upstream:
            errors.append("UPSTREAM_UNAVAILABLE")

        try:
            worktree_text = self.git_runner(repository_path, ("worktree", "list", "--porcelain"))
            worktree_paths = tuple(
                line.removeprefix("worktree ").strip()
                for line in worktree_text.splitlines()
                if line.startswith("worktree ")
            )
        except (OSError, LocalRealityError, subprocess.TimeoutExpired):
            worktree_paths = tuple()
            errors.append("WORKTREE_METADATA_UNAVAILABLE")
        current_path = str(repository_path)
        matching_worktree = next(
            (self._display_path(Path(item)) for item in worktree_paths if str(Path(item).resolve()) == current_path),
            None,
        )
        try:
            head_commit_at = self.git_runner(repository_path, ("show", "-s", "--format=%cI", "HEAD")) or None
        except (OSError, LocalRealityError, subprocess.TimeoutExpired):
            head_commit_at = None
            errors.append("HEAD_TIMESTAMP_UNAVAILABLE")

        document_path: Path | None = None
        document_observed_at = None
        declared_branch = None
        declared_head = None
        if status_document is not None:
            document_path = Path(status_document)
            if not document_path.is_absolute():
                document_path = repository_path / document_path
            document_path = document_path.resolve()
            try:
                document_path.relative_to(repository_path)
            except ValueError:
                document_path = None
                errors.append("STATUS_DOCUMENT_OUTSIDE_REPOSITORY")
            if document_path is not None and document_path.is_file():
                document_observed_at, declared_branch, declared_head = _parse_document_metadata(document_path)
                if document_observed_at is None:
                    errors.append("DOCUMENT_OBSERVATION_MISSING")
            elif document_path is not None:
                errors.append("STATUS_DOCUMENT_MISSING")

        stale_reasons: list[str] = []
        if declared_branch and branch and declared_branch != branch:
            stale_reasons.append("DOCUMENT_BRANCH_MISMATCH")
        if declared_head and head_sha and not head_sha.startswith(declared_head):
            stale_reasons.append("DOCUMENT_HEAD_MISMATCH")
        observed = _parse_timestamp(document_observed_at)
        committed = _parse_timestamp(head_commit_at)
        current_time = self._current_time()
        if observed and observed > current_time:
            stale_reasons.append("DOCUMENT_OBSERVATION_IN_FUTURE")
        elif observed and (current_time - observed).days > self.freshness_days:
            stale_reasons.append("DOCUMENT_OBSERVATION_OLD")
        if observed and committed:
            if document_observed_at and len(document_observed_at) == 10:
                # A date-only observation is defined on the UTC calendar.  Git
                # commit timestamps may carry a local offset, so compare both
                # dates in UTC instead of treating the offset as a later day.
                before_head = observed.date() < committed.astimezone(timezone.utc).date()
            else:
                before_head = observed < committed
            if before_head:
                stale_reasons.append("DOCUMENT_BEFORE_HEAD")

        if stale_reasons:
            status = "stale"
            errors.extend(stale_reasons)
        elif (
            not document_path
            or not document_observed_at
            or not declared_branch
            or not declared_head
            or detached
            or not upstream
            or not head_sha
        ):
            status = "partial"
        elif errors:
            status = "partial"
        else:
            status = "fresh"

        return LocalRepositorySnapshot(
            repository=repository,
            path=self._display_path(repository_path),
            status=status,
            branch=branch,
            head_sha=head_sha,
            dirty=dirty,
            upstream=upstream,
            ahead=ahead,
            behind=behind,
            detached=detached,
            worktree=bool(worktree_paths),
            worktree_path=matching_worktree,
            worktree_paths=tuple(self._display_path(Path(item)) for item in worktree_paths),
            status_document=(self._display_path(document_path) if document_path else None),
            document_observed_at=document_observed_at,
            document_declared_branch=declared_branch,
            document_declared_head=declared_head,
            head_commit_at=head_commit_at,
            collected_at=collected_at,
            errors=tuple(dict.fromkeys(errors)),
        )

    def scan_all(
        self,
        projects: Mapping[str, str | Path],
        *,
        status_documents: Mapping[str, str | Path | None] | None = None,
    ) -> tuple[LocalRepositorySnapshot, ...]:
        documents = status_documents or {}
        return tuple(
            self.scan(
                repository,
                projects[repository],
                status_document=documents.get(repository, "docs/project/status.md"),
            )
            for repository in sorted(projects)
        )
