"""Regression tests for the PR #7 review findings: path identity, private file
descriptors and error-message privacy."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unicodedata
import unittest
from unittest.mock import patch

from adapters.codex.route_agent import HookError, handle
from agent_runtime import private_files
from agent_runtime.cli import main as cli_main
from agent_runtime.path_identity import normalized_spelling
from agent_runtime.persona_layers import PersonaSettingsError, load_persona_layers
from agent_runtime.project_overlay import (
    PROJECT_OVERLAY_ENV,
    ProjectOverlay,
    ProjectOverlayError,
    apply_project_overlay,
    initialize_project_overlay,
    load_project_overlay,
)
from agent_runtime.registry import AgentRegistry, RegistryValidationError
from agent_runtime.routing import resolve_route


ROOT = Path(__file__).resolve().parents[1]
PERSONA_ENV = "MULTI_AGENT_INDUSTRY_PERSONA_SETTINGS"
NFC_NAME = unicodedata.normalize("NFC", "sample-café")
NFD_NAME = unicodedata.normalize("NFD", "sample-café")


def registry() -> AgentRegistry:
    return AgentRegistry.from_dict(json.loads((ROOT / ".agents" / "registry.json").read_text(encoding="utf-8")))


def overlay(projects: dict) -> dict:
    return {"schema_version": "1.0", "scope": "personal", "projects": projects}


def hold(path: str) -> dict:
    return {"owner_agent_id": "implementer", "status": "hold", "hold_agent_id": "reviewer", "path": path}


def active(path: str, agent: str = "implementer") -> dict:
    return {"owner_agent_id": agent, "status": "active", "path": path}


def write_private(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)
        path.parent.chmod(0o700)
    return path


def _probe(directory: Path, created: str, variant: str) -> bool:
    """Detect filesystem behaviour at runtime instead of assuming it from the platform."""

    probe_root = Path(tempfile.mkdtemp(dir=directory))
    try:
        target = probe_root / created
        target.mkdir()
        other = probe_root / variant
        return other.exists() and os.path.samefile(target, other)
    except OSError:
        return False


_PROBE_DIR = Path(tempfile.gettempdir())
CASE_INSENSITIVE = _probe(_PROBE_DIR, "SampleCaseProbe", "samplecaseprobe")
NORMALIZATION_INSENSITIVE = _probe(_PROBE_DIR, NFC_NAME, NFD_NAME)


class PathIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = registry()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def merged(self, projects: dict) -> AgentRegistry:
        parsed = ProjectOverlay.from_dict(overlay(projects), registry=self.registry)
        return apply_project_overlay(self.registry, parsed, base_dir=ROOT)

    def assert_hold_applies(self, merged: AgentRegistry, cwd: Path) -> None:
        project = merged.project_for_working_directory(cwd, base_dir=ROOT)
        self.assertEqual(project, "sample-held")
        for request in (
            {"explicit_agent_id": "implementer"},
            {"requested_role": "implementer"},
            {"supporting_agent_ids": ("implementer",)},
        ):
            decision = resolve_route(merged, working_directory_project_id=project, **request)
            self.assertEqual(decision.status, "blocked", request)
            self.assertIn("on hold", decision.reason)
        self.assertEqual(
            resolve_route(merged, working_directory_project_id=project).state.active_agent_id, "reviewer"
        )

    @unittest.skipUnless(CASE_INSENSITIVE, "requires a case-insensitive filesystem")
    def test_case_variant_cwd_cannot_bypass_hold(self) -> None:
        held = self.base / "HeldProject"
        (held / "src").mkdir(parents=True)
        merged = self.merged({"sample-held": hold(str(held))})
        self.assert_hold_applies(merged, self.base / "heldproject")
        self.assert_hold_applies(merged, self.base / "HELDPROJECT" / "src")
        self.assert_hold_applies(merged, self.base / "heldproject" / "not-created-yet")

    @unittest.skipUnless(CASE_INSENSITIVE, "requires a case-insensitive filesystem")
    def test_case_variant_roots_collide(self) -> None:
        (self.base / "SharedRoot").mkdir()
        with self.assertRaisesRegex(ProjectOverlayError, "already used"):
            self.merged(
                {
                    "sample-held": hold(str(self.base / "SharedRoot")),
                    "sample-open": active(str(self.base / "sharedroot")),
                }
            )

    @unittest.skipUnless(NORMALIZATION_INSENSITIVE, "requires a normalization-insensitive filesystem")
    def test_unicode_nfc_and_nfd_spellings_match_and_collide(self) -> None:
        (self.base / NFC_NAME).mkdir()
        merged = self.merged({"sample-held": hold(str(self.base / NFD_NAME))})
        self.assert_hold_applies(merged, self.base / NFC_NAME)
        with self.assertRaisesRegex(ProjectOverlayError, "already used"):
            self.merged(
                {
                    "sample-held": hold(str(self.base / NFC_NAME)),
                    "sample-open": active(str(self.base / NFD_NAME)),
                }
            )

    def test_unicode_spellings_collide_even_when_paths_do_not_exist(self) -> None:
        self.assertEqual(normalized_spelling(self.base / NFC_NAME), normalized_spelling(self.base / NFD_NAME))
        with self.assertRaisesRegex(ProjectOverlayError, "already used"):
            self.merged(
                {
                    "sample-held": hold(str(self.base / "missing" / NFC_NAME)),
                    "sample-open": active(str(self.base / "missing" / NFD_NAME)),
                }
            )

    @unittest.skipUnless(sys.platform == "darwin" or os.name == "nt", "case folding fallback is platform-specific")
    def test_missing_paths_use_case_folded_spelling(self) -> None:
        merged = self.merged({"sample-held": hold(str(self.base / "NotCreated"))})
        self.assertEqual(
            merged.project_for_working_directory(self.base / "notcreated" / "x", base_dir=ROOT), "sample-held"
        )

    @unittest.skipUnless(os.name == "posix", "symlinked directories")
    def test_symlinked_alias_cannot_bypass_hold_and_collides(self) -> None:
        real = self.base / "real-held"
        (real / "src").mkdir(parents=True)
        alias = self.base / "alias-held"
        alias.symlink_to(real, target_is_directory=True)
        self.assert_hold_applies(self.merged({"sample-held": hold(str(real))}), alias / "src")
        self.assert_hold_applies(self.merged({"sample-held": hold(str(alias))}), real / "src")
        with self.assertRaisesRegex(ProjectOverlayError, "already used"):
            self.merged({"sample-held": hold(str(real)), "sample-open": active(str(alias))})

    @unittest.skipUnless(os.name == "posix", "symlinked directories")
    def test_root_nested_via_alias_is_deeper_and_wins(self) -> None:
        real = self.base / "outer"
        (real / "inner" / "src").mkdir(parents=True)
        alias = self.base / "outer-alias"
        alias.symlink_to(real, target_is_directory=True)
        merged = self.merged(
            {"sample-open": active(str(real), "reviewer"), "sample-held": hold(str(alias / "inner"))}
        )
        self.assert_hold_applies(merged, real / "inner" / "src")
        self.assertEqual(merged.project_for_working_directory(real / "other", base_dir=ROOT), "sample-open")

    def test_trailing_slash_spelling_matches_and_collides(self) -> None:
        held = self.base / "slash-held"
        held.mkdir()
        self.assert_hold_applies(self.merged({"sample-held": hold(str(held) + "/")}), held)
        with self.assertRaisesRegex(ProjectOverlayError, "already used"):
            self.merged({"sample-held": hold(str(held)), "sample-open": active(str(held) + "/")})

    def test_matching_failure_fails_closed_instead_of_selecting_no_project(self) -> None:
        held = self.base / "held"
        held.mkdir()
        merged = self.merged({"sample-held": hold(str(held))})
        with patch("agent_runtime.registry.ancestor_identities", side_effect=OSError("synthetic")):
            with self.assertRaises(RegistryValidationError):
                merged.project_for_working_directory(held, base_dir=ROOT)


class HookPathIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.held = self.base / "workspace" / "HeldProject"
        self.held.mkdir(parents=True)
        self.overlay_path = write_private(
            self.base / "config" / "project-overlay.json", overlay({"sample-held": hold(str(self.held))})
        )
        state = self.base / "state"
        state.mkdir(mode=0o700)
        for patcher in (
            patch("adapters.codex.route_agent.tempfile.gettempdir", return_value=str(state)),
            patch.dict(
                os.environ,
                {PERSONA_ENV: str(self.base / "config" / "missing.json"), PROJECT_OVERLAY_ENV: str(self.overlay_path)},
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_hook(self, cwd: Path, prompt: str) -> dict:
        return handle({"hook_event_name": "UserPromptSubmit", "session_id": "synthetic-identity", "cwd": str(cwd), "prompt": prompt})

    @unittest.skipUnless(CASE_INSENSITIVE, "requires a case-insensitive filesystem")
    def test_hook_blocks_implementer_for_case_variant_cwd(self) -> None:
        result = self.run_hook(self.base / "workspace" / "heldproject", "please @agent:implementer")
        self.assertEqual(result["decision"], "block")
        self.assertIn("on hold", result["reason"])

    def test_hook_fails_closed_when_matching_raises(self) -> None:
        with patch("agent_runtime.registry.ancestor_identities", side_effect=OSError("synthetic")):
            with self.assertRaises(HookError):
                self.run_hook(self.held, "please @agent:implementer")


@unittest.skipUnless(private_files._descriptor_checks_supported(), "POSIX descriptor checks")
class PrivateFileDescriptorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = registry()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.private_dir = self.base / "private"
        self.path = write_private(
            self.private_dir / "project-overlay.json",
            overlay({"sample-alpha": {"owner_agent_id": "implementer", "status": "active"}}),
        )

    def test_normal_owner_only_setup_loads_for_overlay_and_personas(self) -> None:
        self.assertEqual(set(load_project_overlay(self.registry, self.path, repo_root=ROOT).projects), {"sample-alpha"})
        persona = write_private(
            self.private_dir / "persona-settings.json",
            {
                "schema_version": "1.0",
                "scope": "personal",
                "profiles": {},
                "default_profile_id": None,
                "assignments": {"agents": {}, "roles": {}},
            },
        )
        layers = load_persona_layers(
            self.registry, global_path=ROOT / ".agents" / "persona-settings.json", personal_path=persona, repo_root=ROOT
        )
        self.assertEqual(layers.personal_settings.scope, "personal")

    def test_missing_file_in_broad_directory_is_still_empty(self) -> None:
        broad = self.base / "broad"
        broad.mkdir(mode=0o755)
        broad.chmod(0o755)
        self.assertEqual(dict(load_project_overlay(self.registry, broad / "project-overlay.json", repo_root=ROOT).projects), {})

    def test_parent_directory_symlink_alias_is_rejected(self) -> None:
        alias = self.base / "alias"
        alias.symlink_to(self.private_dir, target_is_directory=True)
        with self.assertRaisesRegex(ProjectOverlayError, "directory must not be a symlink"):
            load_project_overlay(self.registry, alias / "project-overlay.json", repo_root=ROOT)
        with self.assertRaisesRegex(PersonaSettingsError, "directory must not be a symlink"):
            load_persona_layers(
                self.registry,
                global_path=ROOT / ".agents" / "persona-settings.json",
                personal_path=alias / "persona-settings.json",
                repo_root=ROOT,
            )
        with self.assertRaisesRegex(ProjectOverlayError, "directory must not be a symlink"):
            initialize_project_overlay(alias / "new-overlay.json", repo_root=ROOT)

    def test_file_symlink_is_rejected(self) -> None:
        link = self.private_dir / "linked.json"
        link.symlink_to(self.path)
        with self.assertRaisesRegex(ProjectOverlayError, "symlink"):
            load_project_overlay(self.registry, link, repo_root=ROOT)

    def test_rejects_non_regular_files_and_broad_directories(self) -> None:
        fifo = self.private_dir / "fifo.json"
        os.mkfifo(fifo, 0o600)
        with self.assertRaisesRegex(ProjectOverlayError, "regular file"):
            load_project_overlay(self.registry, fifo, repo_root=ROOT)
        self.private_dir.chmod(0o750)
        with self.assertRaisesRegex(ProjectOverlayError, "directory permissions"):
            load_project_overlay(self.registry, self.path, repo_root=ROOT)

    def test_file_swapped_after_open_is_not_read(self) -> None:
        real_open = os.open
        replacement = self.private_dir / "replacement.json"
        write_private(replacement, overlay({"sample-swapped": {"owner_agent_id": "reviewer", "status": "active"}}))
        replacement.chmod(0o644)

        def swapping_open(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            if path == self.path.name and "dir_fd" in kwargs:
                # Replace the pathname after the loader opened it; content must come
                # from the already-open descriptor, checked with fstat.
                os.replace(replacement, self.path)
            return descriptor

        with patch.object(private_files.os, "open", side_effect=swapping_open):
            loaded = load_project_overlay(self.registry, self.path, repo_root=ROOT)
        self.assertEqual(set(loaded.projects), {"sample-alpha"})
        with self.assertRaisesRegex(ProjectOverlayError, "owner-only"):
            load_project_overlay(self.registry, self.path, repo_root=ROOT)

    def test_owner_and_mode_come_from_the_open_descriptor(self) -> None:
        real_fstat = os.fstat
        seen: list[int] = []

        def recording_fstat(descriptor):
            seen.append(descriptor)
            return real_fstat(descriptor)

        with patch.object(private_files.os, "fstat", side_effect=recording_fstat):
            load_project_overlay(self.registry, self.path, repo_root=ROOT)
        self.assertGreaterEqual(len(seen), 2)


@unittest.skipUnless(private_files._descriptor_checks_supported(), "POSIX descriptor checks")
class AncestorSwapTests(unittest.TestCase):
    """An ancestor symlink re-pointed after the repository check must not redirect I/O."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.outside = self.base / "outside"
        self.repo = self.base / "repo"
        write_private(self.outside / "private" / "overlay.json", {"origin": "outside"})
        write_private(self.repo / "private" / "overlay.json", {"origin": "repo"})
        for directory in (self.outside, self.repo):
            directory.chmod(0o700)
        self.ancestor = self.base / "ancestor"
        self.ancestor.symlink_to(self.outside, target_is_directory=True)

    def repoint_before_first_open(self):
        real_open = os.open
        state = {"swapped": False}

        def swapping_open(path, flags, *args, **kwargs):
            if not state["swapped"]:
                state["swapped"] = True
                self.ancestor.unlink()
                self.ancestor.symlink_to(self.repo, target_is_directory=True)
            return real_open(path, flags, *args, **kwargs)

        return patch.object(private_files.os, "open", side_effect=swapping_open), state

    def read(self) -> object:
        return private_files.read_private_json(
            self.ancestor / "private" / "overlay.json", repo_root=self.repo, label="sample", error=ProjectOverlayError
        )

    def test_read_without_swap_reads_the_checked_target(self) -> None:
        self.assertEqual(self.read(), {"origin": "outside"})

    def test_read_after_ancestor_swap_never_returns_repository_content(self) -> None:
        patcher, state = self.repoint_before_first_open()
        with patcher:
            try:
                result = self.read()
            except ProjectOverlayError:
                result = None
        self.assertTrue(state["swapped"])
        self.assertNotEqual(result, {"origin": "repo"})
        self.assertIn(result, ({"origin": "outside"}, None))

    def test_init_after_ancestor_swap_never_writes_into_repository(self) -> None:
        patcher, state = self.repoint_before_first_open()
        with patcher:
            try:
                initialize_project_overlay(self.ancestor / "private" / "new-overlay.json", repo_root=self.repo)
            except ProjectOverlayError:
                pass
        self.assertTrue(state["swapped"])
        self.assertFalse((self.repo / "private" / "new-overlay.json").exists())

    def test_checked_directory_identity_mismatch_fails_closed(self) -> None:
        real_open = os.open
        target = self.outside / "private"
        moved = self.outside / "private-moved"
        state = {"done": False}

        def replacing_open(path, flags, *args, **kwargs):
            # Replace the checked directory with a different one of the same name
            # just before the walk reaches it.
            if path == "private" and "dir_fd" in kwargs and not state["done"]:
                state["done"] = True
                target.rename(moved)
                write_private(target / "overlay.json", {"origin": "replaced"})
            return real_open(path, flags, *args, **kwargs)

        with patch.object(private_files.os, "open", side_effect=replacing_open):
            with self.assertRaisesRegex(ProjectOverlayError, "changed during the check"):
                private_files.read_private_json(
                    target / "overlay.json", repo_root=self.repo, label="sample", error=ProjectOverlayError
                )

    def test_file_replaced_before_open_fails_closed(self) -> None:
        real_open = os.open
        target = self.outside / "private" / "overlay.json"
        state = {"done": False}

        def replacing_open(path, flags, *args, **kwargs):
            if path == target.name and "dir_fd" in kwargs and not state["done"]:
                state["done"] = True
                replacement = target.with_name("replacement.json")
                write_private(replacement, {"origin": "replaced"})
                os.replace(replacement, target)
            return real_open(path, flags, *args, **kwargs)

        with patch.object(private_files.os, "open", side_effect=replacing_open):
            with self.assertRaisesRegex(ProjectOverlayError, "changed during the check"):
                private_files.read_private_json(target, repo_root=self.repo, label="sample", error=ProjectOverlayError)


