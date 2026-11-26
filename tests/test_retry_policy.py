from __future__ import annotations

import unittest

from portfolio_control_plane.ledger import StateLedgerError, ProjectSnapshot
from portfolio_control_plane.retry import TaskRetryError, TaskRetryState


class TaskRetryPolicyTests(unittest.TestCase):
    def test_no_output_is_not_validation_failure_and_repeated_no_output_holds(self) -> None:
        first = TaskRetryState.new("20260901-write-task", expected_write=True).start_attempt()

        incomplete = first.record_runtime_result("COMPLETED", changed_files=())

        self.assertEqual(incomplete.last_runtime_state, "INCOMPLETE_NO_OUTPUT")
        self.assertEqual(incomplete.retry_budget_remaining, 1)
        self.assertFalse(incomplete.automatic_retry_allowed)
        self.assertFalse(
            incomplete.fallback_allowed(
                root_authorized=True,
                reason="capability mismatch",
            )
        )

        second = incomplete.start_attempt(root_authorized=True)
        held = second.record_runtime_result("COMPLETED", changed_files=())

        self.assertEqual(held.last_runtime_state, "HOLD_RUNTIME")
        self.assertEqual(held.hold_reason, "repeated_no_progress")
        self.assertEqual(held.retry_budget_remaining, 0)
        with self.assertRaises(TaskRetryError):
            held.start_attempt(root_authorized=True)

    def test_transient_retry_is_same_scope_and_fallback_never_resets_budget(self) -> None:
        unstarted = TaskRetryState.new("20260901-unstarted")
        self.assertFalse(
            unstarted.fallback_allowed(
                root_authorized=True,
                reason="capability mismatch",
            )
        )

        started = TaskRetryState.new("20260901-transient", expected_write=False).start_attempt()
        timed_out = started.record_runtime_result("TIMED_OUT")

        self.assertTrue(timed_out.automatic_retry_allowed)
        retried = timed_out.start_attempt()
        self.assertEqual(retried.task_id, started.task_id)
        self.assertEqual(retried.attempt_count, 2)
        self.assertEqual(retried.retry_budget_remaining, 0)

        failed = TaskRetryState.new("20260901-fallback", expected_write=False).start_attempt()
        failed = failed.record_runtime_result("FAILED")
        self.assertTrue(
            failed.fallback_allowed(
                root_authorized=True,
                reason="runtime lacks required capability",
            )
        )
        fallback = failed.start_attempt(
            fallback=True,
            root_authorized=True,
            reason="runtime lacks required capability",
        )
        self.assertEqual(fallback.task_id, failed.task_id)
        self.assertEqual(fallback.attempt_count, 2)
        self.assertEqual(fallback.fallback_attempt_count, 1)
        self.assertEqual(fallback.retry_budget_remaining, 0)

    def test_serialization_rejects_budget_tampering(self) -> None:
        state = TaskRetryState.new("20260901-serialize", expected_write=True).start_attempt()
        payload = state.as_dict()
        restored = TaskRetryState.from_dict(payload)
        self.assertEqual(restored, state)

        payload["retry_budget_remaining"] = 99
        with self.assertRaises(TaskRetryError):
            TaskRetryState.from_dict(payload)

    def test_project_snapshot_accepts_task_retry_as_additive_state(self) -> None:
        retry = TaskRetryState.new("20260901-ledger", expected_write=False).start_attempt()
        snapshot = ProjectSnapshot(
            repository="owner/repo",
            project_state="in_progress",
            active_branch="main",
            active_pr=None,
            current_goal="record task state",
            blocker=(),
            last_verified_sha=None,
            last_pass_at=None,
            validation_cycle=0,
            manual_gate={"required": False, "status": "not_required", "reason": None},
            next_executable_work="continue",
            external_write_permission=False,
            observed_at="2026-09-01T00:00:00+00:00",
            task_retry=retry,
        )

        restored = ProjectSnapshot.from_dict(snapshot.to_dict())

        self.assertEqual(restored.task_retry, retry)
        self.assertNotIn("credential", snapshot.to_dict())

    def test_project_snapshot_rejects_invalid_task_retry(self) -> None:
        snapshot = {
            "schema_version": "0.1",
            "repository": "owner/repo",
            "project_state": "in_progress",
            "active_branch": None,
            "active_pr": None,
            "current_goal": "record task state",
            "blocker": [],
            "last_verified_sha": None,
            "last_pass_at": None,
            "validation_cycle": 0,
            "manual_gate": {"required": False, "status": "not_required", "reason": None},
            "next_executable_work": None,
            "external_write_permission": False,
            "observed_at": "2026-09-01T00:00:00+00:00",
            "history": [],
            "task_retry": {"policy_version": "1.0"},
        }
        with self.assertRaises(StateLedgerError):
            ProjectSnapshot.from_dict(snapshot)


if __name__ == "__main__":
    unittest.main()
