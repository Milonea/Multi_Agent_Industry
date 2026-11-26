from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adapters.codex.route_agent import handle


ROOT = Path(__file__).resolve().parents[1]


class CodexAdapterTests(unittest.TestCase):
    def test_hook_routes_from_registry_and_persists_only_minimal_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "adapters.codex.route_agent.tempfile.gettempdir", return_value=directory
        ):
            prompt = "synthetic prompt text @agent:reviewer"
            result = handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "synthetic-session-001",
                    "cwd": str(ROOT),
                    "prompt": prompt,
                }
            )

            self.assertIn("hookSpecificOutput", result)
            context = result["hookSpecificOutput"]["additionalContext"]
            self.assertIn("active_agent_id=reviewer", context)
            self.assertIn("runtime_binding=codex/reviewer", context)
            self.assertNotIn(prompt, context)

            state_files = list(Path(directory).rglob("*.json"))
            self.assertEqual(len(state_files), 1)
            serialized = state_files[0].read_text(encoding="utf-8")
            self.assertNotIn(prompt, serialized)
            state = json.loads(serialized)
            self.assertEqual(
                set(state),
                {"version", "active_agent_id", "active_project_id", "supporting_agent_ids"},
            )
            self.assertEqual(state["active_agent_id"], "reviewer")
            if os.name == "posix":
                self.assertEqual(state_files[0].stat().st_mode & 0o777, 0o600)

            continued = handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "synthetic-session-001",
                    "cwd": str(ROOT.parent),
                    "prompt": "continue in another working directory",
                }
            )
            continued_context = continued["hookSpecificOutput"]["additionalContext"]
            self.assertIn("active_agent_id=reviewer", continued_context)
            self.assertIn("route_source=continuity", continued_context)
            self.assertEqual(len(list(Path(directory).rglob("*.json"))), 1)

    def test_unregistered_agent_is_blocked_without_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "adapters.codex.route_agent.tempfile.gettempdir", return_value=directory
        ):
            result = handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "synthetic-session-002",
                    "cwd": str(ROOT),
                    "prompt": "please @agent:not_registered",
                }
            )
            self.assertEqual(result["decision"], "block")
            self.assertEqual(list(Path(directory).rglob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
