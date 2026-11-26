from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adapters.codex.route_agent import HookError, handle


ROOT = Path(__file__).resolve().parents[1]


class CodexAdapterTests(unittest.TestCase):
    def test_hook_routes_from_registry_and_persists_only_minimal_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "adapters.codex.route_agent.tempfile.gettempdir", return_value=directory
        ), patch.dict(
            os.environ,
            {"MULTI_AGENT_INDUSTRY_PERSONA_SETTINGS": str(Path(directory) / "missing-personas.json")},
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
        ), patch.dict(
            os.environ,
            {"MULTI_AGENT_INDUSTRY_PERSONA_SETTINGS": str(Path(directory) / "missing-personas.json")},
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

    def test_personal_presentation_settings_are_applied_without_persisting_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "adapters.codex.route_agent.tempfile.gettempdir", return_value=directory
        ):
            personal_path = Path(directory) / "persona-settings.json"
            private_style = "한국어로 따뜻하고 간결하게 응답합니다"
            personal_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "scope": "personal",
                        "profiles": {
                            "my_reviewer": {
                                "profile_id": "my_reviewer",
                                "locale": "ko-KR",
                                "style": private_style,
                                "tone_tags": ["warm", "concise"],
                            }
                        },
                        "default_profile_id": None,
                        "assignments": {"agents": {"reviewer": "my_reviewer"}, "roles": {}},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            if os.name == "posix":
                personal_path.chmod(0o600)

            with patch.dict(os.environ, {"MULTI_AGENT_INDUSTRY_PERSONA_SETTINGS": str(personal_path)}):
                result = handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "synthetic-persona-session",
                        "cwd": str(ROOT),
                        "prompt": "please @agent:reviewer",
                    }
                )

            context = result["hookSpecificOutput"]["additionalContext"]
            self.assertIn(private_style, context)
            self.assertIn("Tone tags: warm, concise", context)
            self.assertNotIn("my_reviewer", context)

            state_files = list((Path(directory) / "multi-agent-industry-route-state").glob("*.json"))
            self.assertEqual(len(state_files), 1)
            self.assertNotIn(private_style, state_files[0].read_text(encoding="utf-8"))

    def test_invalid_personal_settings_fail_closed_without_echoing_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "adapters.codex.route_agent.tempfile.gettempdir", return_value=directory
        ):
            personal_path = Path(directory) / "persona-settings.json"
            secret_like_value = "private invalid value that must not be echoed"
            personal_path.write_text(secret_like_value, encoding="utf-8")
            if os.name == "posix":
                personal_path.chmod(0o600)

            with patch.dict(os.environ, {"MULTI_AGENT_INDUSTRY_PERSONA_SETTINGS": str(personal_path)}):
                with self.assertRaises(HookError) as error:
                    handle(
                        {
                            "hook_event_name": "UserPromptSubmit",
                            "session_id": "synthetic-invalid-persona-session",
                            "cwd": str(ROOT),
                            "prompt": "please @agent:reviewer",
                        }
                    )

            self.assertNotIn(secret_like_value, str(error.exception))


if __name__ == "__main__":
    unittest.main()