class IdentityErrorFailClosedTests(unittest.TestCase):
    """Only a missing path may fall back to spelling comparison."""

    def setUp(self) -> None:
        self.registry = registry()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.held = self.base / "held"
        self.held.mkdir()
        parsed = ProjectOverlay.from_dict(overlay({"sample-held": hold(str(self.held))}), registry=self.registry)
        self.merged = apply_project_overlay(self.registry, parsed, base_dir=ROOT)

    @unittest.skipUnless(os.name == "posix", "symlink cycles")
    def test_symlink_cycle_under_hold_root_fails_closed(self) -> None:
        (self.held / "loop").symlink_to(self.held / "loop")
        with self.assertRaises(RegistryValidationError):
            self.merged.project_for_working_directory(self.held / "loop" / "inner", base_dir=ROOT)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0, "needs non-root POSIX")
    def test_unreadable_directory_under_hold_root_fails_closed(self) -> None:
        locked = self.held / "locked"
        (locked / "inner").mkdir(parents=True)
        locked.chmod(0o000)
        self.addCleanup(locked.chmod, 0o700)
        with self.assertRaises(RegistryValidationError):
            self.merged.project_for_working_directory(locked / "inner", base_dir=ROOT)

    def test_missing_path_still_uses_spelling_fallback(self) -> None:
        self.assertEqual(
            self.merged.project_for_working_directory(self.held / "not-created" / "x", base_dir=ROOT), "sample-held"
        )

    @unittest.skipUnless(os.name == "posix", "symlink cycles")
    def test_hook_blocks_when_cwd_is_inside_a_symlink_cycle(self) -> None:
        (self.held / "loop").symlink_to(self.held / "loop")
        overlay_path = write_private(self.base / "config" / "project-overlay.json", overlay({"sample-held": hold(str(self.held))}))
        state = self.base / "state"
        state.mkdir(mode=0o700)
        with patch("adapters.codex.route_agent.tempfile.gettempdir", return_value=str(state)), patch.dict(
            os.environ, {PERSONA_ENV: str(self.base / "config" / "missing.json"), PROJECT_OVERLAY_ENV: str(overlay_path)}
        ):
            with self.assertRaises(HookError):
                handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "synthetic-loop",
                        "cwd": str(self.held / "loop" / "inner"),
                        "prompt": "please @agent:implementer",
                    }
                )


