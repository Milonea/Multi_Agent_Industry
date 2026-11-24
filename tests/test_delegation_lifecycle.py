from __future__ import annotations

import unittest

from portfolio_control_plane import (
    DEFAULT_SILENCE_POLL_INTERVAL_SECONDS,
    DEFAULT_WATCHDOG_TIMEOUT_SECONDS,
    DelegationLifecycle,
    DelegationLifecycleError,
    advance_after_wait,
    validate_lifecycle_snapshot,
)


class DelegationLifecycleTests(unittest.TestCase):
    def test_malformed_action_and_child_id_are_rejected_without_type_errors(self) -> None:
        with self.assertRaises(DelegationLifecycleError):
            DelegationLifecycle("task", "runtime", next_action=[])  # type: ignore[arg-type]
        with self.assertRaises(DelegationLifecycleError):
            self._working().observe("child_started", child_thread_id=[])  # type: ignore[arg-type]

    def _working(self) -> DelegationLifecycle:
        return (
            DelegationLifecycle("20260830-ayumu", "iwabitsu_ayumu")
            .observe("child_started", child_thread_id="child-1")
            .observe("enter_received")
        )

    def test_silence_keeps_same_child_pending_and_not_hold(self) -> None:
        lifecycle = self._working().observe("silence_observed")

        self.assertEqual(lifecycle.state, "working")
        self.assertEqual(lifecycle.gate_status, "pending")
        self.assertFalse(lifecycle.is_validation_hold)
        self.assertEqual(lifecycle.user_visible_status, "delegated")
        self.assertEqual(lifecycle.next_action, "poll_same_child")
        self.assertEqual(lifecycle.child_thread_id, "child-1")

    def test_active_task_cannot_switch_to_a_duplicate_child(self) -> None:
        with self.assertRaises(DelegationLifecycleError):
            self._working().observe("child_started", child_thread_id="child-2")

    def test_final_report_is_requested_once_after_terminal_child(self) -> None:
        lifecycle = self._working().observe("child_completed")
        lifecycle = lifecycle.observe("final_report_requested")

        self.assertEqual(lifecycle.state, "awaiting_result")
        self.assertEqual(lifecycle.final_report_requests, 1)
        with self.assertRaises(DelegationLifecycleError):
            lifecycle.observe("final_report_requested")

        missing = lifecycle.observe("result_missing")
        self.assertEqual(missing.state, "result_missing")
        self.assertEqual(missing.user_visible_status, "blocked")
        self.assertEqual(missing.gate_status, "blocked")
        self.assertFalse(missing.is_validation_hold)
        self.assertFalse(missing.promotion_allowed)

    def test_active_child_uses_one_interrupt_then_one_final_report_request(self) -> None:
        lifecycle = self._working().observe("silence_observed")
        lifecycle = lifecycle.observe("watchdog_expired")
        self.assertEqual(lifecycle.state, "working")
        self.assertEqual(lifecycle.gate_status, "pending")
        self.assertEqual(lifecycle.next_action, "await_interrupt_result")
        lifecycle = lifecycle.observe("interrupt_requested")
        self.assertEqual(lifecycle.interrupt_requests, 1)
        with self.assertRaises(DelegationLifecycleError):
            lifecycle.observe("silence_observed").observe("interrupt_requested")
        lifecycle = lifecycle.observe("final_report_requested")

        self.assertEqual(lifecycle.state, "awaiting_result")
        self.assertEqual(lifecycle.final_report_requests, 1)
        self.assertEqual(lifecycle.next_action, "read_same_child_result")

        with self.assertRaises(DelegationLifecycleError):
            lifecycle.observe("final_report_requested")

    def test_wait_timeout_is_polling_until_the_watchdog_boundary(self) -> None:
        lifecycle = advance_after_wait(
            self._working(),
            elapsed_seconds=DEFAULT_SILENCE_POLL_INTERVAL_SECONDS,
            timed_out=True,
        )
        self.assertEqual(lifecycle.last_event, "silence_observed")
        self.assertEqual(lifecycle.next_action, "poll_same_child")
        self.assertEqual(lifecycle.gate_status, "pending")

        lifecycle = advance_after_wait(lifecycle, elapsed_seconds=240, timed_out=True)
        self.assertEqual(lifecycle.last_event, "silence_observed")
        self.assertEqual(lifecycle.next_action, "poll_same_child")
        self.assertEqual(lifecycle.gate_status, "pending")

        lifecycle = advance_after_wait(
            lifecycle,
            elapsed_seconds=DEFAULT_WATCHDOG_TIMEOUT_SECONDS,
            timed_out=True,
        )
        self.assertEqual(lifecycle.last_event, "watchdog_expired")
        self.assertEqual(lifecycle.next_action, "await_interrupt_result")
        self.assertEqual(lifecycle.gate_status, "pending")

    def test_active_child_cannot_receive_final_report_request_without_interrupt(self) -> None:
        with self.assertRaises(DelegationLifecycleError):
            self._working().observe("final_report_requested")

    def test_validation_hold_requires_a_received_result(self) -> None:
        with self.assertRaises(DelegationLifecycleError):
            self._working().observe("validation_held")

        held = (
            self._working()
            .observe("child_completed")
            .observe("result_received")
            .observe("validation_held")
        )
        self.assertEqual(held.gate_status, "hold")
        self.assertTrue(held.is_validation_hold)
        self.assertEqual(held.user_visible_status, "delegated")

    def test_pass_is_the_only_state_that_allows_promotion(self) -> None:
        completed = (
            self._working()
            .observe("child_completed")
            .observe("result_received")
        )
        self.assertFalse(completed.promotion_allowed)
        passed = completed.observe("validation_passed")
        self.assertEqual(passed.gate_status, "pass")
        self.assertTrue(passed.promotion_allowed)

    def test_snapshot_round_trip_preserves_pending_state(self) -> None:
        snapshot = self._working().observe("silence_observed").to_dict()

        restored = validate_lifecycle_snapshot(snapshot)
        self.assertEqual(restored["state"], "working")
        self.assertEqual(restored["gate_status"], "pending")
        self.assertEqual(restored["user_visible_status"], "delegated")
        self.assertEqual(restored["child_thread_id"], "child-1")
        self.assertEqual(restored["interrupt_requests"], 0)

    def test_snapshot_validation_is_strict(self) -> None:
        snapshot = self._working().to_dict()
        snapshot["user_visible_status"] = "blocked"
        with self.assertRaises(DelegationLifecycleError):
            validate_lifecycle_snapshot(snapshot)

        snapshot = self._working().to_dict()
        snapshot["attempt"] = True
        with self.assertRaises(DelegationLifecycleError):
            validate_lifecycle_snapshot(snapshot)


if __name__ == "__main__":
    unittest.main()
