from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import unittest

from portfolio_control_plane.read_only_goal import (
    ReadOnlyGoalExecutor,
    ReadOnlyGoalTarget,
)
from portfolio_control_plane.reality import LocalRepositorySnapshot


@dataclass(frozen=True)
class FakePreflight:
    status: str
    reason: str = "FIXTURE"

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "reason": self.reason}


def snapshot(*, status: str = "fresh") -> LocalRepositorySnapshot:
    return LocalRepositorySnapshot(
        repository="fixture/project",
        path="project",
        status=status,
        branch="main",
        head_sha="a" * 40,
        dirty=False,
        upstream="origin/main",
        ahead=0,
        behind=0,
        detached=False,
        worktree=True,
        worktree_path="project",
        worktree_paths=("project",),
        status_document="project/docs/project/status.md",
        document_observed_at="2026-09-08",
        document_declared_branch="main",
        document_declared_head="a" * 40,
        head_commit_at="2026-09-08T00:00:00+00:00",
        collected_at=datetime.now(timezone.utc).isoformat(),
        errors=(),
    )


TARGET = ReadOnlyGoalTarget(
    repository="fixture/project",
    project_root="project",
    route_id="fixture-agent",
    assigned_agent_id="fixture-agent-instance",
    runtime_agent="fixture_runtime",
    assignment_file="assignments/fixture.yaml",
    status_document="docs/project/status.md",
)


class ReadOnlyGoalTests(unittest.TestCase):
    def test_input_types_are_rejected_before_execution(self) -> None:
        with self.assertRaises(ValueError):
            ReadOnlyGoalExecutor(max_total_attempts=True)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            ReadOnlyGoalTarget("fixture/project", "project", "route", "agent", "runtime", "assignment", "")

    def test_non_object_execution_result_is_bounded_as_a_failed_turn(self) -> None:
        result = self.run_goal(execute=lambda _target, _request, _attempt: [])

        self.assertEqual(result.status, "HOLD")
        self.assertEqual(result.reason, "TURN_FAILED")
        self.assertEqual(result.retry_count, 1)
    def run_goal(
        self,
        *,
        reality: LocalRepositorySnapshot | None = None,
        preflight: FakePreflight | None = None,
        execute=None,
    ):
        return ReadOnlyGoalExecutor().run(
            goal_id="readonly-fixture",
            request="inspect the current repository",
            target=TARGET,
            reality=reality or snapshot(),
            preflight=preflight or FakePreflight("PASS"),
            execute=execute,
        )

    def test_runtime_preflight_failure_holds_without_execution(self) -> None:
        calls: list[int] = []

        def execute(_target, _request, attempt):
            calls.append(attempt)
            return {"status": "PASS"}

        result = self.run_goal(
            preflight=FakePreflight("SANDBOX_UNAVAILABLE"),
            execute=execute,
        )

        self.assertEqual(result.status, "HOLD")
        self.assertEqual(result.reason, "SANDBOX_UNAVAILABLE")
        self.assertEqual(calls, [])
        self.assertEqual(result.retry_count, 0)
        self.assertFalse(result.envelope["result"]["external_writes"]["attempted"])
        self.assertEqual(result.evidence["final_state"], "NEEDS_MANUAL")

    def test_passed_preflight_without_explicit_turn_stays_read_only_hold(self) -> None:
        result = self.run_goal()

        self.assertEqual(result.status, "HOLD")
        self.assertEqual(result.reason, "TURN_PROBE_NOT_REQUESTED")
        self.assertEqual(result.execution["status"], "NOT_RUN")
        self.assertEqual(result.retry_count, 0)

    def test_execution_retries_once_and_returns_validated_evidence(self) -> None:
        calls: list[int] = []

        def execute(_target, _request, attempt):
            calls.append(attempt)
            if attempt == 1:
                raise TimeoutError("fixture timeout")
            return {"status": "PASS", "summary": "fixture result"}

        result = self.run_goal(execute=execute)

        self.assertEqual(result.status, "PASS")
        self.assertEqual(calls, [1, 2])
        self.assertEqual(result.retry_count, 1)
        self.assertIsNone(result.reason)
        self.assertEqual(result.envelope["result"]["changed_paths"], [])
        self.assertFalse(result.envelope["result"]["external_writes"]["attempted"])

    def test_unknown_execution_reason_is_not_emitted_as_free_text(self) -> None:
        def execute(_target, _request, _attempt):
            return {"status": "FAIL", "reason": "secret internal traceback"}

        result = self.run_goal(execute=execute)

        self.assertEqual(result.status, "HOLD")
        self.assertEqual(result.reason, "TURN_FAILED")
        self.assertNotIn("secret internal traceback", str(result.to_dict()))
        self.assertEqual(result.retry_count, 1)

    def test_unavailable_reality_holds_before_preflight_or_execution(self) -> None:
        result = self.run_goal(reality=snapshot(status="unavailable"))

        self.assertEqual(result.status, "HOLD")
        self.assertEqual(result.reason, "REALITY_UNAVAILABLE")
        self.assertEqual(result.execution["status"], "NOT_RUN")


if __name__ == "__main__":
    unittest.main()
