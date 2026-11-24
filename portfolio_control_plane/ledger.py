"""Read-only Project State Ledger v0 snapshot model and loader."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .retry import TaskRetryError, TaskRetryState


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
PROJECT_STATES = frozenset({"unknown", "planned", "in_progress", "blocked", "verified", "stale"})
GATE_STATES = frozenset({"not_required", "pending", "passed", "blocked"})
_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_version",
        "repository",
        "project_state",
        "active_branch",
        "active_pr",
        "current_goal",
        "blocker",
        "last_verified_sha",
        "last_pass_at",
        "validation_cycle",
        "manual_gate",
        "next_executable_work",
        "external_write_permission",
        "observed_at",
        "history",
        "task_retry",
    }
)
_ACTIVE_PR_FIELDS = frozenset({"number", "head_sha", "state"})
_MANUAL_GATE_FIELDS = frozenset({"required", "status", "reason"})
_HISTORY_FIELDS = frozenset({"state", "changed_at", "reason"})
_SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "credential",
    "oauth",
    "raw_log",
    "conversation",
    "event_title",
    "attendee",
    "location",
)


class StateLedgerError(ValueError):
    """Raised when a state snapshot cannot be safely consumed."""


def _required(mapping: Mapping[str, Any], fields: tuple[str, ...], scope: str) -> None:
    missing = [field for field in fields if field not in mapping]
    if missing:
        raise StateLedgerError(f"{scope} is missing required fields: {', '.join(missing)}")


def _no_unexpected(mapping: Mapping[str, Any], allowed: frozenset[str], scope: str) -> None:
    unexpected = sorted(str(key) for key in mapping if key not in allowed)
    if unexpected:
        raise StateLedgerError(f"{scope} contains unsupported fields: {', '.join(unexpected)}")


def _timestamp(value: Any, field: str, *, allow_none: bool = False) -> None:
    if value is None and allow_none:
        return
    if not isinstance(value, str) or not value.strip():
        suffix = " or null" if allow_none else ""
        raise StateLedgerError(f"{field} must be an ISO timestamp{suffix}")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateLedgerError(f"{field} must be an ISO timestamp") from exc


def _safe_keys(value: Any, path: str = "snapshot") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                raise StateLedgerError(f"sensitive field is not allowed: {path}.{key}")
            _safe_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _safe_keys(child, f"{path}[{index}]")


def validate_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        raise StateLedgerError("snapshot must be an object")
    _safe_keys(snapshot)
    _no_unexpected(snapshot, _SNAPSHOT_FIELDS, "snapshot")
    _required(
        snapshot,
        (
            "schema_version",
            "repository",
            "project_state",
            "active_branch",
            "active_pr",
            "current_goal",
            "blocker",
            "last_verified_sha",
            "last_pass_at",
            "validation_cycle",
            "manual_gate",
            "next_executable_work",
            "external_write_permission",
            "observed_at",
            "history",
        ),
        "snapshot",
    )
    if snapshot["schema_version"] != "0.1":
        raise StateLedgerError(f"unsupported ledger version: {snapshot['schema_version']!r}")
    if not isinstance(snapshot["repository"], str) or not REPOSITORY_RE.fullmatch(snapshot["repository"]):
        raise StateLedgerError("repository must be an owner/name string")
    if not isinstance(snapshot["project_state"], str) or snapshot["project_state"] not in PROJECT_STATES:
        raise StateLedgerError("project_state is not supported")
    if "task_retry" in snapshot and snapshot["task_retry"] is not None:
        try:
            TaskRetryState.from_dict(snapshot["task_retry"])
        except TaskRetryError as exc:
            raise StateLedgerError(f"task_retry is invalid: {exc}") from exc
    if snapshot["active_branch"] is not None and (
        not isinstance(snapshot["active_branch"], str) or not snapshot["active_branch"].strip()
    ):
        raise StateLedgerError("active_branch must be a non-empty string or null")
    if snapshot["active_pr"] is not None:
        if not isinstance(snapshot["active_pr"], Mapping):
            raise StateLedgerError("active_pr must be an object or null")
        _no_unexpected(snapshot["active_pr"], _ACTIVE_PR_FIELDS, "active_pr")
        _required(snapshot["active_pr"], ("number", "head_sha", "state"), "active_pr")
        if type(snapshot["active_pr"]["number"]) is not int or snapshot["active_pr"]["number"] < 1:
            raise StateLedgerError("active_pr.number must be a positive integer")
        if not isinstance(snapshot["active_pr"]["head_sha"], str) or not SHA_RE.fullmatch(snapshot["active_pr"]["head_sha"]):
            raise StateLedgerError("active_pr.head_sha must be a 40-character lowercase SHA")
        if not isinstance(snapshot["active_pr"]["state"], str) or snapshot["active_pr"]["state"] not in {"open", "draft", "closed", "merged"}:
            raise StateLedgerError("active_pr.state is invalid")
    if not isinstance(snapshot["current_goal"], str) or not snapshot["current_goal"].strip():
        raise StateLedgerError("current_goal must be a non-empty string")
    if not isinstance(snapshot["blocker"], list) or not all(isinstance(item, str) for item in snapshot["blocker"]):
        raise StateLedgerError("blocker must be an array of strings")
    if snapshot["last_verified_sha"] is not None and (
        not isinstance(snapshot["last_verified_sha"], str)
        or not SHA_RE.fullmatch(snapshot["last_verified_sha"])
    ):
        raise StateLedgerError("last_verified_sha must be a SHA or null")
    _timestamp(snapshot["last_pass_at"], "last_pass_at", allow_none=True)
    cycle = snapshot["validation_cycle"]
    if not isinstance(cycle, int) or isinstance(cycle, bool) or cycle < 0:
        raise StateLedgerError("validation_cycle must be a non-negative integer")
    gate = snapshot["manual_gate"]
    if not isinstance(gate, Mapping):
        raise StateLedgerError("manual_gate must be an object")
    _no_unexpected(gate, _MANUAL_GATE_FIELDS, "manual_gate")
    _required(gate, ("required", "status", "reason"), "manual_gate")
    if not isinstance(gate["required"], bool) or not isinstance(gate["status"], str) or gate["status"] not in GATE_STATES:
        raise StateLedgerError("manual_gate.required/status are invalid")
    if (not gate["required"]) != (gate["status"] == "not_required"):
        raise StateLedgerError("manual_gate.required and status are inconsistent")
    if gate["reason"] is not None and not isinstance(gate["reason"], str):
        raise StateLedgerError("manual_gate.reason must be a string or null")
    if gate["status"] in {"pending", "blocked"} and (
        not isinstance(gate["reason"], str) or not gate["reason"].strip()
    ):
        raise StateLedgerError("manual_gate pending/blocked status requires a reason")
    if not isinstance(snapshot["next_executable_work"], str) and snapshot["next_executable_work"] is not None:
        raise StateLedgerError("next_executable_work must be a string or null")
    if not isinstance(snapshot["external_write_permission"], bool):
        raise StateLedgerError("external_write_permission must be boolean")
    _timestamp(snapshot["observed_at"], "observed_at")
    if not isinstance(snapshot["history"], list):
        raise StateLedgerError("history must be an array")
    for index, item in enumerate(snapshot["history"]):
        if not isinstance(item, Mapping):
            raise StateLedgerError(f"history[{index}] must be an object")
        _no_unexpected(item, _HISTORY_FIELDS, f"history[{index}]")
        _required(item, ("state", "changed_at", "reason"), f"history[{index}]")
        if not isinstance(item["state"], str) or item["state"] not in PROJECT_STATES or not isinstance(item["reason"], str):
            raise StateLedgerError(f"history[{index}] is invalid")
        _timestamp(item["changed_at"], f"history[{index}].changed_at")
    return json.loads(json.dumps(snapshot, ensure_ascii=False))


@dataclass(frozen=True)
class ProjectSnapshot:
    repository: str
    project_state: str
    active_branch: str | None
    active_pr: dict[str, Any] | None
    current_goal: str
    blocker: tuple[str, ...]
    last_verified_sha: str | None
    last_pass_at: str | None
    validation_cycle: int
    manual_gate: dict[str, Any]
    next_executable_work: str | None
    external_write_permission: bool
    observed_at: str
    history: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    task_retry: TaskRetryState | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProjectSnapshot":
        snapshot = validate_snapshot(data)
        return cls(
            repository=snapshot["repository"],
            project_state=snapshot["project_state"],
            active_branch=snapshot["active_branch"],
            active_pr=dict(snapshot["active_pr"]) if snapshot["active_pr"] else None,
            current_goal=snapshot["current_goal"],
            blocker=tuple(snapshot["blocker"]),
            last_verified_sha=snapshot["last_verified_sha"],
            last_pass_at=snapshot["last_pass_at"],
            validation_cycle=snapshot["validation_cycle"],
            manual_gate=dict(snapshot["manual_gate"]),
            next_executable_work=snapshot["next_executable_work"],
            external_write_permission=snapshot["external_write_permission"],
            observed_at=snapshot["observed_at"],
            history=tuple(dict(item) for item in snapshot["history"]),
            task_retry=(
                TaskRetryState.from_dict(snapshot["task_retry"])
                if snapshot.get("task_retry") is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": "0.1",
            "repository": self.repository,
            "project_state": self.project_state,
            "active_branch": self.active_branch,
            "active_pr": dict(self.active_pr) if self.active_pr else None,
            "current_goal": self.current_goal,
            "blocker": list(self.blocker),
            "last_verified_sha": self.last_verified_sha,
            "last_pass_at": self.last_pass_at,
            "validation_cycle": self.validation_cycle,
            "manual_gate": dict(self.manual_gate),
            "next_executable_work": self.next_executable_work,
            "external_write_permission": self.external_write_permission,
            "observed_at": self.observed_at,
            "history": [dict(item) for item in self.history],
        }
        if self.task_retry is not None:
            result["task_retry"] = self.task_retry.as_dict()
        return result


def load_snapshot(path: str | Path) -> ProjectSnapshot:
    """Load one snapshot without performing writes or network access."""

    snapshot_path = Path(path)
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    return ProjectSnapshot.from_dict(data)
