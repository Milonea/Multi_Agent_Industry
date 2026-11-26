"""Deterministic, side-effect-free Goal execution fixture.

This module is an integration seam for contract tests.  It does not spawn a
Codex child, touch a repository, or call a provider.  A test supplies a runtime
callback that returns a validated run/result envelope, a transient runtime
state, or ``None``.  The executor then proves the ordering, bounded retry,
dependency blocking, evidence rendering, and donor routing rules that a real
adapter must preserve.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from .contracts import ContractError, validate_run_result
from .evidence import EvidenceReport
from .retry import TaskRetryError, TaskRetryState


TERMINAL_TASK_STATES = frozenset(
    {"PASS", "HOLD", "MANUAL", "FAIL", "BLOCKED", "ROUTED"}
)
BLOCKING_TASK_STATES = frozenset({"HOLD", "MANUAL", "FAIL", "BLOCKED", "ROUTED"})
VALID_RUNTIME_STATES = frozenset(
    {"TIMED_OUT", "STALLED", "INTERRUPTED", "LAUNCH_FAILED", "FAILED"}
)


class GoalExecutionError(ValueError):
    """Raised when a fixture Goal graph or result is malformed."""


@dataclass(frozen=True)
class GoalTask:
    task_id: str
    project: str
    capability: str
    runtime_agent: str
    dependencies: tuple[str, ...] = tuple()
    role: str = "core"
    canonical_owner: str | None = None
    canonical_queue: str | None = None
    contract: str | None = None
    expected_write: bool = False
    max_total_attempts: int = 2

    def __post_init__(self) -> None:
        for field in ("task_id", "project", "capability", "runtime_agent"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise GoalExecutionError(f"{field} must be non-empty")
        if not isinstance(self.role, str) or self.role not in {
            "core",
            "donor-legacy",
            "independent",
            "reference",
            "role-pending",
            "portfolio-evidence",
        }:
            raise GoalExecutionError("role is invalid")
        for field in ("canonical_owner", "canonical_queue", "contract"):
            value = getattr(self, field)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise GoalExecutionError(f"{field} must be a non-empty string or null")
        if not isinstance(self.dependencies, tuple) or not all(
            isinstance(item, str) and item.strip() for item in self.dependencies
        ):
            raise GoalExecutionError("dependencies must be a tuple of non-empty strings")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise GoalExecutionError("dependencies must not contain duplicates")
        if not isinstance(self.expected_write, bool):
            raise GoalExecutionError("expected_write must be boolean")
        if (
            isinstance(self.max_total_attempts, bool)
            or not isinstance(self.max_total_attempts, int)
            or self.max_total_attempts < 1
        ):
            raise GoalExecutionError("max_total_attempts must be a positive integer")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GoalTask":
        if not isinstance(data, Mapping):
            raise GoalExecutionError("task must be an object")
        dependencies = data.get("dependencies", [])
        if not isinstance(dependencies, list):
            raise GoalExecutionError("task.dependencies must be an array")
        return cls(
            task_id=data.get("task_id", ""),
            project=data.get("project", ""),
            capability=data.get("capability", ""),
            runtime_agent=data.get("runtime_agent", ""),
            dependencies=tuple(dependencies),
            role=data.get("role", "core"),
            canonical_owner=data.get("canonical_owner"),
            canonical_queue=data.get("canonical_queue"),
            contract=data.get("contract"),
            expected_write=data.get("expected_write", False),
            max_total_attempts=data.get("max_total_attempts", 2),
        )


@dataclass(frozen=True)
class GoalExecutionResult:
    goal_id: str
    status: str
    execution_order: tuple[str, ...]
    records: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "status": self.status,
            "execution_order": list(self.execution_order),
            "records": [dict(record) for record in self.records],
        }


RuntimeCallback = Callable[[GoalTask, int], Mapping[str, Any] | None]


class FixtureGoalExecutor:
    """Run a bounded Goal graph using an injected fixture runtime."""

    def __init__(self, *, allow_scoped_no_output_retry: bool = True) -> None:
        self.allow_scoped_no_output_retry = allow_scoped_no_output_retry

    def run(
        self,
        goal_id: str,
        tasks: Iterable[GoalTask | Mapping[str, Any]],
        runtime: RuntimeCallback,
    ) -> GoalExecutionResult:
        if not isinstance(goal_id, str) or not goal_id.strip():
            raise GoalExecutionError("goal_id must be non-empty")
        if not callable(runtime):
            raise GoalExecutionError("runtime must be callable")

        try:
            raw_tasks = list(tasks)
        except TypeError as exc:
            raise GoalExecutionError("tasks must be iterable") from exc
        normalized = [
            task if isinstance(task, GoalTask) else GoalTask.from_dict(task)
            for task in raw_tasks
        ]
        task_by_id: dict[str, GoalTask] = {}
        for task in normalized:
            if task.task_id in task_by_id:
                raise GoalExecutionError(f"duplicate task_id: {task.task_id}")
            task_by_id[task.task_id] = task
        for task in normalized:
            missing = sorted(set(task.dependencies) - set(task_by_id))
            if missing:
                raise GoalExecutionError(
                    f"{task.task_id} has missing dependencies: {', '.join(missing)}"
                )

        pending = set(task_by_id)
        records: dict[str, dict[str, Any]] = {}
        execution_order: list[str] = []

        while pending:
            progressed = False
            for task_id in sorted(pending):
                task = task_by_id[task_id]
                dependency_records = [records.get(dep) for dep in task.dependencies]
                if any(record is None for record in dependency_records):
                    continue
                blocked_dependencies = [
                    dep
                    for dep, record in zip(task.dependencies, dependency_records)
                    if record is not None and record["status"] in BLOCKING_TASK_STATES
                ]
                if blocked_dependencies:
                    records[task_id] = {
                        "task_id": task_id,
                        "project": task.project,
                        "runtime_agent": task.runtime_agent,
                        "status": "BLOCKED",
                        "executed": False,
                        "attempts": 0,
                        "reason": "dependency is not promotable",
                        "blocked_by": blocked_dependencies,
                        "evidence": [],
                    }
                    pending.remove(task_id)
                    execution_order.append(task_id)
                    progressed = True
                    continue

                if task.role == "donor-legacy":
                    records[task_id] = self._route_donor(task)
                    pending.remove(task_id)
                    execution_order.append(task_id)
                    progressed = True
                    continue

                records[task_id] = self._execute_task(task, runtime)
                pending.remove(task_id)
                execution_order.append(task_id)
                progressed = True

            if not progressed:
                # A cycle or an unresolved graph is never executed implicitly.
                for task_id in sorted(pending):
                    records[task_id] = {
                        "task_id": task_id,
                        "project": task_by_id[task_id].project,
                        "runtime_agent": task_by_id[task_id].runtime_agent,
                        "status": "BLOCKED",
                        "executed": False,
                        "attempts": 0,
                        "reason": "unresolved dependency cycle or graph state",
                        "blocked_by": list(task_by_id[task_id].dependencies),
                        "evidence": [],
                    }
                    execution_order.append(task_id)
                pending.clear()

        ordered_records = tuple(records[task_id] for task_id in execution_order)
        statuses = {record["status"] for record in ordered_records}
        if "FAIL" in statuses:
            status = "FAIL"
        elif "BLOCKED" in statuses:
            status = "BLOCKED"
        elif "MANUAL" in statuses:
            status = "MANUAL"
        elif "HOLD" in statuses:
            status = "HOLD"
        else:
            status = "PASS"
        return GoalExecutionResult(goal_id, status, tuple(execution_order), ordered_records)

    def _route_donor(self, task: GoalTask) -> dict[str, Any]:
        if not task.canonical_owner or not task.canonical_queue or not task.contract:
            return {
                "task_id": task.task_id,
                "project": task.project,
                "runtime_agent": task.runtime_agent,
                "status": "BLOCKED",
                "executed": False,
                "attempts": 0,
                "reason": "donor requires canonical owner, queue, and contract",
                "blocked_by": [],
                "evidence": [],
            }
        return {
            "task_id": task.task_id,
            "project": task.project,
            "runtime_agent": task.runtime_agent,
            "status": "ROUTED",
            "executed": False,
            "attempts": 0,
            "reason": "donor work is routed to canonical owner",
            "route": {
                "canonical_owner": task.canonical_owner,
                "canonical_queue": task.canonical_queue,
                "contract": task.contract,
            },
            "evidence": [],
        }

    def _execute_task(self, task: GoalTask, runtime: RuntimeCallback) -> dict[str, Any]:
        retry = TaskRetryState.new(
            task.task_id,
            expected_write=task.expected_write,
            max_total_attempts=task.max_total_attempts,
        )
        runtime_states: list[str] = []
        evidence: list[dict[str, Any]] = []

        while True:
            try:
                retry = retry.start_attempt(
                    root_authorized=(retry.attempt_count > 0),
                )
            except TaskRetryError as exc:
                return self._runtime_record(
                    task,
                    retry,
                    runtime_states,
                    evidence,
                    status="BLOCKED",
                    reason=str(exc),
                )

            attempt = retry.attempt_count
            output = runtime(task, attempt)
            if output is None:
                runtime_states.append("INCOMPLETE_NO_OUTPUT")
                retry = retry.record_runtime_result("INCOMPLETE_NO_OUTPUT")
                if (
                    retry.last_runtime_state == "INCOMPLETE_NO_OUTPUT"
                    and retry.retry_budget_remaining > 0
                    and self.allow_scoped_no_output_retry
                ):
                    continue
                return self._runtime_record(
                    task,
                    retry,
                    runtime_states,
                    evidence,
                    status="HOLD",
                    reason=retry.hold_reason or "required result was not returned",
                )

            if not isinstance(output, Mapping):
                raise GoalExecutionError("fixture runtime must return an object or None")
            if "runtime_state" in output and "schema_version" not in output:
                runtime_state = output["runtime_state"]
                if not isinstance(runtime_state, str) or runtime_state not in VALID_RUNTIME_STATES:
                    raise GoalExecutionError("fixture runtime_state is invalid")
                runtime_states.append(runtime_state)
                retry = retry.record_runtime_result(runtime_state)
                if retry.automatic_retry_allowed:
                    continue
                reason = "runtime retry budget exhausted or root retry is required"
                if retry.retry_budget_remaining > 0:
                    reason = "runtime retry requires explicit root authorization"
                return self._runtime_record(
                    task,
                    retry,
                    runtime_states,
                    evidence,
                    status="HOLD" if retry.retry_budget_remaining == 0 else "BLOCKED",
                    reason=reason,
                )

            try:
                envelope = validate_run_result(output)
            except ContractError as exc:
                raise GoalExecutionError(f"fixture run result is invalid: {exc}") from exc
            report = EvidenceReport.from_envelope(envelope, agent=task.runtime_agent)
            evidence.append(report.to_dict())
            result_status = envelope["result"]["status"]
            if result_status == "PASS":
                status = "PASS"
            elif result_status == "NEEDS_MANUAL":
                status = "MANUAL"
            elif result_status in {"HOLD_AUTO", "HOLD_MANUAL"}:
                status = "HOLD"
            else:
                status = "FAIL"
            retry = retry.record_runtime_result(
                "COMPLETED",
                changed_files=envelope["result"]["changed_paths"],
                progress_observed=True,
            )
            return self._runtime_record(
                task,
                retry,
                runtime_states,
                evidence,
                status=status,
                reason=envelope["result"]["summary"],
            )

    @staticmethod
    def _runtime_record(
        task: GoalTask,
        retry: TaskRetryState,
        runtime_states: list[str],
        evidence: list[dict[str, Any]],
        *,
        status: str,
        reason: str,
    ) -> dict[str, Any]:
        if status not in TERMINAL_TASK_STATES:
            raise GoalExecutionError("invalid fixture terminal task status")
        return {
            "task_id": task.task_id,
            "project": task.project,
            "runtime_agent": task.runtime_agent,
            "status": status,
            "executed": True,
            "attempts": retry.attempt_count,
            "runtime_states": list(runtime_states),
            "retry": retry.as_dict(),
            "reason": reason,
            "evidence": evidence,
        }


__all__ = [
    "BLOCKING_TASK_STATES",
    "FixtureGoalExecutor",
    "GoalExecutionError",
    "GoalExecutionResult",
    "GoalTask",
    "TERMINAL_TASK_STATES",
]
