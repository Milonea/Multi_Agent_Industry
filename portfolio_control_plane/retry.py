"""Task-scoped retry and fallback circuit breaker.

The runtime lifecycle in :mod:`portfolio_control_plane.delegation` describes one
child thread.  This module deliberately tracks the larger task so that changing
the child runtime cannot reset the retry budget or turn a missing artifact into a
new fallback loop.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping


RETRY_POLICY_VERSION = "1.0"
DEFAULT_MAX_TOTAL_ATTEMPTS = 2
DEFAULT_MAX_FALLBACK_ATTEMPTS = 1

RUNTIME_STATES = frozenset(
    {
        "NOT_STARTED",
        "WORKING",
        "COMPLETED",
        "FAILED",
        "TIMED_OUT",
        "STALLED",
        "INTERRUPTED",
        "LAUNCH_FAILED",
        "RESULT_MISSING",
        "INCOMPLETE_NO_OUTPUT",
        "HOLD_RUNTIME",
    }
)
TRANSIENT_RUNTIME_STATES = frozenset(
    {"TIMED_OUT", "STALLED", "INTERRUPTED", "LAUNCH_FAILED"}
)
_TERMINAL_RESULT_STATES = frozenset({"COMPLETED", "FAILED"})
_REQUIRED_FIELDS = (
    "policy_version",
    "task_id",
    "expected_write",
    "attempt_count",
    "max_total_attempts",
    "fallback_attempt_count",
    "max_fallback_attempts",
    "retry_budget_remaining",
    "last_runtime_state",
    "progress_observed",
    "hold_reason",
)


class TaskRetryError(ValueError):
    """Raised when a task retry or fallback would violate its budget."""


def _require_non_empty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskRetryError(f"{field} must be a non-empty string")
    return value.strip()


def _require_count(value: Any, field: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise TaskRetryError(f"{field} must be an integer >= {minimum}")
    return value


def _normalize_runtime_state(value: str) -> str:
    if not isinstance(value, str):
        raise TaskRetryError("runtime state must be a string")
    normalized = value.strip().replace("-", "_").replace(" ", "_").upper()
    if normalized not in RUNTIME_STATES:
        raise TaskRetryError(f"unsupported runtime state: {value!r}")
    return normalized


@dataclass(frozen=True)
class TaskRetryState:
    """Immutable task-level state shared by every runtime assignment.

    ``attempt_count`` is zero before the first runtime attempt.  A fallback
    assignment increments the same counter and records a separate fallback
    count; it never creates a fresh budget.
    """

    task_id: str
    expected_write: bool = False
    attempt_count: int = 0
    max_total_attempts: int = DEFAULT_MAX_TOTAL_ATTEMPTS
    fallback_attempt_count: int = 0
    max_fallback_attempts: int = DEFAULT_MAX_FALLBACK_ATTEMPTS
    last_runtime_state: str = "NOT_STARTED"
    progress_observed: bool = False
    hold_reason: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.task_id, "task_id")
        if not isinstance(self.expected_write, bool):
            raise TaskRetryError("expected_write must be boolean")
        attempt_count = _require_count(self.attempt_count, "attempt_count")
        max_total_attempts = _require_count(
            self.max_total_attempts,
            "max_total_attempts",
            minimum=1,
        )
        fallback_attempt_count = _require_count(
            self.fallback_attempt_count,
            "fallback_attempt_count",
        )
        max_fallback_attempts = _require_count(
            self.max_fallback_attempts,
            "max_fallback_attempts",
        )
        if attempt_count > max_total_attempts:
            raise TaskRetryError("attempt_count exceeds max_total_attempts")
        if fallback_attempt_count > max_fallback_attempts:
            raise TaskRetryError("fallback_attempt_count exceeds max_fallback_attempts")
        if fallback_attempt_count > attempt_count:
            raise TaskRetryError("fallback attempts cannot exceed total attempts")
        normalized = _normalize_runtime_state(self.last_runtime_state)
        if normalized != self.last_runtime_state:
            raise TaskRetryError("last_runtime_state must use the canonical uppercase value")
        if attempt_count == 0 and normalized != "NOT_STARTED":
            raise TaskRetryError("a task without an attempt must be NOT_STARTED")
        if not isinstance(self.progress_observed, bool):
            raise TaskRetryError("progress_observed must be boolean")
        if self.hold_reason is not None:
            _require_non_empty(self.hold_reason, "hold_reason")
        if normalized == "HOLD_RUNTIME" and self.hold_reason is None:
            raise TaskRetryError("HOLD_RUNTIME requires hold_reason")

    @classmethod
    def new(
        cls,
        task_id: str,
        *,
        expected_write: bool = False,
        max_total_attempts: int = DEFAULT_MAX_TOTAL_ATTEMPTS,
        max_fallback_attempts: int = DEFAULT_MAX_FALLBACK_ATTEMPTS,
    ) -> "TaskRetryState":
        return cls(
            task_id=task_id,
            expected_write=expected_write,
            max_total_attempts=max_total_attempts,
            max_fallback_attempts=max_fallback_attempts,
        )

    @property
    def retry_budget_remaining(self) -> int:
        return max(self.max_total_attempts - self.attempt_count, 0)

    @property
    def fallback_budget_remaining(self) -> int:
        return max(self.max_fallback_attempts - self.fallback_attempt_count, 0)

    @property
    def is_held(self) -> bool:
        return self.last_runtime_state == "HOLD_RUNTIME"

    @property
    def automatic_retry_allowed(self) -> bool:
        """Whether the root may automatically retry the same task scope."""

        return (
            not self.is_held
            and self.retry_budget_remaining > 0
            and self.last_runtime_state in TRANSIENT_RUNTIME_STATES
        )

    def fallback_allowed(
        self,
        *,
        root_authorized: bool,
        reason: str | None,
    ) -> bool:
        """Return whether a root-authorized capability fallback is admissible."""

        return bool(
            root_authorized
            and isinstance(reason, str)
            and reason.strip()
            and not self.is_held
            and self.last_runtime_state != "INCOMPLETE_NO_OUTPUT"
            and self.attempt_count > 0
            and self.retry_budget_remaining > 0
            and self.fallback_budget_remaining > 0
        )

    def start_attempt(
        self,
        *,
        fallback: bool = False,
        root_authorized: bool = False,
        reason: str | None = None,
    ) -> "TaskRetryState":
        """Consume one task attempt without ever resetting its counters.

        Automatic callers may use this only after a concrete transient runtime
        state.  A root-authorized same-scope retry may recover an incomplete
        result, but a fallback runtime is never allowed after no output.
        """

        if self.is_held:
            raise TaskRetryError("HOLD_RUNTIME closes the task for this run")
        if self.retry_budget_remaining <= 0:
            raise TaskRetryError("task retry budget is exhausted")
        if fallback:
            if not self.fallback_allowed(
                root_authorized=root_authorized,
                reason=reason,
            ):
                raise TaskRetryError(
                    "fallback requires root authorization, a concrete reason, "
                    "and an unused fallback budget"
                )
        elif self.attempt_count > 0 and not (
            self.automatic_retry_allowed or root_authorized
        ):
            raise TaskRetryError(
                "automatic retry is limited to transient runtime conditions"
            )
        if self.last_runtime_state == "INCOMPLETE_NO_OUTPUT" and fallback:
            raise TaskRetryError("fallback is forbidden after INCOMPLETE_NO_OUTPUT")
        return replace(
            self,
            attempt_count=self.attempt_count + 1,
            fallback_attempt_count=(
                self.fallback_attempt_count + 1
                if fallback
                else self.fallback_attempt_count
            ),
            last_runtime_state="WORKING",
            progress_observed=False,
            hold_reason=None,
        )

    def record_runtime_result(
        self,
        runtime_state: str,
        *,
        changed_files: Iterable[str] = (),
        progress_observed: bool = False,
    ) -> "TaskRetryState":
        """Record runtime evidence and classify missing required artifacts.

        ``changed_files`` is intentionally consumed only as a count.  It never
        becomes part of the retry state, avoiding accidental path disclosure.
        """

        if self.attempt_count < 1:
            raise TaskRetryError("runtime results require a started attempt")
        if not isinstance(progress_observed, bool):
            raise TaskRetryError("progress_observed must be boolean")
        if isinstance(changed_files, (str, bytes)):
            raise TaskRetryError("changed_files must be an iterable of paths")
        try:
            has_changed_files = any(True for _ in changed_files)
        except TypeError as exc:
            raise TaskRetryError("changed_files must be an iterable of paths") from exc
        normalized = _normalize_runtime_state(runtime_state)
        hold_reason: str | None = None

        if normalized == "HOLD_RUNTIME":
            hold_reason = self.hold_reason or "root_orchestrator_runtime_hold"
        elif normalized == "INCOMPLETE_NO_OUTPUT":
            hold_reason = "required_artifact_missing"
        elif (
            self.expected_write
            and normalized in _TERMINAL_RESULT_STATES
            and not has_changed_files
        ):
            normalized = "INCOMPLETE_NO_OUTPUT"
            hold_reason = "required_artifact_missing"

        if normalized == "INCOMPLETE_NO_OUTPUT" and self.attempt_count >= self.max_total_attempts:
            normalized = "HOLD_RUNTIME"
            hold_reason = "repeated_no_progress"

        return replace(
            self,
            last_runtime_state=normalized,
            progress_observed=progress_observed,
            hold_reason=hold_reason,
        )

    def hold_runtime(self, reason: str) -> "TaskRetryState":
        return replace(
            self,
            last_runtime_state="HOLD_RUNTIME",
            hold_reason=_require_non_empty(reason, "reason"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_version": RETRY_POLICY_VERSION,
            "task_id": self.task_id,
            "expected_write": self.expected_write,
            "attempt_count": self.attempt_count,
            "max_total_attempts": self.max_total_attempts,
            "fallback_attempt_count": self.fallback_attempt_count,
            "max_fallback_attempts": self.max_fallback_attempts,
            "retry_budget_remaining": self.retry_budget_remaining,
            "last_runtime_state": self.last_runtime_state,
            "progress_observed": self.progress_observed,
            "hold_reason": self.hold_reason,
        }

    to_dict = as_dict

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskRetryState":
        if not isinstance(data, Mapping):
            raise TaskRetryError("task retry state must be an object")
        missing = [field for field in _REQUIRED_FIELDS if field not in data]
        if missing:
            raise TaskRetryError(
                "task retry state is missing: " + ", ".join(missing)
            )
        unexpected = sorted(set(data) - set(_REQUIRED_FIELDS))
        if unexpected:
            raise TaskRetryError(
                "task retry state has unknown fields: " + ", ".join(unexpected)
            )
        if data["policy_version"] != RETRY_POLICY_VERSION:
            raise TaskRetryError("unsupported task retry policy version")
        state = cls(
            task_id=data["task_id"],
            expected_write=data["expected_write"],
            attempt_count=data["attempt_count"],
            max_total_attempts=data["max_total_attempts"],
            fallback_attempt_count=data["fallback_attempt_count"],
            max_fallback_attempts=data["max_fallback_attempts"],
            last_runtime_state=data["last_runtime_state"],
            progress_observed=data["progress_observed"],
            hold_reason=data["hold_reason"],
        )
        if data["retry_budget_remaining"] != state.retry_budget_remaining:
            raise TaskRetryError("retry_budget_remaining does not match attempt_count")
        return state


__all__ = [
    "DEFAULT_MAX_FALLBACK_ATTEMPTS",
    "DEFAULT_MAX_TOTAL_ATTEMPTS",
    "RUNTIME_STATES",
    "RETRY_POLICY_VERSION",
    "TRANSIENT_RUNTIME_STATES",
    "TaskRetryError",
    "TaskRetryState",
]
