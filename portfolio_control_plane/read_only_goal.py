"""Minimal read-only Goal orchestration over real local repository state."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Mapping, Protocol

from .contracts import make_run_result
from .evidence import EvidenceReport, redact_text
from .reality import LocalRepositorySnapshot


class PreflightResult(Protocol):
    status: str

    def to_dict(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ReadOnlyGoalTarget:
    repository: str
    project_root: str
    route_id: str
    assigned_agent_id: str
    runtime_agent: str
    assignment_file: str
    status_document: str

    def __post_init__(self) -> None:
        for field in (
            "repository",
            "project_root",
            "route_id",
            "assigned_agent_id",
            "runtime_agent",
            "assignment_file",
            "status_document",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be non-empty")

    def to_dict(self) -> dict[str, str]:
        return {
            "repository": self.repository,
            "project_root": self.project_root,
            "route_id": self.route_id,
            "assigned_agent_id": self.assigned_agent_id,
            "runtime_agent": self.runtime_agent,
            "assignment_file": self.assignment_file,
            "status_document": self.status_document,
        }


ExecutionCallback = Callable[
    [ReadOnlyGoalTarget, str, int],
    Mapping[str, Any],
]


@dataclass(frozen=True)
class ReadOnlyGoalResult:
    status: str
    goal_id: str
    request: str
    target: ReadOnlyGoalTarget
    reality: dict[str, Any]
    preflight: dict[str, Any]
    execution: dict[str, Any]
    retry_count: int
    reason: str | None
    envelope: dict[str, Any]
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "0.1",
            "status": self.status,
            "goal_id": self.goal_id,
            "request": redact_text(self.request),
            "target": self.target.to_dict(),
            "reality": dict(self.reality),
            "preflight": dict(self.preflight),
            "execution": dict(self.execution),
            "retry_count": self.retry_count,
            "reason": self.reason,
            "envelope": dict(self.envelope),
            "evidence": dict(self.evidence),
        }


def _reason_from_exception(exc: Exception) -> str:
    text = f"{type(exc).__name__} {exc}".casefold()
    if "timeout" in text or "timed out" in text:
        return "TURN_TIMEOUT"
    return "TURN_FAILED"


_REASON_CODES = frozenset(
    {
        "REALITY_UNAVAILABLE",
        "TURN_PROBE_NOT_REQUESTED",
        "AUTH_UNAVAILABLE",
        "STATE_INIT_FAILED",
        "SANDBOX_UNAVAILABLE",
        "APP_SERVER_UNAVAILABLE",
        "TURN_TIMEOUT",
        "TURN_FAILED",
        "ENVIRONMENT_BLOCKED",
        "UNKNOWN",
        "DELEGATION_TRACE_MISMATCH",
    }
)


def _reason_code(value: Any, *, fallback: str = "TURN_FAILED") -> str:
    candidate = str(value or "").strip().upper()
    if candidate in _REASON_CODES and re.fullmatch(r"[A-Z0-9_]+", candidate):
        return candidate
    return fallback


class ReadOnlyGoalExecutor:
    """Run one selected target with explicit reality, preflight, and retry gates."""

    def __init__(self, *, max_total_attempts: int = 2) -> None:
        if (
            isinstance(max_total_attempts, bool)
            or not isinstance(max_total_attempts, int)
            or max_total_attempts < 1
        ):
            raise ValueError("max_total_attempts must be positive")
        self.max_total_attempts = max_total_attempts

    def run(
        self,
        *,
        goal_id: str,
        request: str,
        target: ReadOnlyGoalTarget,
        reality: LocalRepositorySnapshot,
        preflight: PreflightResult,
        execute: ExecutionCallback | None = None,
    ) -> ReadOnlyGoalResult:
        if not isinstance(goal_id, str) or not isinstance(request, str):
            raise ValueError("goal_id and request must be strings")
        if not goal_id.strip() or not request.strip():
            raise ValueError("goal_id and request are required")
        if not isinstance(target, ReadOnlyGoalTarget):
            raise ValueError("target must be a ReadOnlyGoalTarget")
        if not callable(getattr(reality, "to_dict", None)) or not isinstance(
            getattr(reality, "status", None), str
        ):
            raise ValueError("reality must provide a string status and to_dict()")
        if not callable(getattr(preflight, "to_dict", None)) or not isinstance(
            getattr(preflight, "status", None), str
        ):
            raise ValueError("preflight must provide a string status and to_dict()")
        reality_data = reality.to_dict()
        preflight_data = preflight.to_dict()
        if not isinstance(reality_data, Mapping) or not isinstance(preflight_data, Mapping):
            raise ValueError("reality.to_dict() and preflight.to_dict() must return objects")
        reality_data = dict(reality_data)
        preflight_data = dict(preflight_data)

        if reality.status in {"unknown", "unavailable"}:
            return self._hold(
                goal_id=goal_id,
                request=request,
                target=target,
                reality=reality_data,
                preflight=preflight_data,
                execution={"status": "NOT_RUN", "reason": "REALITY_UNAVAILABLE"},
                retry_count=0,
                reason="REALITY_UNAVAILABLE",
            )
        if preflight.status != "PASS":
            preflight_reason = _reason_code(preflight.status, fallback="UNKNOWN")
            return self._hold(
                goal_id=goal_id,
                request=request,
                target=target,
                reality=reality_data,
                preflight=preflight_data,
                execution={"status": "NOT_RUN", "reason": preflight_reason},
                retry_count=0,
                reason=preflight_reason,
            )
        if execute is None:
            return self._hold(
                goal_id=goal_id,
                request=request,
                target=target,
                reality=reality_data,
                preflight=preflight_data,
                execution={"status": "NOT_RUN", "reason": "TURN_PROBE_NOT_REQUESTED"},
                retry_count=0,
                reason="TURN_PROBE_NOT_REQUESTED",
            )

        attempts = 0
        last_reason = "TURN_FAILED"
        while attempts < self.max_total_attempts:
            attempts += 1
            try:
                raw_execution = execute(target, request, attempts)
                if not isinstance(raw_execution, Mapping):
                    raise ValueError("execution callback must return an object")
                execution = dict(raw_execution)
            except Exception as exc:
                last_reason = _reason_from_exception(exc)
                execution = {"status": "FAIL", "reason": last_reason}
            if execution.get("status") == "PASS":
                return self._pass(
                    goal_id=goal_id,
                    request=request,
                    target=target,
                    reality=reality_data,
                    preflight=preflight_data,
                    execution=execution,
                    retry_count=attempts - 1,
                )
            last_reason = _reason_code(execution.get("reason"), fallback=last_reason)

        return self._hold(
            goal_id=goal_id,
            request=request,
            target=target,
            reality=reality_data,
            preflight=preflight_data,
            execution={"status": "FAIL", "reason": last_reason},
            retry_count=attempts - 1,
            reason=last_reason,
        )

    def _pass(
        self,
        *,
        goal_id: str,
        request: str,
        target: ReadOnlyGoalTarget,
        reality: dict[str, Any],
        preflight: dict[str, Any],
        execution: dict[str, Any],
        retry_count: int,
    ) -> ReadOnlyGoalResult:
        envelope = self._envelope(
            goal_id=goal_id,
            request=request,
            target=target,
            reality=reality,
            result_status="PASS",
            tests=[
                {"name": "local reality", "status": "PASS"},
                {"name": "runtime preflight", "status": "PASS"},
                {"name": "read-only execution", "status": "PASS"},
            ],
            risks=list(reality.get("errors", [])),
            cycle=retry_count,
            findings=[],
            next_action="record read-only evidence",
        )
        evidence = EvidenceReport.from_envelope(envelope, agent=target.runtime_agent).to_dict()
        return ReadOnlyGoalResult(
            status="PASS",
            goal_id=goal_id,
            request=request,
            target=target,
            reality=reality,
            preflight=preflight,
            execution=execution,
            retry_count=retry_count,
            reason=None,
            envelope=envelope,
            evidence=evidence,
        )

    def _hold(
        self,
        *,
        goal_id: str,
        request: str,
        target: ReadOnlyGoalTarget,
        reality: dict[str, Any],
        preflight: dict[str, Any],
        execution: dict[str, Any],
        retry_count: int,
        reason: str,
    ) -> ReadOnlyGoalResult:
        envelope = self._envelope(
            goal_id=goal_id,
            request=request,
            target=target,
            reality=reality,
            result_status="NEEDS_MANUAL",
            tests=[
                {
                    "name": "local reality",
                    "status": "PASS" if reality.get("status") not in {"unknown", "unavailable"} else "UNKNOWN",
                },
                {
                    "name": "runtime preflight",
                    "status": "PASS" if preflight.get("status") == "PASS" else "UNKNOWN",
                },
                {"name": "read-only execution", "status": "UNKNOWN"},
            ],
            risks=[reason, *[str(item) for item in reality.get("errors", [])]],
            cycle=retry_count,
            findings=[reason],
            next_action=f"resolve {reason} and rerun the bounded read-only Goal",
        )
        evidence = EvidenceReport.from_envelope(envelope, agent=target.runtime_agent).to_dict()
        return ReadOnlyGoalResult(
            status="HOLD",
            goal_id=goal_id,
            request=request,
            target=target,
            reality=reality,
            preflight=preflight,
            execution=execution,
            retry_count=retry_count,
            reason=reason,
            envelope=envelope,
            evidence=evidence,
        )

    @staticmethod
    def _envelope(
        *,
        goal_id: str,
        request: str,
        target: ReadOnlyGoalTarget,
        reality: Mapping[str, Any],
        result_status: str,
        tests: list[dict[str, str]],
        risks: list[str],
        cycle: int,
        findings: list[str],
        next_action: str,
    ) -> dict[str, Any]:
        head_sha = reality.get("head_sha")
        if not isinstance(head_sha, str) or re.fullmatch(r"[0-9a-f]{40}", head_sha) is None:
            head_sha = "0" * 40
        return make_run_result(
            run={
                "id": goal_id,
                "project": target.repository,
                "capability": "agent.read_only_goal",
                "request_summary": redact_text(request)[:500],
                "base_sha": head_sha,
                "head_sha": head_sha,
            },
            result={
                "status": result_status,
                "summary": (
                    "read-only Goal completed"
                    if result_status == "PASS"
                    else "read-only Goal held at a verified gate"
                ),
                "changed_paths": [],
                "external_writes": {"attempted": False, "count": 0, "targets": []},
                "tests": tests,
                "risks": [redact_text(str(item)) for item in risks],
                "rollback": "No writes were permitted; no rollback is required.",
                "manual_gates": [] if result_status == "PASS" else [{"reason": findings[0]}],
            },
            validation={
                "validator": "read-only-goal",
                "cycle": cycle,
                "findings": [redact_text(str(item)) for item in findings],
                "next_action": redact_text(next_action),
            },
        )
