"""Read-only proposals for bringing status documents back to runtime truth."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .reality import LocalRepositorySnapshot


FRESH = "fresh"
STALE = "stale"
PARTIAL = "partial"
CONFLICTING = "conflicting"
UNKNOWN = "unknown"
APPROVALS_DIRECTORY = ".tmp.documentation-approvals"


@dataclass(frozen=True)
class DocumentationProposal:
    """A minimal, non-mutating update suggestion for one status document."""

    repository: str
    document: str | None
    status: str
    observed: dict[str, Any]
    recorded: dict[str, Any]
    drift: tuple[str, ...]
    suggested_update: dict[str, Any] | None
    confidence: str
    evidence: tuple[str, ...]
    document_sha256: str | None = None
    read_only: bool = True
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "repository": self.repository,
            "document": self.document,
            "status": self.status,
            "observed": dict(self.observed),
            "recorded": dict(self.recorded),
            "drift": list(self.drift),
            "suggested_update": self.suggested_update,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "document_sha256": self.document_sha256,
            "read_only": self.read_only,
            "mutations": [],
            "generated_at": self.generated_at,
        }

    @property
    def proposal_id(self) -> str:
        """Return an immutable identifier for this exact observation/proposal."""

        stable_observed = dict(self.observed)
        # Collection time changes on every scan; it is evidence, not proposal
        # identity. The Git/document fields remain stable until meaningful
        # runtime or document state changes.
        stable_observed.pop("collected_at", None)
        identity = {
            "repository": self.repository,
            "document": self.document,
            "status": self.status,
            "observed": stable_observed,
            "recorded": self.recorded,
            "drift": self.drift,
            "suggested_update": self.suggested_update,
            "document_sha256": self.document_sha256,
        }
        digest = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()[:20]
        return f"doc-{digest}"


def _observed(snapshot: LocalRepositorySnapshot) -> dict[str, Any]:
    return {
        "path": snapshot.path,
        "branch": snapshot.branch,
        "head_sha": snapshot.head_sha,
        "upstream": snapshot.upstream,
        "ahead": snapshot.ahead,
        "behind": snapshot.behind,
        "dirty": snapshot.dirty,
        "detached": snapshot.detached,
        "worktree": snapshot.worktree,
        "worktree_path": snapshot.worktree_path,
        "head_commit_at": snapshot.head_commit_at,
        "collected_at": snapshot.collected_at,
    }


def _recorded(snapshot: LocalRepositorySnapshot) -> dict[str, Any]:
    return {
        "document_observed_at": snapshot.document_observed_at,
        "branch": snapshot.document_declared_branch,
        "head_sha": snapshot.document_declared_head,
    }


def _display(value: Any) -> str:
    if value is None or value == "":
        return "(missing)"
    return str(value)


def _document_path(workspace_root: Path | None, document: str | None) -> Path | None:
    if workspace_root is None or not document:
        return None
    root = workspace_root.resolve()
    candidate = (root / document).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _classification(snapshot: LocalRepositorySnapshot) -> str:
    if snapshot.status == "fresh":
        return FRESH
    if snapshot.status == "unavailable":
        return UNKNOWN
    errors = set(snapshot.errors)
    if {"DOCUMENT_BRANCH_MISMATCH", "DOCUMENT_HEAD_MISMATCH"}.issubset(errors):
        return CONFLICTING
    if snapshot.status == "stale":
        return STALE
    if snapshot.status == "partial":
        return PARTIAL
    return UNKNOWN


def _drift(snapshot: LocalRepositorySnapshot, status: str) -> list[str]:
    errors = set(snapshot.errors)
    drift: list[str] = []
    if snapshot.branch and snapshot.document_declared_branch != snapshot.branch:
        if snapshot.document_declared_branch:
            drift.append(
                f"branch: recorded {_display(snapshot.document_declared_branch)} "
                f"-> actual {_display(snapshot.branch)}"
            )
        else:
            drift.append(f"branch: missing in document -> actual {_display(snapshot.branch)}")
    if snapshot.head_sha and (
        not snapshot.document_declared_head
        or not snapshot.head_sha.startswith(snapshot.document_declared_head)
    ):
        if snapshot.document_declared_head:
            drift.append(
                f"HEAD: recorded {_display(snapshot.document_declared_head)} "
                f"-> actual {_display(snapshot.head_sha)}"
            )
        else:
            drift.append(f"HEAD: missing in document -> actual {_display(snapshot.head_sha)}")
    if not snapshot.document_observed_at:
        drift.append("observation timestamp: missing in document")
    if "DOCUMENT_OBSERVATION_OLD" in errors:
        drift.append("observation timestamp: older than freshness threshold")
    if "DOCUMENT_OBSERVATION_IN_FUTURE" in errors:
        drift.append("observation timestamp: later than collection time")
    if "DOCUMENT_BEFORE_HEAD" in errors:
        drift.append("observation timestamp: predates current HEAD")
    if status == PARTIAL and not drift:
        if "STATUS_DOCUMENT_MISSING" in errors:
            drift.append("status document: missing")
        elif "UPSTREAM_UNAVAILABLE" in errors:
            drift.append("repository metadata: upstream unavailable")
        else:
            drift.append("repository/document metadata: incomplete")
    if status == UNKNOWN:
        drift.append("runtime repository observation: unavailable")
    return list(dict.fromkeys(drift))


def _suggested_update(
    snapshot: LocalRepositorySnapshot,
    status: str,
) -> tuple[dict[str, Any] | None, str]:
    if status == FRESH:
        return None, "high"
    if status == UNKNOWN:
        return None, "low"
    replacements: list[dict[str, Any]] = []
    if snapshot.branch or snapshot.detached:
        branch = snapshot.branch or "(detached HEAD)"
        head = snapshot.head_sha or "(unknown)"
        replacements.append(
            {
                "field": "local checkout",
                "replacement": f"- local checkout: `{branch}`, HEAD: `{head}`",
            }
        )
    if snapshot.document_observed_at or snapshot.status in {STALE, PARTIAL, CONFLICTING}:
        date = snapshot.collected_at[:10] if snapshot.collected_at else None
        if date:
            replacements.append(
                {
                    "field": "observation date",
                    "replacement": f"# Current snapshot — {date}",
                }
            )
    if not replacements:
        return None, "low"
    confidence = "high" if snapshot.branch and snapshot.head_sha else "medium"
    return {
        "strategy": "replace-matching-fields",
        "replacements": replacements,
        "automatic_write": False,
    }, confidence


def build_documentation_proposal(
    snapshot: LocalRepositorySnapshot,
    *,
    validation: Mapping[str, Any] | None = None,
    workspace_root: str | Path | None = None,
) -> DocumentationProposal:
    """Compare one recorded document observation to one local Git snapshot."""

    status = _classification(snapshot)
    observed = _observed(snapshot)
    if validation is not None:
        observed["validation"] = dict(validation)
    recorded = _recorded(snapshot)
    drift = _drift(snapshot, status)
    suggestion, confidence = _suggested_update(snapshot, status)
    evidence = ["local-git-runtime", "status-document-lead-metadata"]
    evidence.extend(snapshot.errors)
    document_sha256 = _sha256(
        _document_path(Path(workspace_root).resolve() if workspace_root else None, snapshot.status_document)
    )
    return DocumentationProposal(
        repository=snapshot.repository,
        document=snapshot.status_document,
        status=status,
        observed=observed,
        recorded=recorded,
        drift=tuple(drift),
        suggested_update=suggestion,
        confidence=confidence,
        evidence=tuple(dict.fromkeys(evidence)),
        document_sha256=document_sha256,
    )


def build_documentation_proposals(
    snapshots: Sequence[LocalRepositorySnapshot],
    *,
    validation_by_repository: Mapping[str, Mapping[str, Any]] | None = None,
    workspace_root: str | Path | None = None,
) -> tuple[DocumentationProposal, ...]:
    validation = validation_by_repository or {}
    return tuple(
        build_documentation_proposal(
            snapshot,
            validation=validation.get(snapshot.repository),
            workspace_root=workspace_root,
        )
        for snapshot in snapshots
    )


def _approval_directory(root: str | Path) -> Path:
    return Path(root).resolve() / APPROVALS_DIRECTORY


def _proposal_path(root: str | Path, proposal_id: str) -> Path:
    return _approval_directory(root) / "proposals" / f"{proposal_id}.json"


def _approval_path(root: str | Path, proposal_id: str) -> Path:
    return _approval_directory(root) / "approvals" / f"{proposal_id}.json"


def save_documentation_proposals(
    root: str | Path,
    proposals: Sequence[DocumentationProposal],
) -> tuple[str, ...]:
    """Persist proposal envelopes in an ignored local cache for explicit approval."""

    directory = _approval_directory(root) / "proposals"
    directory.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for proposal in proposals:
        path = directory / f"{proposal.proposal_id}.json"
        path.write_text(
            json.dumps(proposal.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        paths.append(str(path.relative_to(Path(root).resolve())))
    return tuple(paths)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def load_documentation_proposal(root: str | Path, proposal_id: str) -> dict[str, Any] | None:
    if not proposal_id.startswith("doc-") or "/" in proposal_id or "\\" in proposal_id:
        return None
    return _load_json(_proposal_path(root, proposal_id))


def load_documentation_approval(root: str | Path, proposal_id: str) -> dict[str, Any] | None:
    """Read the ignored approval record without changing proposal state."""

    if not proposal_id.startswith("doc-") or "/" in proposal_id or "\\" in proposal_id:
        return None
    return _load_json(_approval_path(root, proposal_id))


def _current_document_hash(root: str | Path, proposal: Mapping[str, Any]) -> tuple[Path | None, str | None]:
    document = proposal.get("document")
    if not isinstance(document, str) or not document:
        return None, None
    path = _document_path(Path(root).resolve(), document)
    return path, _sha256(path)


def approve_documentation_proposal(
    root: str | Path,
    proposal_id: str,
    *,
    approved_by: str,
    approved_at: str | None = None,
) -> dict[str, Any]:
    """Record an explicit approval without changing the target document."""

    proposal = load_documentation_proposal(root, proposal_id)
    if proposal is None:
        return {"status": "APPROVAL_REJECTED", "reason": "PROPOSAL_NOT_FOUND", "proposal_id": proposal_id}
    if proposal.get("proposal_id") != proposal_id:
        return {"status": "APPROVAL_REJECTED", "reason": "PROPOSAL_ID_MISMATCH", "proposal_id": proposal_id}
    proposal_status = proposal.get("status")
    if not isinstance(proposal_status, str) or proposal_status in {FRESH, UNKNOWN} or not proposal.get("suggested_update"):
        return {"status": "APPROVAL_REJECTED", "reason": "PROPOSAL_NOT_APPLICABLE", "proposal_id": proposal_id}
    if not isinstance(approved_by, str) or not approved_by.strip():
        return {"status": "APPROVAL_REJECTED", "reason": "APPROVED_BY_REQUIRED", "proposal_id": proposal_id}
    if approved_at is not None:
        if not isinstance(approved_at, str) or not approved_at.strip():
            return {"status": "APPROVAL_REJECTED", "reason": "APPROVED_AT_INVALID", "proposal_id": proposal_id}
        try:
            datetime.fromisoformat(approved_at.strip().replace("Z", "+00:00"))
        except ValueError:
            return {"status": "APPROVAL_REJECTED", "reason": "APPROVED_AT_INVALID", "proposal_id": proposal_id}
        approval_timestamp = approved_at.strip()
    else:
        approval_timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    document_path, current_hash = _current_document_hash(root, proposal)
    expected_hash = proposal.get("document_sha256")
    if document_path is None or not expected_hash:
        return {"status": "APPROVAL_REJECTED", "reason": "DOCUMENT_HASH_UNAVAILABLE", "proposal_id": proposal_id}
    if current_hash != expected_hash:
        return {
            "status": "STALE_PROPOSAL",
            "reason": "DOCUMENT_CHANGED_SINCE_PROPOSAL",
            "proposal_id": proposal_id,
            "document": proposal.get("document"),
        }
    approval = {
        "schema_version": "0.1",
        "approval_id": f"approval-{proposal_id.removeprefix('doc-')}",
        "status": "APPROVED",
        "proposal_id": proposal_id,
        "document": proposal.get("document"),
        "expected_document_sha256": expected_hash,
        "approved_by": approved_by.strip(),
        "approved_at": approval_timestamp,
        "read_only": True,
        "mutations": [],
    }
    path = _approval_path(root, proposal_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(approval, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return approval


def _replacement_prefix(field: str) -> str | None:
    return {
        "local checkout": "- local checkout:",
        "observation date": "# Current snapshot —",
    }.get(field)


def _apply_replacements(
    text: str,
    replacements: Sequence[Mapping[str, Any]],
) -> tuple[str | None, str | None]:
    lines = text.splitlines(keepends=True)
    for replacement in replacements:
        field = replacement.get("field")
        value = replacement.get("replacement")
        prefix = _replacement_prefix(str(field))
        if not prefix or not isinstance(value, str):
            return None, "PATCH_SPEC_INVALID"
        candidates = [
            index
            for index, line in enumerate(lines[:40])
            if line.rstrip("\r\n").startswith(prefix)
        ]
        if len(candidates) != 1:
            return None, "PATCH_TARGET_NOT_FOUND"
        index = candidates[0]
        ending = "\n" if lines[index].endswith("\n") else ""
        lines[index] = value + ending
    return "".join(lines), None


def apply_documentation_proposal(root: str | Path, proposal_id: str) -> dict[str, Any]:
    """Apply only an approved, hash-matching minimal proposal."""

    proposal = load_documentation_proposal(root, proposal_id)
    if proposal is None:
        return {"status": "APPLY_REJECTED", "reason": "PROPOSAL_NOT_FOUND", "proposal_id": proposal_id}
    approval = _load_json(_approval_path(root, proposal_id))
    if approval is None or approval.get("status") != "APPROVED":
        return {"status": "APPLY_REJECTED", "reason": "EXPLICIT_APPROVAL_REQUIRED", "proposal_id": proposal_id}
    if approval.get("proposal_id") != proposal_id:
        return {"status": "APPLY_REJECTED", "reason": "APPROVAL_ID_MISMATCH", "proposal_id": proposal_id}
    document_path, current_hash = _current_document_hash(root, proposal)
    expected_hash = proposal.get("document_sha256")
    if document_path is None or not expected_hash:
        return {"status": "APPLY_REJECTED", "reason": "DOCUMENT_HASH_UNAVAILABLE", "proposal_id": proposal_id}
    if current_hash != expected_hash or approval.get("expected_document_sha256") != expected_hash:
        return {
            "status": "STALE_PROPOSAL",
            "reason": "DOCUMENT_CHANGED_SINCE_APPROVAL",
            "proposal_id": proposal_id,
            "document": proposal.get("document"),
            "mutations": [],
        }
    suggested = proposal.get("suggested_update")
    replacements = suggested.get("replacements") if isinstance(suggested, Mapping) else None
    if not isinstance(replacements, list):
        return {"status": "APPLY_REJECTED", "reason": "PATCH_SPEC_INVALID", "proposal_id": proposal_id}
    try:
        with document_path.open("r", encoding="utf-8", newline="") as stream:
            original = stream.read()
        updated, error = _apply_replacements(original, replacements)
    except OSError:
        return {"status": "APPLY_REJECTED", "reason": "DOCUMENT_READ_FAILED", "proposal_id": proposal_id}
    if error or updated is None:
        return {"status": "APPLY_REJECTED", "reason": error or "PATCH_FAILED", "proposal_id": proposal_id}
    if updated == original:
        return {"status": "APPLIED", "reason": "NO_CONTENT_CHANGE", "proposal_id": proposal_id, "mutations": []}
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=str(proposal.get("document")),
            tofile=str(proposal.get("document")),
        )
    )
    try:
        with document_path.open("w", encoding="utf-8", newline="") as stream:
            stream.write(updated)
    except OSError:
        return {"status": "APPLY_REJECTED", "reason": "DOCUMENT_WRITE_FAILED", "proposal_id": proposal_id}
    approval["status"] = "APPLIED"
    approval["applied_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    approval["read_only"] = False
    approval["mutations"] = [str(document_path.relative_to(Path(root).resolve()))]
    approval_path = _approval_path(root, proposal_id)
    temporary_approval_path = approval_path.with_name(approval_path.name + ".tmp")
    try:
        temporary_approval_path.write_text(
            json.dumps(approval, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_approval_path, approval_path)
    except OSError:
        try:
            temporary_approval_path.unlink(missing_ok=True)
            with document_path.open("w", encoding="utf-8", newline="") as stream:
                stream.write(original)
        except OSError:
            return {
                "status": "HOLD_MANUAL",
                "reason": "ROLLBACK_FAILED",
                "proposal_id": proposal_id,
                "document": proposal.get("document"),
                "mutations": [proposal.get("document")],
            }
        return {
            "status": "APPLY_REJECTED",
            "reason": "APPROVAL_UPDATE_FAILED",
            "proposal_id": proposal_id,
            "document": proposal.get("document"),
            "mutations": [],
        }
    return {
        "status": "APPLIED",
        "reason": "MINIMAL_PATCH_APPLIED",
        "proposal_id": proposal_id,
        "document": proposal.get("document"),
        "diff": diff,
        "mutations": [proposal.get("document")],
        "read_only": False,
        "rollback": "Revert the tracked document before commit if the approved patch is not desired.",
    }


__all__ = [
    "APPROVALS_DIRECTORY",
    "CONFLICTING",
    "FRESH",
    "PARTIAL",
    "STALE",
    "UNKNOWN",
    "DocumentationProposal",
    "apply_documentation_proposal",
    "approve_documentation_proposal",
    "build_documentation_proposal",
    "build_documentation_proposals",
    "load_documentation_proposal",
    "load_documentation_approval",
    "save_documentation_proposals",
]
