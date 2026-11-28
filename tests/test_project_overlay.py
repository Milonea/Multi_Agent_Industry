from __future__ import annotations

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from adapters.codex.route_agent import HookError, handle
from agent_runtime.cli import main as cli_main
from agent_runtime.persona_layers import PersonaLayers, PersonaSettings, PersonaSettingsError
from agent_runtime.project_overlay import (
    PROJECT_OVERLAY_ENV,
    ProjectOverlay,
    ProjectOverlayError,
    apply_project_overlay,
    default_project_overlay_path,
    initialize_project_overlay,
    load_project_overlay,
)
from agent_runtime.registry import AgentRegistry, RegistryValidationError
from agent_runtime.routing import ConversationState, resolve_route


ROOT = Path(__file__).resolve().parents[1]
PERSONA_ENV = "MULTI_AGENT_INDUSTRY_PERSONA_SETTINGS"


def registry_data() -> dict:
    return json.loads((ROOT / ".agents" / "registry.json").read_text(encoding="utf-8"))


def overlay(projects: dict) -> dict:
    return {"schema_version": "1.0", "scope": "personal", "projects": projects}


def profile(profile_id: str, style: str) -> dict:
    return {"profile_id": profile_id, "locale": "ko-KR", "style": style, "tone_tags": ["concise"]}


def write_private(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)
        path.parent.chmod(0o700)
    return path


class ProjectOverlayLoadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = AgentRegistry.from_dict(registry_data())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def test_missing_file_is_an_empty_overlay(self) -> None:
        loaded = load_project_overlay(self.registry, self.base / "absent.json", repo_root=ROOT)
        self.assertEqual(dict(loaded.projects), {})
        self.assertIs(apply_project_overlay(self.registry, loaded, base_dir=ROOT), self.registry)

    def test_malformed_file_fails_closed_without_echoing_contents(self) -> None:
        path = self.base / "project-overlay.json"
        secret = "private-project-name-that-must-not-leak"
        path.write_text("{not json " + secret, encoding="utf-8")
        path.chmod(0o600)
        with self.assertRaises(ProjectOverlayError) as error:
            load_project_overlay(self.registry, path, repo_root=ROOT)
        self.assertNotIn(secret, str(error.exception))

        write_private(path, {"schema_version": "1.0", "scope": "personal", "projects": [secret]})
        with self.assertRaises(ProjectOverlayError) as error:
            load_project_overlay(self.registry, path, repo_root=ROOT)
        self.assertNotIn(secret, str(error.exception))

    @unittest.skipUnless(os.name == "posix", "POSIX permission modes and symlinks")
    def test_symlink_and_broad_permissions_are_rejected(self) -> None:
        real = write_private(self.base / "real" / "project-overlay.json", overlay({}))
        link = self.base / "real" / "link.json"
        link.symlink_to(real)
        with self.assertRaisesRegex(ProjectOverlayError, "symlink"):
            load_project_overlay(self.registry, link, repo_root=ROOT)

        real.chmod(0o644)
        with self.assertRaisesRegex(ProjectOverlayError, "owner-only"):
            load_project_overlay(self.registry, real, repo_root=ROOT)

        real.chmod(0o600)
        real.parent.chmod(0o755)
        with self.assertRaisesRegex(ProjectOverlayError, "directory permissions"):
            load_project_overlay(self.registry, real, repo_root=ROOT)

    def test_file_inside_repository_is_rejected(self) -> None:
        with self.assertRaisesRegex(ProjectOverlayError, "outside the repository"):
            load_project_overlay(
                self.registry, ROOT / ".agents" / "project-overlay.personal.json", repo_root=ROOT
            )

    def test_env_override_must_be_absolute_and_is_used_by_default(self) -> None:
        target = write_private(
            self.base / "cfg" / "custom-overlay.json",
            overlay({"sample-alpha": {"owner_agent_id": "implementer", "status": "active"}}),
        )
        with patch.dict(os.environ, {PROJECT_OVERLAY_ENV: str(target)}):
            self.assertEqual(default_project_overlay_path(), target)
            loaded = load_project_overlay(self.registry, repo_root=ROOT)
        self.assertEqual(set(loaded.projects), {"sample-alpha"})
        with patch.dict(os.environ, {PROJECT_OVERLAY_ENV: "relative/overlay.json"}):
            with self.assertRaisesRegex(ProjectOverlayError, "absolute"):
                default_project_overlay_path()

    def test_default_path_shares_the_persona_settings_directory(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(PROJECT_OVERLAY_ENV, None)
            os.environ.pop(PERSONA_ENV, None)
            from agent_runtime.persona_layers import default_personal_settings_path

            self.assertEqual(default_project_overlay_path().name, "project-overlay.json")
            self.assertEqual(default_project_overlay_path().parent, default_personal_settings_path().parent)

    @unittest.skipUnless(os.name == "posix", "POSIX permission modes are platform-specific")
    def test_initializer_creates_private_empty_file_and_never_overwrites(self) -> None:
        path = self.base / "settings" / "project-overlay.json"
        created = initialize_project_overlay(path, repo_root=ROOT)
        self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(created.parent.stat().st_mode), 0o700)
        self.assertEqual(dict(load_project_overlay(self.registry, created, repo_root=ROOT).projects), {})
        with self.assertRaisesRegex(ProjectOverlayError, "already exists"):
            initialize_project_overlay(path, repo_root=ROOT)
        with self.assertRaisesRegex(ProjectOverlayError, "outside the repository"):
            initialize_project_overlay(ROOT / ".agents" / "project-overlay.personal.json", repo_root=ROOT)


class ProjectOverlayValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = AgentRegistry.from_dict(registry_data())

    def parse(self, projects: dict) -> ProjectOverlay:
        return ProjectOverlay.from_dict(overlay(projects), registry=self.registry)

    def test_example_file_is_valid_and_synthetic(self) -> None:
        raw = json.loads((ROOT / ".agents" / "project-overlay.personal.example.json").read_text(encoding="utf-8"))
        parsed = ProjectOverlay.from_dict(raw, registry=self.registry)
        self.assertTrue(all(project_id.startswith("sample-") for project_id in parsed.projects))
        self.assertEqual(parsed.counts(), {"active": 1, "hold": 1, "inventory_only": 1})

    def test_unknown_agents_and_invalid_hold_configuration_are_rejected(self) -> None:
        cases = [
            {"owner_agent_id": "not_registered", "status": "active"},
            {"owner_agent_id": "implementer", "status": "paused"},
            {"owner_agent_id": "implementer", "status": "hold"},
            {"owner_agent_id": "implementer", "status": "hold", "hold_agent_id": "not_registered"},
            {"owner_agent_id": "implementer", "status": "hold", "hold_agent_id": "implementer"},
            {"owner_agent_id": "implementer", "status": "hold", "hold_agent_id": "documentation_specialist"},
            {"owner_agent_id": "implementer", "status": "active", "hold_agent_id": "researcher"},
            {"status": "active"},
            {"owner_agent_id": "implementer", "status": "active", "persona_assignments": {"agents": {"ghost": "p1"}}},
            {"owner_agent_id": "implementer", "status": "active", "persona_assignments": {"roles": {"ghost": "p1"}}},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ProjectOverlayError):
                self.parse({"sample-alpha": case})

    def test_field_formats_are_strict(self) -> None:
        base = {"owner_agent_id": "implementer", "status": "active"}
        invalid = [
            {"path": "relative/path"},
            {"path": "~/sample"},
            {"path": "/synthetic/../escape"},
            {"worktree_roots": ["relative"]},
            {"repository": "no-slash"},
            {"repository": "a/b/c"},
            {"constraints": ["line\nbreak"]},
            {"constraints": ["same", "same"]},
            {"constraints": ["x" * 301]},
            {"validation": ["python -m unittest"]},
            {"validation": [[]]},
            {"manual_gates": [""]},
            {"runtime": {"provider": "other", "agent": "codex"}},
            {"runtime": {"provider": "orca", "agent": "gpt"}},
            {"runtime": {"provider": "orca"}},
            {"runtime": {"provider": "orca", "agent": "codex", "workspace": "current"}},
            {"runtime": {"provider": "orca", "agent": "codex", "orca_project_id": "has space"}},
            {"alias": "sample"},
            {"aliases": ["sample"]},
        ]
        for extra in invalid:
            with self.subTest(extra=extra), self.assertRaises(ProjectOverlayError):
                self.parse({"sample-alpha": {**base, **extra}})
        with self.assertRaises(ProjectOverlayError):
            ProjectOverlay.from_dict({**overlay({}), "aliases": {}}, registry=self.registry)
        with self.assertRaises(ProjectOverlayError):
            ProjectOverlay.from_dict({**overlay({}), "scope": "global"}, registry=self.registry)

    def test_authority_keys_are_rejected_anywhere_except_persona_roles(self) -> None:
        base = {"owner_agent_id": "implementer", "status": "active"}
        for extra in (
            {"permissions": ["write_external"]},
            {"capabilities": ["deploy"]},
            {"runtime_binding": {"provider": "codex"}},
            {"runtime": {"provider": "orca", "agent": "codex", "tools": ["shell"]}},
            {"persona_assignments": {"roles": {}, "permissions": {}}},
            {"persona_assignments": {"agents": {"implementer": "p1"}, "commands": {}}},
        ):
            with self.subTest(extra=extra), self.assertRaisesRegex(ProjectOverlayError, "authority"):
                self.parse({"sample-alpha": {**base, **extra}})
        with self.assertRaisesRegex(ProjectOverlayError, "authority"):
            self.parse({"tools": base})
        with self.assertRaisesRegex(ProjectOverlayError, "authority"):
            ProjectOverlay.from_dict({**overlay({}), "routes": {}}, registry=self.registry)
        allowed = self.parse(
            {"sample-alpha": {**base, "persona_assignments": {"roles": {"implementer": "p1"}}}}
        )
        self.assertEqual(dict(allowed.projects["sample-alpha"].persona_role_assignments), {"implementer": "p1"})

    def test_unknown_profile_ids_fail_cross_file_validation(self) -> None:
        parsed = self.parse(
            {
                "sample-alpha": {
                    "owner_agent_id": "implementer",
                    "status": "active",
                    "persona_assignments": {"roles": {"implementer": "sample-missing-profile"}},
                }
            }
        )
        global_settings = PersonaSettings.empty("global")
        with self.assertRaisesRegex(PersonaSettingsError, "unknown profile"):
            PersonaLayers(self.registry, global_settings, None, parsed)


class ProjectOverlayRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = registry_data()
        self.registry = AgentRegistry.from_dict(self.raw)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def merged(self, projects: dict, registry: AgentRegistry | None = None) -> AgentRegistry:
        registry = registry or self.registry
        parsed = ProjectOverlay.from_dict(overlay(projects), registry=registry)
        return apply_project_overlay(registry, parsed, base_dir=ROOT)

    def test_union_with_registry_routes_and_collisions_fail_closed(self) -> None:
        configured = copy.deepcopy(self.raw)
        configured["project_routes"] = {
            "sample-shared": {"agent_id": "reviewer", "path": str(self.base / "shared")}
        }
        shared = AgentRegistry.from_dict(configured)
        merged = self.merged(
            {"sample-alpha": {"owner_agent_id": "implementer", "status": "active", "path": str(self.base / "alpha")}},
            shared,
        )
        self.assertEqual(merged.project_routes, {"sample-shared": "reviewer", "sample-alpha": "implementer"})
        self.assertEqual(merged.project_status("sample-shared"), "active")

        with self.assertRaisesRegex(ProjectOverlayError, "duplicates a registered project id"):
            self.merged({"sample-shared": {"owner_agent_id": "implementer", "status": "active"}}, shared)
        with self.assertRaisesRegex(ProjectOverlayError, "already used"):
            self.merged(
                {"sample-alpha": {"owner_agent_id": "implementer", "status": "active", "path": str(self.base / "shared")}},
                shared,
            )
        with self.assertRaisesRegex(ProjectOverlayError, "already used"):
            self.merged(
                {
                    "sample-alpha": {"owner_agent_id": "implementer", "status": "active", "path": str(self.base / "a")},
                    "sample-beta": {
                        "owner_agent_id": "reviewer",
                        "status": "active",
                        "worktree_roots": [str(self.base / "a")],
                    },
                }
            )
        with self.assertRaisesRegex(ProjectOverlayError, "already used"):
            self.merged(
                {
                    "sample-alpha": {"owner_agent_id": "implementer", "status": "active", "path": str(self.base / "a")},
                    "sample-inventory": {"owner_agent_id": "coordinator", "status": "inventory_only", "path": str(self.base / "a")},
                }
            )

    @unittest.skipUnless(os.name == "posix", "symlinked directories")
    def test_paths_resolving_to_the_same_directory_collide(self) -> None:
        real = self.base / "real"
        real.mkdir()
        (self.base / "alias").symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ProjectOverlayError, "already used"):
            self.merged(
                {
                    "sample-alpha": {"owner_agent_id": "implementer", "status": "active", "path": str(real)},
                    "sample-beta": {"owner_agent_id": "reviewer", "status": "active", "path": str(self.base / "alias")},
                }
            )

    def test_deepest_path_and_worktree_roots_select_the_project(self) -> None:
        merged = self.merged(
            {
                "sample-alpha": {
                    "owner_agent_id": "implementer",
                    "status": "active",
                    "path": str(self.base / "alpha"),
                    "worktree_roots": [str(self.base / "worktrees" / "alpha")],
                },
                "sample-beta": {"owner_agent_id": "reviewer", "status": "active", "path": str(self.base / "alpha" / "beta")},
            }
        )
        lookup = lambda cwd: merged.project_for_working_directory(cwd, base_dir=ROOT)  # noqa: E731
        self.assertEqual(lookup(self.base / "alpha" / "src"), "sample-alpha")
        self.assertEqual(lookup(self.base / "alpha" / "beta" / "src"), "sample-beta")
        self.assertEqual(lookup(self.base / "worktrees" / "alpha" / "feature-1"), "sample-alpha")
        self.assertIsNone(lookup(self.base / "worktrees"))

    def test_equal_depth_match_fails_closed(self) -> None:
        # Two projects whose declared paths resolve to the same directory without passing
        # through the overlay merge (which would already reject them).
        ambiguous = AgentRegistry(
            default_agent_id="coordinator",
            agents=self.registry.agents,
            max_registered_agents=100,
            max_concurrent_agents=4,
            project_routes={"sample-alpha": "implementer", "sample-beta": "reviewer"},
            project_paths={"sample-alpha": str(self.base / "same")},
            project_worktree_roots={"sample-beta": (str(self.base / "same"),)},
        )
        with self.assertRaises(RegistryValidationError):
            ambiguous.project_for_working_directory(self.base / "same" / "src", base_dir=ROOT)

    def test_hold_routes_to_hold_agent_and_refuses_implementers_everywhere(self) -> None:
        merged = self.merged(
            {
                "sample-hold": {
                    "owner_agent_id": "implementer",
                    "status": "hold",
                    "hold_agent_id": "researcher",
                    "path": str(self.base / "hold"),
                },
                "sample-open": {"owner_agent_id": "implementer", "status": "active"},
            }
        )
        explicit_project = resolve_route(merged, project_id="sample-hold")
        self.assertTrue(explicit_project.allowed)
        self.assertEqual(explicit_project.state.active_agent_id, "researcher")
        cwd_route = resolve_route(merged, working_directory_project_id="sample-hold")
        self.assertEqual(cwd_route.state.active_agent_id, "researcher")
        self.assertEqual(cwd_route.route_source, "working_directory")

        blocked_requests = [
            {"explicit_agent_id": "implementer", "working_directory_project_id": "sample-hold"},
            {"explicit_agent_id": "implementer", "project_id": "sample-hold"},
            {"explicit_agent_id": "documentation_specialist", "project_id": "sample-hold"},
            {"requested_role": "implementer", "project_id": "sample-hold"},
            {"requested_role": "implementer", "working_directory_project_id": "sample-hold"},
            {"previous": ConversationState("implementer", "sample-hold")},
            {"project_id": "sample-hold", "supporting_agent_ids": ("implementer",)},
        ]
        for request in blocked_requests:
            with self.subTest(request=request):
                decision = resolve_route(merged, **request)
                self.assertEqual(decision.status, "blocked")
                self.assertIn("on hold", decision.reason)

        reviewer = resolve_route(merged, explicit_agent_id="reviewer", project_id="sample-hold")
        self.assertTrue(reviewer.allowed)
        switched = resolve_route(
            merged, project_id="sample-open", previous=ConversationState("implementer", "sample-hold")
        )
        self.assertTrue(switched.allowed)
        self.assertEqual(switched.state.active_agent_id, "implementer")

    def test_inventory_only_projects_are_never_routed(self) -> None:
        merged = self.merged(
            {
                "sample-parent": {"owner_agent_id": "implementer", "status": "active", "path": str(self.base / "parent")},
                "sample-inventory": {
                    "owner_agent_id": "implementer",
                    "status": "inventory_only",
                    "path": str(self.base / "parent" / "legacy"),
                },
            }
        )
        self.assertNotIn("sample-inventory", merged.project_routes)
        self.assertEqual(merged.project_status("sample-inventory"), "inventory_only")
        blocked = resolve_route(merged, project_id="sample-inventory")
        self.assertEqual(blocked.status, "blocked")
        self.assertIn("inventory-only", blocked.reason)
        self.assertIsNone(
            merged.project_for_working_directory(self.base / "parent" / "legacy" / "src", base_dir=ROOT)
        )
        self.assertEqual(
            merged.project_for_working_directory(self.base / "parent" / "src", base_dir=ROOT), "sample-parent"
        )
        stale = resolve_route(merged, previous=ConversationState("implementer", "sample-inventory"))
        self.assertEqual(stale.status, "blocked")


class ProjectPersonaTierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = AgentRegistry.from_dict(registry_data())
        self.global_settings = PersonaSettings.from_dict(
            {
                "schema_version": "1.0",
                "scope": "global",
                "profiles": {"sample-global": profile("sample-global", "global style")},
                "default_profile_id": "sample-global",
                "assignments": {"agents": {}, "roles": {}},
            },
            expected_scope="global",
        )
        self.personal = PersonaSettings.from_dict(
            {
                "schema_version": "1.0",
                "scope": "personal",
                "profiles": {
                    "sample-personal-agent": profile("sample-personal-agent", "personal agent style"),
                    "sample-project-role": profile("sample-project-role", "project role style"),
                    "sample-project-agent": profile("sample-project-agent", "project agent style"),
                },
                "default_profile_id": None,
                "assignments": {"agents": {"implementer": "sample-personal-agent", "validator": "sample-personal-agent"}, "roles": {}},
            },
            expected_scope="personal",
        )
        self.overlay = ProjectOverlay.from_dict(
            overlay(
                {
                    "sample-alpha": {
                        "owner_agent_id": "implementer",
                        "status": "active",
                        "persona_assignments": {
                            "roles": {"implementer": "sample-project-role", "validator": "sample-project-role"},
                            "agents": {"validator": "sample-project-agent"},
                        },
                    },
                    "sample-inventory": {
                        "owner_agent_id": "implementer",
                        "status": "inventory_only",
                        "persona_assignments": {"roles": {"implementer": "sample-global"}},
                    },
                }
            ),
            registry=self.registry,
        )
        self.layers = PersonaLayers(self.registry, self.global_settings, self.personal, self.overlay)

    def test_project_agent_then_project_role_precede_personal_tiers(self) -> None:
        validator = self.registry.get("validator")
        implementer = self.registry.get("implementer")
        reviewer = self.registry.get("reviewer")
        resolved = self.layers.resolve(validator, project_id="sample-alpha")
        self.assertEqual((resolved.source, resolved.profile_id), ("project-agent", "sample-project-agent"))
        resolved = self.layers.resolve(implementer, project_id="sample-alpha")
        self.assertEqual((resolved.source, resolved.profile_id), ("project-role", "sample-project-role"))
        resolved = self.layers.resolve(reviewer, project_id="sample-alpha")
        self.assertEqual(resolved.source, "global-default")

    def test_project_tier_applies_only_to_the_active_routed_project(self) -> None:
        implementer = self.registry.get("implementer")
        for project_id in (None, "sample-other", "sample-inventory"):
            with self.subTest(project_id=project_id):
                resolved = self.layers.resolve(implementer, project_id=project_id)
                self.assertEqual(resolved.source, "personal-agent")


class ProjectOverlayHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.config = self.base / "config"
        self.workspace = self.base / "workspace"
        (self.workspace / "alpha" / "src").mkdir(parents=True)
        (self.workspace / "hold").mkdir(parents=True)
        (self.workspace / "worktrees" / "alpha-feature").mkdir(parents=True)
        self.persona_path = write_private(
            self.config / "persona-settings.json",
            {
                "schema_version": "1.0",
                "scope": "personal",
                "profiles": {
                    "sample-project-voice": {
                        **profile("sample-project-voice", "synthetic project voice"),
                        "source": {
                            "project": "sample-source-project",
                            "records": [{"path": "records/sample.json", "record_id": "sample-record", "sha256": "a" * 64}],
                        },
                    }
                },
                "default_profile_id": None,
                "assignments": {"agents": {}, "roles": {}},
            },
        )
        self.overlay_raw = overlay(
            {
                "sample-alpha": {
                    "path": str(self.workspace / "alpha"),
                    "worktree_roots": [str(self.workspace / "worktrees" / "alpha-feature")],
                    "repository": "example-owner/sample-alpha-repository",
                    "owner_agent_id": "implementer",
                    "status": "active",
                    "persona_assignments": {"roles": {"implementer": "sample-project-voice"}},
                    "constraints": ["synthetic constraint: no external writes"],
                    "validation": [["python", "-m", "unittest"]],
                    "manual_gates": ["sample-gate"],
                    "runtime": {
                        "provider": "orca",
                        "agent": "codex",
                        "orca_project_id": "sample-orca-identifier",
                        "workspace": "new-top-level",
                    },
                },
                "sample-hold": {
                    "path": str(self.workspace / "hold"),
                    "owner_agent_id": "implementer",
                    "status": "hold",
                    "hold_agent_id": "researcher",
                },
            }
        )
        self.overlay_path = write_private(self.config / "project-overlay.json", self.overlay_raw)
        self.state_root = self.base / "state"
        self.state_root.mkdir(mode=0o700)
        for patcher in (
            patch("adapters.codex.route_agent.tempfile.gettempdir", return_value=str(self.state_root)),
            patch.dict(os.environ, {PERSONA_ENV: str(self.persona_path), PROJECT_OVERLAY_ENV: str(self.overlay_path)}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_hook(self, session: str, cwd: Path, prompt: str) -> dict:
        return handle({"hook_event_name": "UserPromptSubmit", "session_id": session, "cwd": str(cwd), "prompt": prompt})

    def test_context_has_project_status_and_constraints_but_no_private_locations(self) -> None:
        for session, cwd in (
            ("synthetic-hook-1", self.workspace / "alpha" / "src"),
            ("synthetic-hook-2", self.workspace / "worktrees" / "alpha-feature"),
        ):
            with self.subTest(cwd=cwd):
                context = self.run_hook(session, cwd, "synthetic first turn")["hookSpecificOutput"]["additionalContext"]
                self.assertIn("active_agent_id=implementer", context)
                self.assertIn("project_id=sample-alpha", context)
                self.assertIn("project_status=active", context)
                self.assertIn("route_source=working_directory", context)
                self.assertIn("synthetic constraint: no external writes", context)
                self.assertIn("synthetic project voice", context)
                for private in (
                    str(self.workspace),
                    str(self.base),
                    "example-owner",
                    "sample-alpha-repository",
                    "sample-orca-identifier",
                    "new-top-level",
                    "sample-gate",
                    "sample-project-voice",
                    "sample-source-project",
                    "records/sample.json",
                    "a" * 64,
                ):
                    self.assertNotIn(private, context)

        for state_file in self.state_root.rglob("*.json"):
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(set(state), {"version", "active_agent_id", "active_project_id", "supporting_agent_ids"})
            self.assertNotIn(str(self.workspace), state_file.read_text(encoding="utf-8"))
            self.assertNotIn("synthetic constraint", state_file.read_text(encoding="utf-8"))

    def test_hold_project_blocks_implementer_tokens_and_continuity(self) -> None:
        hold = self.workspace / "hold"
        first = self.run_hook("synthetic-hold-1", hold, "synthetic first turn")
        context = first["hookSpecificOutput"]["additionalContext"]
        self.assertIn("active_agent_id=researcher", context)
        self.assertIn("project_status=hold", context)
        self.assertIn("on hold", context)
        for prompt in (
            "please @agent:implementer",
            "please role:implementer",
            "support:agent:implementer",
            "please @agent:<placeholder> @agent:implementer",
        ):
            with self.subTest(prompt=prompt):
                result = self.run_hook("synthetic-hold-2", hold, prompt)
                self.assertEqual(result["decision"], "block")
                self.assertIn("on hold", result["reason"])

        alpha = self.workspace / "alpha"
        self.assertIn(
            "active_agent_id=implementer",
            self.run_hook("synthetic-hold-3", alpha, "synthetic")["hookSpecificOutput"]["additionalContext"],
        )
        # Status changes to hold after an implementer conversation was established.
        self.overlay_raw["projects"]["sample-alpha"].update({"status": "hold", "hold_agent_id": "reviewer"})
        write_private(self.overlay_path, self.overlay_raw)
        result = self.run_hook("synthetic-hold-3", alpha, "continue")
        self.assertEqual(result["decision"], "block")
        explicit = self.run_hook("synthetic-hold-3", alpha, "switch project:sample-alpha")
        self.assertIn("active_agent_id=reviewer", explicit["hookSpecificOutput"]["additionalContext"])

    def test_hold_project_bad_tokens_never_bypass_hold(self) -> None:
        hold = self.workspace / "hold"
        # Malformed or unavailable tokens next to a write-capable selection still block.
        for index, prompt in enumerate(
            (
                "@agent:IMPLEMENTER",
                "role:<placeholder> role:implementer",
                "project:<placeholder> @agent:implementer",
                "@agent:private_missing @agent:implementer",
                "support:agent:<placeholder> support:agent:implementer",
                "support:agent:researcher support:agent:implementer",
            )
        ):
            with self.subTest(case=index):
                result = self.run_hook(f"synthetic-hold-bad-{index}", hold, prompt)
                self.assertEqual(result["decision"], "block")
                self.assertIn("on hold", result["reason"])
                self.assertNotIn("<placeholder>", result["reason"])

        # A bad token alone routes exactly as the prompt without it: the hold agent.
        for index, prompt in enumerate(("project:<placeholder>", "@agent:<placeholder>", "role:<placeholder>")):
            with self.subTest(alone=index):
                context = self.run_hook(f"synthetic-hold-alone-{index}", hold, prompt)["hookSpecificOutput"][
                    "additionalContext"
                ]
                self.assertIn("active_agent_id=researcher", context)
                self.assertIn("project_status=hold", context)
                self.assertIn("route_source=working_directory", context)
                self.assertIn("Routing warning:", context)
                self.assertNotIn("<placeholder>", context)

        # Ignoring an explicit project token never reroutes away from the hold working directory.
        context = self.run_hook("synthetic-hold-project", hold, "project:sample-unknown")["hookSpecificOutput"][
            "additionalContext"
        ]
        self.assertIn("active_agent_id=researcher", context)
        self.assertIn("project_id=sample-hold", context)
        self.assertNotIn("sample-unknown", context)

    def test_hold_project_blocks_write_capable_value_inside_ignored_group(self) -> None:
        hold = self.workspace / "hold"
        for index, prompt in enumerate(
            (
                "@agent:reviewer @agent:implementer",
                "@agent:implementer @agent:reviewer",
                "role:reviewer role:implementer",
                "@agent:reviewer role:implementer",
                "support:agent:validator support:agent:documentation_specialist",
            )
        ):
            with self.subTest(case=index):
                result = self.run_hook(f"synthetic-hold-group-{index}", hold, prompt)
                self.assertEqual(result["decision"], "block")
                self.assertIn("on hold", result["reason"])

        # From an active working directory, a conflicting project group that includes
        # the hold project still refuses the write-capable agent.
        alpha = self.workspace / "alpha"
        result = self.run_hook(
            "synthetic-hold-project-group", alpha, "project:sample-alpha project:sample-hold @agent:implementer"
        )
        self.assertEqual(result["decision"], "block")
        self.assertIn("on hold", result["reason"])
        # Without the write-capable agent the ignored project group warns and keeps the cwd route.
        context = self.run_hook(
            "synthetic-hold-project-group-ok", alpha, "project:sample-alpha project:sample-hold @agent:reviewer"
        )["hookSpecificOutput"]["additionalContext"]
        self.assertIn("active_agent_id=reviewer", context)
        self.assertIn("project_id=sample-alpha", context)
        self.assertIn("multiple distinct values", context)

    def test_hold_project_blocks_ambiguous_role_with_write_capable_match(self) -> None:
        data = json.loads((ROOT / ".agents" / "registry.json").read_text(encoding="utf-8"))
        extra = copy.deepcopy(next(agent for agent in data["agents"] if agent["agent_id"] == "implementer"))
        extra["agent_id"] = "implementer_backup"
        extra["runtime_binding"]["agent_name"] = "implementer_backup"
        data["agents"].append(extra)
        registry_path = self.base / "registry-ambiguous.json"
        registry_path.write_text(json.dumps(data), encoding="utf-8")
        with patch("adapters.codex.route_agent._registry_path", return_value=registry_path):
            held = self.run_hook("synthetic-hold-ambiguous", self.workspace / "hold", "role:implementer")
            self.assertEqual(held["decision"], "block")
            self.assertIn("on hold", held["reason"])
            active = self.run_hook("synthetic-active-ambiguous", self.workspace / "alpha", "role:implementer")
            context = active["hookSpecificOutput"]["additionalContext"]
            self.assertIn("maps to multiple enabled agents", context)
            self.assertIn("route_source=working_directory", context)

    def test_hold_project_blocks_disabled_or_incomplete_write_capable_selection(self) -> None:
        for label, binding in (
            ("disabled", None),
            ("incomplete", {"provider": None, "agent_name": None, "enabled": False}),
        ):
            data = json.loads((ROOT / ".agents" / "registry.json").read_text(encoding="utf-8"))
            writer = copy.deepcopy(next(agent for agent in data["agents"] if agent["agent_id"] == "implementer"))
            writer.update({"agent_id": "kit_writer", "role": "kit_writer"})
            writer["runtime_binding"]["agent_name"] = "kit_writer"
            writer["runtime_binding"]["enabled"] = False
            if binding is not None:
                writer["runtime_binding"] = binding
            data["agents"].append(writer)
            registry_path = self.base / f"registry-{label}.json"
            registry_path.write_text(json.dumps(data), encoding="utf-8")
            with patch("adapters.codex.route_agent._registry_path", return_value=registry_path):
                for index, prompt in enumerate(
                    ("@agent:kit_writer", "role:kit_writer", "support:agent:kit_writer", "@agent:reviewer @agent:kit_writer")
                ):
                    with self.subTest(binding=label, prompt=index):
                        held = self.run_hook(f"synthetic-hold-{label}-{index}", self.workspace / "hold", prompt)
                        self.assertEqual(held["decision"], "block")
                        self.assertIn("on hold", held["reason"])
                        active = self.run_hook(f"synthetic-active-{label}-{index}", self.workspace / "alpha", prompt)
                        context = active["hookSpecificOutput"]["additionalContext"]
                        # Only the unavailable value is dropped; a valid read-only one still applies.
                        expected = "route_source=explicit_agent" if index == 3 else "route_source=working_directory"
                        self.assertIn(expected, context)
                        self.assertIn("Routing warning:", context)
                        self.assertNotIn("kit_writer", context)

    def test_hold_project_nested_and_non_ascii_tokens_are_ignored(self) -> None:
        hold = self.workspace / "hold"
        prompts = [
            f"{outer}{joiner}{inner}"
            for outer in ("@agent:<x>", "role:<x>", "project:<x>", "support:agent:<x>")
            for inner in ("@agent:implementer", "role:implementer", "project:sample-alpha", "support:agent:implementer")
            for joiner in ("(", "/")
        ]
        prompts += [
            "@agent:İmplementer",
            "@agent:ımplementer",
            "@agent:ｉmplementer",
            "role:implementeŕ",
            "support:agent:ımplementer",
            "project:sample-аlpha",
        ]
        for index, prompt in enumerate(prompts):
            with self.subTest(case=index):
                result = self.run_hook(f"synthetic-hold-nested-{index}", hold, prompt)
                self.assertNotIn("decision", result)
                context = result["hookSpecificOutput"]["additionalContext"]
                self.assertIn("active_agent_id=researcher", context)
                self.assertIn("project_id=sample-hold", context)
                self.assertIn("supporting_agent_ids=none", context)
                self.assertIn("Routing warning:", context)

    def test_hold_project_two_read_only_values_warn_and_route_to_hold_agent(self) -> None:
        hold = self.workspace / "hold"
        for index, prompt in enumerate(("@agent:reviewer @agent:validator", "role:reviewer role:validator")):
            with self.subTest(case=index):
                context = self.run_hook(f"synthetic-hold-readonly-{index}", hold, prompt)["hookSpecificOutput"][
                    "additionalContext"
                ]
                self.assertIn("active_agent_id=researcher", context)
                self.assertIn("project_status=hold", context)
                self.assertIn("multiple distinct values", context)

    def test_active_project_two_values_warn_and_ignore_the_kind(self) -> None:
        alpha = self.workspace / "alpha"
        for index, prompt in enumerate(
            ("@agent:reviewer @agent:implementer", "role:reviewer role:implementer")
        ):
            with self.subTest(case=index):
                context = self.run_hook(f"synthetic-active-two-{index}", alpha, prompt)["hookSpecificOutput"][
                    "additionalContext"
                ]
                self.assertIn("active_agent_id=implementer", context)
                self.assertIn("project_status=active", context)
                self.assertIn("route_source=working_directory", context)
                self.assertIn("multiple distinct values", context)

    def test_continuity_on_hold_still_blocks_when_bad_tokens_are_ignored(self) -> None:
        alpha = self.workspace / "alpha"
        self.run_hook("synthetic-hold-continuity", alpha, "synthetic")
        self.overlay_raw["projects"]["sample-alpha"].update({"status": "hold", "hold_agent_id": "reviewer"})
        write_private(self.overlay_path, self.overlay_raw)
        for prompt in ("project:<placeholder>", "@agent:<placeholder> role:<placeholder>", "@agent:private_missing"):
            with self.subTest(prompt=prompt):
                result = self.run_hook("synthetic-hold-continuity", alpha, prompt)
                self.assertEqual(result["decision"], "block")
                self.assertIn("on hold", result["reason"])

    def test_invalid_overlay_fails_closed_without_echoing_contents(self) -> None:
        secret = "private-overlay-value-never-echoed"
        self.overlay_raw["projects"]["sample-alpha"]["permissions"] = [secret]
        write_private(self.overlay_path, self.overlay_raw)
        with self.assertRaises(HookError) as error:
            self.run_hook("synthetic-invalid", self.workspace / "alpha", "synthetic")
        self.assertNotIn(secret, str(error.exception))
        self.assertEqual(list(self.state_root.rglob("*.json")), [])

    def test_unknown_profile_in_overlay_fails_closed(self) -> None:
        self.overlay_raw["projects"]["sample-alpha"]["persona_assignments"] = {"roles": {"implementer": "sample-missing"}}
        write_private(self.overlay_path, self.overlay_raw)
        with self.assertRaises(HookError):
            self.run_hook("synthetic-missing-profile", self.workspace / "alpha", "synthetic")

    def test_cli_validate_reports_counts_only(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), patch("agent_runtime.cli.Path.cwd", return_value=ROOT):
            code = cli_main(
                [
                    "validate-project-overlay",
                    "--registry", str(ROOT / ".agents" / "registry.json"),
                    "--global-settings", str(ROOT / ".agents" / "persona-settings.json"),
                ]
            )
        self.assertEqual(code, 0)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["overlay_projects"], 2)
        self.assertEqual(output["hold_projects"], 1)
        self.assertEqual(output["effective_project_routes"], 2)
        self.assertNotIn("sample-alpha", stdout.getvalue())
        self.assertNotIn(str(self.workspace), stdout.getvalue())

        self.overlay_raw["projects"]["sample-alpha"]["persona_assignments"] = {"roles": {"implementer": "sample-missing"}}
        write_private(self.overlay_path, self.overlay_raw)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), patch("agent_runtime.cli.Path.cwd", return_value=ROOT):
            code = cli_main(
                [
                    "validate-project-overlay",
                    "--registry", str(ROOT / ".agents" / "registry.json"),
                    "--global-settings", str(ROOT / ".agents" / "persona-settings.json"),
                ]
            )
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
