"""Fail-closed lifecycle for registered-agent delegation.

The lifecycle deliberately separates execution availability from validation
outcomes.  A quiet child is still a delegated task while its thread is alive;
silence alone must never become a validation HOLD or create a duplicate child.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Any, Mapping


LIFECYCLE_CONTRACT_VERSION = "1.0"
DEFAULT_SILENCE_POLL_INTERVAL_SECONDS = 60
DEFAULT_WATCHDOG_TIMEOUT_SECONDS = 300
MAX_INTERRUPT_REQUESTS = 1
MAX_FINAL_REPORT_REQUESTS = 1

_ACTIVE_STATES = frozenset({"launching", "acknowledged", "working", "awaiting_result"})
_TERMINAL_STATES = frozenset(
    {
        "completed",
        "launch_failed",
        "child_stalled",
        "result_missing",
        "interrupt_failed",
        "blocked",
    }
)
_GATE_STATES = frozenset({"not_started", "pending", "pass", "hold", "blocked"})
_NEXT_ACTIONS = frozenset(
    {
        "poll_same_child",
        "read_same_child_result",
        "record_result",
        "record_blocked",
        "await_interrupt_result",
        "continue_gate",
        "triage_findings",
    }
)
_PENDING_GATE = "pending"


class DelegationLifecycleError(ValueError):
    """Raised when a delegation event violates the lifecycle contract."""


def advance_after_wait(
    lifecycle: "DelegationLifecycle",
    *,
    elapsed_seconds: int | float,
    timed_out: bool,
    poll_interval_seconds: int = DEFAULT_SILENCE_POLL_INTERVAL_SECONDS,
    watchdog_timeout_seconds: int = DEFAULT_WATCHDOG_TIMEOUT_SECONDS,
) -> "DelegationLifecycle":
    """Apply a wait outcome without turning a poll timeout into a failure.

    ``elapsed_seconds`` is the total silence duration for this task, not the
    length of the latest wait call.  A timed-out poll before the watchdog emits
    ``silence_observed``; only the watchdog boundary emits
    ``watchdog_expired`` and schedules the bounded interrupt path.
    """

    if (
        isinstance(elapsed_seconds, bool)
        or not isinstance(elapsed_seconds, (int, float))
        or not math.isfinite(elapsed_seconds)
    ):
        raise DelegationLifecycleError("elapsed_seconds must be a non-negative number")
    if elapsed_seconds < 0:
        raise DelegationLifecycleError("elapsed_seconds must be a non-negative number")
    if not isinstance(timed_out, bool):
        raise DelegationLifecycleError("timed_out must be boolean")
    if (
        isinstance(poll_interval_seconds, bool)
        or not isinstance(poll_interval_seconds, int)
        or poll_interval_seconds < 1
    ):
        raise DelegationLifecycleError("poll_interval_seconds must be a positive integer")
    if (
        isinstance(watchdog_timeout_seconds, bool)
        or not isinstance(watchdog_timeout_seconds, int)
        or watchdog_timeout_seconds < poll_interval_seconds
    ):
        raise DelegationLifecycleError(
            "watchdog_timeout_seconds must be at least the poll interval"
        )
    if not lifecycle.is_active:
        raise DelegationLifecycleError("wait outcomes require an active child")
    if not timed_out:
        return lifecycle
    if elapsed_seconds < watchdog_timeout_seconds:
        return lifecycle.observe("silence_observed")
    return lifecycle.observe("watchdog_expired")


@dataclass(frozen=True)
class DelegationLifecycle:
    """Deterministic state for one registered-agent task.

    ``gate_status`` is intentionally independent from ``state``.  In
    particular, a silent but still-running child remains ``pending`` and
    user-visible ``delegated``; it is not a validation HOLD.
    """

    task_id: str
    runtime_agent: str
    state: str = "launching"
    child_thread_id: str | None = None
    attempt: int = 1
    max_attempts: int = 1
    interrupt_requests: int = 0
    final_report_requests: int = 0
    last_event: str = "spawn_requested"
    next_action: str = "poll_same_child"
    gate_status: str = _PENDING_GATE
    promotion_allowed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise DelegationLifecycleError("task_id must be non-empty")
        if (
            not isinstance(self.runtime_agent, str)
            or not self.runtime_agent.strip()
            or re.fullmatch(r"[a-z0-9_]+", self.runtime_agent) is None
        ):
            raise DelegationLifecycleError("runtime_agent must be non-empty")
        if self.child_thread_id is not None and (
            not isinstance(self.child_thread_id, str) or not self.child_thread_id.strip()
        ):
            raise DelegationLifecycleError("child_thread_id must be a string or null")
        if (
            not isinstance(self.state, str)
            or self.state not in _ACTIVE_STATES | _TERMINAL_STATES
        ):
            raise DelegationLifecycleError(f"unknown delegation state: {self.state}")
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool):
            raise DelegationLifecycleError("attempt must be an integer")
        if not isinstance(self.max_attempts, int) or isinstance(self.max_attempts, bool):
            raise DelegationLifecycleError("max_attempts must be an integer")
        if self.attempt < 1 or self.max_attempts < 1 or self.attempt > self.max_attempts:
            raise DelegationLifecycleError("attempt must be within max_attempts")
        if not isinstance(self.final_report_requests, int) or isinstance(
            self.final_report_requests, bool
        ):
            raise DelegationLifecycleError("final_report_requests must be an integer")
        if not isinstance(self.interrupt_requests, int) or isinstance(
            self.interrupt_requests, bool
        ):
            raise DelegationLifecycleError("interrupt_requests must be an integer")
        if not 0 <= self.interrupt_requests <= MAX_INTERRUPT_REQUESTS:
            raise DelegationLifecycleError("interrupt_requests exceeds the retry limit")
        if not 0 <= self.final_report_requests <= MAX_FINAL_REPORT_REQUESTS:
            raise DelegationLifecycleError("final_report_requests exceeds the retry limit")
        if not isinstance(self.last_event, str) or not self.last_event.strip():
            raise DelegationLifecycleError("last_event must be non-empty")
        if not isinstance(self.next_action, str) or self.next_action not in _NEXT_ACTIONS:
            raise DelegationLifecycleError(f"unknown next action: {self.next_action}")
        if not isinstance(self.gate_status, str) or self.gate_status not in _GATE_STATES:
            raise DelegationLifecycleError(f"unknown gate status: {self.gate_status}")
        if not isinstance(self.promotion_allowed, bool):
            raise DelegationLifecycleError("promotion_allowed must be boolean")
        if self.promotion_allowed and self.gate_status != "pass":
            raise DelegationLifecycleError("promotion is allowed only after a gate pass")

    @property
    def is_active(self) -> bool:
        return self.state in _ACTIVE_STATES

    @property
    def user_visible_status(self) -> str:
        """Return the handoff status exposed to the conversation layer."""

        if self.gate_status == "hold":
            # A completed validator can still own a non-terminal HOLD recovery
            # loop. Keep the conversation delegated until the gate passes.
            return "delegated"
        if self.gate_status == "blocked" or self.state in {
            "launch_failed",
            "child_stalled",
            "result_missing",
            "interrupt_failed",
            "blocked",
        }:
            return "blocked"
        if self.state == "completed":
            return "completed"
        return "delegated"

    @property
    def is_validation_hold(self) -> bool:
        """Silence and runtime failures are not validation findings."""

        return self.gate_status == "hold"

    def observe(self, event: str, *, child_thread_id: str | None = None) -> "DelegationLifecycle":
        """Apply one runtime event without creating a new child implicitly."""

        if not isinstance(event, str) or not event.strip():
            raise DelegationLifecycleError("event must be non-empty")
        updates: dict[str, Any] = {"last_event": event}
        if child_thread_id is not None:
            if not isinstance(child_thread_id, str) or not child_thread_id.strip():
                raise DelegationLifecycleError("child_thread_id must be non-empty")
            if self.child_thread_id and self.child_thread_id != child_thread_id:
                raise DelegationLifecycleError("a task cannot switch child thread implicitly")
            updates["child_thread_id"] = child_thread_id

        if event == "child_started":
            if not updates.get("child_thread_id", self.child_thread_id):
                raise DelegationLifecycleError("child_started requires child_thread_id")
            updates.update(state="acknowledged", next_action="poll_same_child")
        elif event in {"enter_received", "progress_received"}:
            if not self.child_thread_id and not updates.get("child_thread_id"):
                raise DelegationLifecycleError(f"{event} requires a child thread")
            updates.update(state="working", next_action="poll_same_child")
        elif event == "silence_observed":
            if not self.is_active or not self.child_thread_id:
                raise DelegationLifecycleError("silence can only be observed for an active child")
            # This is the critical invariant: a polling interval never becomes
            # a retry, validation finding, or HOLD.
            updates.update(next_action="poll_same_child")
        elif event == "watchdog_expired":
            if self.state not in {"acknowledged", "working"} or not self.child_thread_id:
                raise DelegationLifecycleError("watchdog expiry requires an active child")
            # Watchdog expiry schedules the bounded same-child interrupt path;
            # it is not a child failure and cannot block the validation gate.
            updates.update(next_action="await_interrupt_result")
        elif event == "child_completed":
            if not self.is_active or not self.child_thread_id:
                raise DelegationLifecycleError("child_completed requires a child thread")
            updates.update(state="awaiting_result", next_action="read_same_child_result")
        elif event == "result_received":
            if self.state not in {"acknowledged", "working", "awaiting_result"}:
                raise DelegationLifecycleError("result_received requires an active child")
            if not self.child_thread_id:
                raise DelegationLifecycleError("result_received requires a child thread")
            updates.update(state="completed", next_action="record_result")
        elif event == "final_report_requested":
            if self.state not in {"acknowledged", "working", "awaiting_result"}:
                raise DelegationLifecycleError(
                    "final report may be requested only for an active or terminal child"
                )
            if (
                self.state in {"acknowledged", "working"}
                and self.interrupt_requests != MAX_INTERRUPT_REQUESTS
            ):
                raise DelegationLifecycleError(
                    "an active child requires an interrupt before the final report request"
                )
            if self.final_report_requests >= MAX_FINAL_REPORT_REQUESTS:
                raise DelegationLifecycleError("final report request limit exceeded")
            updates.update(
                state="awaiting_result",
                final_report_requests=self.final_report_requests + 1,
                next_action="read_same_child_result",
            )
        elif event == "result_missing":
            if self.final_report_requests < MAX_FINAL_REPORT_REQUESTS:
                raise DelegationLifecycleError(
                    "result_missing requires the bounded final report request"
                )
            # A runtime failure is blocked, not a validation HOLD.  The gate
            # remains non-promotable and the thread/evidence is preserved.
            updates.update(
                state="result_missing",
                gate_status="blocked",
                next_action="record_blocked",
            )
        elif event == "launch_failed":
            updates.update(
                state="launch_failed",
                gate_status="blocked",
                next_action="record_blocked",
            )
        elif event == "child_stalled":
            updates.update(
                state="child_stalled",
                gate_status="blocked",
                next_action="record_blocked",
            )
        elif event == "interrupt_requested":
            if self.state not in {"acknowledged", "working"}:
                raise DelegationLifecycleError(
                    "interrupt can only be requested for an active child"
                )
            if self.interrupt_requests >= MAX_INTERRUPT_REQUESTS:
                raise DelegationLifecycleError("interrupt request limit exceeded")
            updates.update(
                interrupt_requests=self.interrupt_requests + 1,
                next_action="await_interrupt_result",
            )
        elif event == "interrupt_confirmed":
            if (
                self.last_event != "interrupt_requested"
                or self.interrupt_requests != MAX_INTERRUPT_REQUESTS
            ):
                raise DelegationLifecycleError(
                    "interrupt confirmation requires a requested interrupt"
                )
            if not self.child_thread_id:
                raise DelegationLifecycleError("interrupt_confirmed requires a child thread")
            updates.update(state="awaiting_result", next_action="read_same_child_result")
        elif event == "interrupt_failed":
            updates.update(
                state="interrupt_failed",
                gate_status="blocked",
                next_action="record_blocked",
            )
        elif event == "validation_passed":
            if self.state != "completed":
                raise DelegationLifecycleError("validation cannot pass before a result is received")
            updates.update(gate_status="pass", promotion_allowed=True, next_action="continue_gate")
        elif event == "validation_held":
            if self.state != "completed":
                raise DelegationLifecycleError(
                    "validation HOLD requires a received validation result"
                )
            updates.update(
                gate_status="hold",
                promotion_allowed=False,
                next_action="triage_findings",
            )
        elif event == "validation_blocked":
            updates.update(
                gate_status="blocked",
                promotion_allowed=False,
                next_action="record_blocked",
            )
        else:
            raise DelegationLifecycleError(f"unknown delegation event: {event}")

        return replace(self, **updates)

    def to_dict(self) -> dict[str, Any]:
        """Serialize a redaction-safe lifecycle snapshot."""

        return {
            "contract_version": LIFECYCLE_CONTRACT_VERSION,
            "task_id": self.task_id,
            "runtime_agent": self.runtime_agent,
            "state": self.state,
            "child_thread_id": self.child_thread_id,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "interrupt_requests": self.interrupt_requests,
            "final_report_requests": self.final_report_requests,
            "last_event": self.last_event,
            "next_action": self.next_action,
            "gate_status": self.gate_status,
            "promotion_allowed": self.promotion_allowed,
            "user_visible_status": self.user_visible_status,
        }


def validate_lifecycle_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a serialized lifecycle snapshot and return a safe copy."""

    required = (
        "contract_version",
        "task_id",
        "runtime_agent",
        "state",
        "child_thread_id",
        "attempt",
        "max_attempts",
        "interrupt_requests",
        "final_report_requests",
        "last_event",
        "next_action",
        "gate_status",
        "promotion_allowed",
        "user_visible_status",
    )
    if not isinstance(snapshot, Mapping):
        raise DelegationLifecycleError("lifecycle snapshot must be an object")
    missing = [key for key in required if key not in snapshot]
    if missing:
        raise DelegationLifecycleError("lifecycle snapshot is missing: " + ", ".join(missing))
    if snapshot["contract_version"] != LIFECYCLE_CONTRACT_VERSION:
        raise DelegationLifecycleError("unsupported lifecycle contract version")
    unexpected = sorted(str(key) for key in snapshot if key not in required)
    if unexpected:
        raise DelegationLifecycleError(
            "lifecycle snapshot has unknown fields: " + ", ".join(unexpected)
        )
    for key in ("task_id", "runtime_agent", "state", "last_event", "next_action", "gate_status"):
        if not isinstance(snapshot[key], str):
            raise DelegationLifecycleError(f"{key} must be a string")
    if snapshot["child_thread_id"] is not None and not isinstance(snapshot["child_thread_id"], str):
        raise DelegationLifecycleError("child_thread_id must be a string or null")
    for key in (
        "attempt",
        "max_attempts",
        "interrupt_requests",
        "final_report_requests",
    ):
        if not isinstance(snapshot[key], int) or isinstance(snapshot[key], bool):
            raise DelegationLifecycleError(f"{key} must be an integer")
    if not isinstance(snapshot["promotion_allowed"], bool):
        raise DelegationLifecycleError("promotion_allowed must be boolean")
    if not isinstance(snapshot["user_visible_status"], str):
        raise DelegationLifecycleError("user_visible_status must be a string")
    lifecycle = DelegationLifecycle(
        task_id=snapshot["task_id"],
        runtime_agent=snapshot["runtime_agent"],
        state=snapshot["state"],
        child_thread_id=snapshot["child_thread_id"],
        attempt=snapshot["attempt"],
        max_attempts=snapshot["max_attempts"],
        interrupt_requests=snapshot["interrupt_requests"],
        final_report_requests=snapshot["final_report_requests"],
        last_event=snapshot["last_event"],
        next_action=snapshot["next_action"],
        gate_status=snapshot["gate_status"],
        promotion_allowed=snapshot["promotion_allowed"],
    )
    normalized = lifecycle.to_dict()
    if snapshot["user_visible_status"] != normalized["user_visible_status"]:
        raise DelegationLifecycleError("user_visible_status does not match lifecycle state")
    return normalized
