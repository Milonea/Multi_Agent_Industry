from __future__ import annotations

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adapters.codex.route_agent import HookError, handle, main


ROOT = Path(__file__).resolve().parents[1]


class CodexAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        # Never read the user's real private project overlay during tests.
        overlay_dir = tempfile.TemporaryDirectory()
        self.addCleanup(overlay_dir.cleanup)
        patcher = patch.dict(
            os.environ,
            {"MULTI_AGENT_INDUSTRY_PROJECT_OVERLAY": str(Path(overlay_dir.name) / "missing-overlay.json")},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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
            self.assertNotIn("Presentation policy", context)
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
                    "cwd": str(ROOT),
                    "prompt": "continue in the same working directory",
                }
            )
            continued_context = continued["hookSpecificOutput"]["additionalContext"]
            self.assertIn("active_agent_id=reviewer", continued_context)
            self.assertIn("route_source=continuity", continued_context)
            self.assertEqual(len(list(Path(directory).rglob("*.json"))), 1)

            elsewhere = handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "synthetic-session-001",
                    "cwd": str(ROOT.parent),
                    "prompt": "continue in another working directory",
                }
            )
            elsewhere_context = elsewhere["hookSpecificOutput"]["additionalContext"]
            self.assertIn("active_agent_id=coordinator", elsewhere_context)
            self.assertIn("route_source=coordinator", elsewhere_context)
            state_files = list(Path(directory).rglob("*.json"))
            self.assertEqual(len(state_files), 2)
            for state_file in state_files:
                self.assertNotIn("synthetic-session-001", state_file.name)
                self.assertNotIn(str(ROOT.parent), state_file.read_text(encoding="utf-8"))

    def test_working_directory_selects_registered_project_and_token_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "adapters.codex.route_agent.tempfile.gettempdir", return_value=directory
        ), patch.dict(
            os.environ,
            {"MULTI_AGENT_INDUSTRY_PERSONA_SETTINGS": str(Path(directory) / "missing-personas.json")},
        ):
            workspace = Path(directory) / "workspaces"
            alpha = workspace / "alpha"
            nested = alpha / "nested-beta"
            (nested / "src").mkdir(parents=True)
            registry_data = json.loads((ROOT / ".agents" / "registry.json").read_text(encoding="utf-8"))
            registry_data["project_routes"] = {
                "sample-alpha": {"agent_id": "implementer", "path": str(alpha)},
                "sample-beta": {"agent_id": "reviewer", "path": str(nested)},
                "sample-gamma": "validator",
            }
            registry_file = Path(directory) / "registry.json"
            registry_file.write_text(json.dumps(registry_data), encoding="utf-8")

            def run(session: str, cwd: Path, prompt: str) -> str:
                with patch(
                    "adapters.codex.route_agent._registry_path", return_value=registry_file
                ):
                    result = handle(
                        {
                            "hook_event_name": "UserPromptSubmit",
                            "session_id": session,
                            "cwd": str(cwd),
                            "prompt": prompt,
                        }
                    )
                return result["hookSpecificOutput"]["additionalContext"]

            context = run("synthetic-cwd-1", alpha, "synthetic first turn")
            self.assertIn("active_agent_id=implementer", context)
            self.assertIn("project_id=sample-alpha", context)
            self.assertIn("route_source=working_directory", context)

            deeper = run("synthetic-cwd-2", nested / "src", "synthetic first turn")
            self.assertIn("active_agent_id=reviewer", deeper)
            self.assertIn("project_id=sample-beta", deeper)

            explicit = run("synthetic-cwd-3", alpha, "switch project:sample-gamma")
            self.assertIn("active_agent_id=validator", explicit)
            self.assertIn("project_id=sample-gamma", explicit)
            self.assertIn("route_source=project", explicit)
            follow_up = run("synthetic-cwd-3", alpha, "synthetic follow-up")
            self.assertIn("project_id=sample-gamma", follow_up)
            self.assertIn("route_source=continuity", follow_up)

            outside = run("synthetic-cwd-4", workspace, "synthetic first turn")
            self.assertIn("project_id=none", outside)
            self.assertIn("route_source=coordinator", outside)

            for state_file in Path(directory).rglob("*.json"):
                if state_file == registry_file:
                    continue
                serialized = state_file.read_text(encoding="utf-8")
                self.assertNotIn("synthetic", serialized)
                self.assertNotIn(str(workspace), serialized)

    def test_relative_or_missing_working_directory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "adapters.codex.route_agent.tempfile.gettempdir", return_value=directory
        ), patch.dict(
            os.environ,
            {"MULTI_AGENT_INDUSTRY_PERSONA_SETTINGS": str(Path(directory) / "missing-personas.json")},
        ):
            for cwd in ("relative/path", "", None):
                with self.assertRaises(HookError):
                    handle(
                        {
                            "hook_event_name": "UserPromptSubmit",
                            "session_id": "synthetic-session-cwd",
                            "cwd": cwd,
                            "prompt": "please @agent:reviewer",
                        }
                    )
            self.assertEqual(list(Path(directory).rglob("*.json")), [])

    def test_unregistered_agent_warns_and_persists_only_the_fallback_route(self) -> None:
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
            context = result["hookSpecificOutput"]["additionalContext"]
            self.assertIn("active_agent_id=coordinator", context)
            self.assertIn("Routing warning:", context)
            self.assertNotIn("not_registered", context)
            state_files = list(Path(directory).rglob("*.json"))
            self.assertEqual(len(state_files), 1)
            self.assertEqual(json.loads(state_files[0].read_text())["active_agent_id"], "coordinator")

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
                        "presentation_policy": {
                            "rules": ["개인 호칭 규칙을 따릅니다"],
                            "presence": {"casual": "high"},
                        },
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
            self.assertIn("Presentation policy (wording only", context)
            self.assertIn("개인 호칭 규칙을 따릅니다", context)
            self.assertIn("casual=high", context)
            self.assertIn("security=low", context)

            state_files = list((Path(directory) / "multi-agent-industry-route-state").glob("*.json"))
            self.assertEqual(len(state_files), 1)
            self.assertNotIn(private_style, state_files[0].read_text(encoding="utf-8"))
            self.assertNotIn("개인 호칭 규칙을 따릅니다", state_files[0].read_text(encoding="utf-8"))

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


class CodexRoutingWarningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.registry_data = json.loads(
            (ROOT / ".agents" / "registry.json").read_text(encoding="utf-8")
        )
        self.registry_path = self.base / "registry.json"
        self.state_base = self.base / "state"
        self.state_base.mkdir()
        self.write_registry(self.registry_data)
        patchers = (
            patch("adapters.codex.route_agent._registry_path", return_value=self.registry_path),
            patch(
                "adapters.codex.route_agent.tempfile.gettempdir",
                return_value=str(self.state_base),
            ),
            patch.dict(
                os.environ,
                {
                    "MULTI_AGENT_INDUSTRY_PERSONA_SETTINGS": str(
                        self.base / "config" / "missing-personas.json"
                    ),
                    "MULTI_AGENT_INDUSTRY_PROJECT_OVERLAY": str(
                        self.base / "config" / "missing-overlay.json"
                    ),
                },
            ),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_registry(self, value: dict) -> None:
        self.registry_path.write_text(json.dumps(value), encoding="utf-8")

    def run_hook(
        self,
        prompt: str,
        *,
        session: str,
        cwd: Path = ROOT,
    ) -> dict:
        return handle(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session,
                "cwd": str(cwd),
                "prompt": prompt,
            }
        )

    def assert_warning_route(
        self,
        result: dict,
        *,
        agent_id: str,
        warning: str,
        private_text: str | None = None,
    ) -> str:
        self.assertNotIn("decision", result)
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"active_agent_id={agent_id}", context)
        self.assertIn(warning, context)
        if private_text is not None:
            self.assertNotIn(private_text, context)
        return context

    def test_invalid_identifier_tokens_warn_and_route_as_absent(self) -> None:
        cases = (
            (
                "@agent:<private-agent-id>",
                "an agent selection token was ignored because its value is not a valid identifier",
                "<private-agent-id>",
            ),
            (
                "project:<private-project-id>",
                "a project selection token was ignored because its value is not a valid identifier",
                "<private-project-id>",
            ),
            (
                "role:<private-role-id>",
                "a role selection token was ignored because its value is not a valid identifier",
                "<private-role-id>",
            ),
            (
                "support:agent:<private-support-id>",
                "a supporting agent token was ignored because its value is not a valid identifier",
                "<private-support-id>",
            ),
        )
        for index, (token, warning, private_text) in enumerate(cases):
            prompt = f"synthetic private prompt {token}"
            with self.subTest(token_kind=index):
                context = self.assert_warning_route(
                    self.run_hook(prompt, session=f"invalid-token-{index}"),
                    agent_id="coordinator",
                    warning="Routing warning: " + warning,
                    private_text=private_text,
                )
                self.assertNotIn(prompt, context)
                self.assertIn("supporting_agent_ids=none", context)
                self.assertEqual(context.count("Routing warning:"), 1)

    def test_pilot_placeholder_project_token_regression_warns_and_continues(self) -> None:
        prompt = (
            "Task spec example: select the target with project:<project-id> before dispatching "
            "the worker."
        )
        context = self.assert_warning_route(
            self.run_hook(prompt, session="pilot-placeholder-regression"),
            agent_id="coordinator",
            warning=(
                "Routing warning: a project selection token was ignored because its value is not "
                "a valid identifier."
            ),
            private_text="<project-id>",
        )
        self.assertNotIn(prompt, context)

    def test_multiple_distinct_values_ignore_the_whole_selection_kind(self) -> None:
        configured = copy.deepcopy(self.registry_data)
        configured["project_routes"] = {
            "sample-first": "reviewer",
            "sample-second": "validator",
        }
        self.write_registry(configured)
        cases = (
            (
                "@agent:reviewer @agent:validator",
                "agent selection tokens were ignored because they specify multiple distinct values",
            ),
            (
                "role:reviewer role:validator",
                "role selection tokens were ignored because they specify multiple distinct values",
            ),
            (
                "project:sample-first project:sample-second",
                "project selection tokens were ignored because they specify multiple distinct values",
            ),
        )
        for index, (prompt, warning) in enumerate(cases):
            with self.subTest(token_kind=index):
                context = self.assert_warning_route(
                    self.run_hook(prompt, session=f"multiple-values-{index}"),
                    agent_id="coordinator",
                    warning="Routing warning: " + warning,
                )
                self.assertIn("route_source=coordinator", context)
                self.assertNotIn(prompt, context)

    def test_unregistered_agent_warns_without_echoing_the_id(self) -> None:
        private_id = "private_missing_agent"
        self.assert_warning_route(
            self.run_hook(f"please @agent:{private_id}", session="unregistered-agent"),
            agent_id="coordinator",
            warning=(
                "Routing warning: an agent selection token was ignored because the requested agent "
                "is unavailable."
            ),
            private_text=private_id,
        )

    def test_disabled_agent_binding_warns_and_routes_as_absent(self) -> None:
        configured = copy.deepcopy(self.registry_data)
        reviewer = next(agent for agent in configured["agents"] if agent["agent_id"] == "reviewer")
        reviewer["runtime_binding"]["enabled"] = False
        self.write_registry(configured)
        self.assert_warning_route(
            self.run_hook("please @agent:reviewer", session="disabled-agent"),
            agent_id="coordinator",
            warning=(
                "Routing warning: an agent selection token was ignored because the requested agent "
                "is unavailable."
            ),
        )

    def test_incomplete_agent_binding_warns_and_routes_as_absent(self) -> None:
        configured = copy.deepcopy(self.registry_data)
        reviewer = next(agent for agent in configured["agents"] if agent["agent_id"] == "reviewer")
        reviewer["runtime_binding"] = {"provider": None, "agent_name": None, "enabled": False}
        self.write_registry(configured)
        self.assert_warning_route(
            self.run_hook("please @agent:reviewer", session="incomplete-agent"),
            agent_id="coordinator",
            warning=(
                "Routing warning: an agent selection token was ignored because the requested agent "
                "is unavailable."
            ),
        )

    def test_unregistered_project_warns_without_echoing_the_id(self) -> None:
        private_id = "private_missing_project"
        self.assert_warning_route(
            self.run_hook(f"please project:{private_id}", session="unregistered-project"),
            agent_id="coordinator",
            warning=(
                "Routing warning: a project selection token was ignored because the requested "
                "project is unavailable."
            ),
            private_text=private_id,
        )

    def test_explicit_inventory_only_project_warns_without_echoing_the_id(self) -> None:
        private_id = "private_inventory_project"
        overlay_path = self.base / "config" / "project-overlay.json"
        overlay_path.parent.mkdir(mode=0o700)
        overlay_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "scope": "personal",
                    "projects": {
                        private_id: {
                            "owner_agent_id": "implementer",
                            "status": "inventory_only",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        if os.name == "posix":
            overlay_path.chmod(0o600)
        with patch.dict(
            os.environ,
            {"MULTI_AGENT_INDUSTRY_PROJECT_OVERLAY": str(overlay_path)},
        ):
            self.assert_warning_route(
                self.run_hook(f"please project:{private_id}", session="inventory-project"),
                agent_id="coordinator",
                warning=(
                    "Routing warning: a project selection token was ignored because the requested "
                    "project is unavailable."
                ),
                private_text=private_id,
            )

    def test_role_with_no_enabled_agent_warns_and_routes_as_absent(self) -> None:
        configured = copy.deepcopy(self.registry_data)
        reviewer = next(agent for agent in configured["agents"] if agent["agent_id"] == "reviewer")
        reviewer["runtime_binding"]["enabled"] = False
        self.write_registry(configured)
        self.assert_warning_route(
            self.run_hook("please role:reviewer", session="unavailable-role"),
            agent_id="coordinator",
            warning=(
                "Routing warning: a role selection token was ignored because no enabled agent is "
                "available for the role."
            ),
        )

    def test_role_with_multiple_enabled_agents_warns_and_routes_as_absent(self) -> None:
        configured = copy.deepcopy(self.registry_data)
        extra = copy.deepcopy(
            next(agent for agent in configured["agents"] if agent["agent_id"] == "reviewer")
        )
        extra["agent_id"] = "reviewer_backup"
        extra["runtime_binding"]["agent_name"] = "reviewer_backup"
        configured["agents"].append(extra)
        self.write_registry(configured)
        self.assert_warning_route(
            self.run_hook("please role:reviewer", session="ambiguous-role"),
            agent_id="coordinator",
            warning=(
                "Routing warning: a role selection token was ignored because the role maps to "
                "multiple enabled agents."
            ),
        )

    def test_invalid_and_unavailable_supporting_agents_are_dropped_individually(self) -> None:
        private_id = "private_missing_support"
        prompt = (
            "support:agent:reviewer support:agent:<private-support> "
            f"support:agent:{private_id} support:agent:validator"
        )
        context = self.assert_warning_route(
            self.run_hook(prompt, session="bad-supports"),
            agent_id="coordinator",
            warning=(
                "Routing warning: a supporting agent token was ignored because its value is not a "
                "valid identifier."
            ),
            private_text=private_id,
        )
        self.assertIn(
            "Routing warning: a supporting agent token was ignored because the requested agent is "
            "unavailable.",
            context,
        )
        self.assertIn("supporting_agent_ids=reviewer,validator", context)
        self.assertNotIn("<private-support>", context)

    def test_duplicate_supporting_agent_drops_only_the_duplicate_entry(self) -> None:
        context = self.assert_warning_route(
            self.run_hook(
                "support:agent:reviewer support:agent:reviewer",
                session="duplicate-support",
            ),
            agent_id="coordinator",
            warning=(
                "Routing warning: a supporting agent token was ignored because it duplicates "
                "another supporting agent token."
            ),
        )
        self.assertIn("supporting_agent_ids=reviewer", context)

    def test_active_agent_is_dropped_from_supporting_agents_with_a_warning(self) -> None:
        context = self.assert_warning_route(
            self.run_hook(
                "@agent:reviewer support:agent:reviewer",
                session="active-support",
            ),
            agent_id="reviewer",
            warning=(
                "Routing warning: a supporting agent token was ignored because the active agent "
                "cannot also be supporting."
            ),
        )
        self.assertIn("supporting_agent_ids=none", context)

    def test_bad_token_preserves_the_same_authority_as_the_prompt_without_it(self) -> None:
        baseline = self.run_hook("role:implementer", session="authority-baseline")
        with_bad_token = self.run_hook(
            "@agent:<private-placeholder> role:implementer",
            session="authority-with-bad-token",
        )
        baseline_context = baseline["hookSpecificOutput"]["additionalContext"]
        warned_context = with_bad_token["hookSpecificOutput"]["additionalContext"]
        for field in (
            "active_agent_id=implementer",
            "declared_permissions=read_workspace,write_workspace",
            "project_id=none",
            "supporting_agent_ids=none",
            "route_source=role",
        ):
            self.assertIn(field, baseline_context)
            self.assertIn(field, warned_context)
        self.assertIn("Routing warning:", warned_context)
        self.assertNotIn("<private-placeholder>", warned_context)

    def test_warning_text_and_private_prompt_are_not_persisted(self) -> None:
        private_value = "<private-project-value>"
        prompt = f"private prompt body project:{private_value}"
        result = self.run_hook(prompt, session="warning-state-privacy")
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Routing warning:", context)
        self.assertNotIn(prompt, context)
        state_files = list(self.state_base.rglob("*.json"))
        self.assertEqual(len(state_files), 1)
        serialized = state_files[0].read_text(encoding="utf-8")
        self.assertEqual(
            set(json.loads(serialized)),
            {"version", "active_agent_id", "active_project_id", "supporting_agent_ids"},
        )
        self.assertNotIn(prompt, serialized)
        self.assertNotIn(private_value, serialized)
        self.assertNotIn("Routing warning", serialized)

    def test_invalid_registry_still_fails_closed_without_echoing_contents(self) -> None:
        private_text = "private-invalid-registry-value"
        self.registry_path.write_text("{broken " + private_text, encoding="utf-8")
        with self.assertRaisesRegex(HookError, "registry could not be validated") as error:
            self.run_hook("synthetic prompt", session="invalid-registry")
        self.assertNotIn(private_text, str(error.exception))
        self.assertEqual(list(self.state_base.rglob("*.json")), [])

    def test_malformed_hook_inputs_are_emitted_as_blocks(self) -> None:
        cases = (
            b"not-json",
            b"[]",
            json.dumps({"hook_event_name": "Other"}).encode(),
            json.dumps({"hook_event_name": "UserPromptSubmit"}).encode(),
            json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "synthetic",
                    "cwd": str(ROOT),
                }
            ).encode(),
            json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "synthetic",
                    "session_id": "missing-cwd",
                }
            ).encode(),
        )
        for index, raw in enumerate(cases):
            stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
            stdout = io.StringIO()
            with self.subTest(case=index), patch("sys.stdin", stdin), contextlib.redirect_stdout(
                stdout
            ):
                self.assertEqual(main(), 0)
            output = json.loads(stdout.getvalue())
            self.assertEqual(output["decision"], "block")
            self.assertTrue(output["reason"])

    def test_ambiguous_working_directory_still_blocks_next_to_bad_tokens(self) -> None:
        from agent_runtime.registry import AgentRegistry, RegistryValidationError

        with patch.object(
            AgentRegistry,
            "project_for_working_directory",
            side_effect=RegistryValidationError("synthetic ambiguity"),
        ):
            with self.assertRaisesRegex(HookError, "ambiguous or invalid"):
                self.run_hook("project:<placeholder> @agent:<placeholder>", session="ambiguous-cwd")
        self.assertEqual(list(self.state_base.rglob("*.json")), [])

    def test_uppercase_tokens_are_normalized_as_before(self) -> None:
        context = self.run_hook("please @agent:Reviewer", session="uppercase-agent")[
            "hookSpecificOutput"
        ]["additionalContext"]
        self.assertIn("active_agent_id=reviewer", context)
        self.assertNotIn("Routing warning:", context)

    def test_valid_prompt_has_no_routing_warning(self) -> None:
        context = self.run_hook(
            "@agent:reviewer support:agent:validator", session="clean-prompt"
        )["hookSpecificOutput"]["additionalContext"]
        self.assertIn("active_agent_id=reviewer", context)
        self.assertIn("supporting_agent_ids=validator", context)
        self.assertNotIn("Routing warning", context)

    @unittest.skipUnless(os.name == "posix", "POSIX route-state permissions")
    def test_unsafe_route_state_directory_and_file_still_fail_closed(self) -> None:
        state_root = self.state_base / "multi-agent-industry-route-state"
        state_root.mkdir(mode=0o755)
        state_root.chmod(0o755)
        with self.assertRaisesRegex(HookError, "directory permissions"):
            self.run_hook("synthetic", session="unsafe-state-directory")

        state_root.chmod(0o700)
        self.run_hook("synthetic", session="unsafe-state-file")
        state_file = next(state_root.glob("*.json"))
        state_file.chmod(0o644)
        with self.assertRaisesRegex(HookError, "file permissions"):
            self.run_hook("synthetic", session="unsafe-state-file")


if __name__ == "__main__":
    unittest.main()