class ErrorPrivacyTests(unittest.TestCase):
    SECRETS = ("sample-secret-project", "/synthetic/private/path", "sample-secret-value")

    def setUp(self) -> None:
        self.registry = registry()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def invalid_overlays(self) -> list[dict]:
        secret_id, secret_path, secret_value = self.SECRETS
        valid = {"owner_agent_id": "implementer", "status": "active"}
        return [
            overlay({secret_id: {**valid, secret_path: secret_value}}),
            overlay({secret_id: {**valid, "status": secret_value}}),
            overlay({secret_id: {**valid, "path": secret_path + "/../" + secret_value}}),
            overlay({secret_id: {**valid, "repository": secret_value}}),
            overlay({secret_id: {**valid, "owner_agent_id": "sample-secret-value"}}),
            overlay({secret_id: {**valid, "permissions": [secret_value]}}),
            overlay({secret_id: {**valid, "runtime": {"provider": "orca", "agent": "codex", secret_path: 1}}}),
            {**overlay({secret_id: valid}), secret_path: secret_value},
            {"schema_version": "1.0", "scope": "personal"},
        ]

    def assert_no_secret(self, text: str) -> None:
        for secret in self.SECRETS:
            self.assertNotIn(secret, text)

    def test_exception_text_contains_no_private_keys_values_or_ids(self) -> None:
        for raw in self.invalid_overlays():
            with self.subTest(raw=list(raw)), self.assertRaises(ProjectOverlayError) as error:
                ProjectOverlay.from_dict(raw, registry=self.registry)
            self.assert_no_secret(str(error.exception))

    def test_cli_output_contains_no_private_data(self) -> None:
        overlay_path = self.base / "config" / "project-overlay.json"
        for raw in self.invalid_overlays():
            write_private(overlay_path, raw)
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), patch(
                "agent_runtime.cli.Path.cwd", return_value=ROOT
            ):
                code = cli_main(
                    [
                        "validate-project-overlay",
                        "--registry", str(ROOT / ".agents" / "registry.json"),
                        "--global-settings", str(ROOT / ".agents" / "persona-settings.json"),
                        "--personal-settings", str(self.base / "config" / "missing.json"),
                        "--project-overlay", str(overlay_path),
                    ]
                )
            with self.subTest(raw=list(raw)):
                self.assertEqual(code, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assert_no_secret(stderr.getvalue())

    def test_merge_collision_message_has_no_project_ids_or_paths(self) -> None:
        secret_id, secret_path, _ = self.SECRETS
        parsed = ProjectOverlay.from_dict(
            overlay({secret_id: active(secret_path), "sample-other": active(secret_path + "/")}),
            registry=self.registry,
        )
        with self.assertRaises(ProjectOverlayError) as error:
            apply_project_overlay(self.registry, parsed, base_dir=ROOT)
        self.assert_no_secret(str(error.exception))

    def test_registry_route_errors_do_not_echo_project_ids(self) -> None:
        raw = json.loads((ROOT / ".agents" / "registry.json").read_text(encoding="utf-8"))
        secret_id, secret_path, _ = self.SECRETS
        for route in ({"agent_id": "ghost"}, {"agent_id": "implementer", "path": "~/x"}, {"agent_id": "implementer", secret_path: 1}):
            raw["project_routes"] = {secret_id: route}
            with self.subTest(route=list(route)), self.assertRaises(RegistryValidationError) as error:
                AgentRegistry.from_dict(raw)
            self.assert_no_secret(str(error.exception))


if __name__ == "__main__":
    unittest.main()
