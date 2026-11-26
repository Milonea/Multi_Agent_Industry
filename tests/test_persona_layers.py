from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from agent_runtime.persona_layers import (
    PersonaLayers,
    PersonaSettings,
    PersonaSettingsError,
    initialize_personal_settings,
    load_persona_layers,
)
from agent_runtime.registry import AgentRegistry


ROOT = Path(__file__).resolve().parents[1]


def profile(profile_id: str, style: str) -> dict[str, object]:
    return {
        "profile_id": profile_id,
        "locale": "ko-KR",
        "style": style,
        "tone_tags": ["concise"],
    }


def settings(
    scope: str,
    *,
    profiles: dict[str, dict[str, object]] | None = None,
    default_profile_id: str | None = None,
    agents: dict[str, str] | None = None,
    roles: dict[str, str] | None = None,
) -> PersonaSettings:
    raw: dict[str, object] = {
        "schema_version": "1.0",
        "scope": scope,
        "profiles": profiles or {},
        "default_profile_id": default_profile_id,
        "assignments": {"agents": agents or {}, "roles": roles or {}},
    }
    return PersonaSettings.from_dict(raw, expected_scope=scope)


class PersonaLayersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry_data = json.loads((ROOT / ".agents/registry.json").read_text(encoding="utf-8"))
        self.registry = AgentRegistry.from_dict(self.registry_data)

    def test_personal_agent_assignment_has_highest_precedence(self) -> None:
        global_settings = settings(
            "global",
            profiles={"global_agent": profile("global_agent", "shared agent style")},
            agents={"reviewer": "global_agent"},
        )
        personal_settings = settings(
            "personal",
            profiles={"private_agent": profile("private_agent", "private agent style")},
            agents={"reviewer": "private_agent"},
        )

        resolved = PersonaLayers(self.registry, global_settings, personal_settings).resolve(
            self.registry.get("reviewer")
        )

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.source, "personal-agent")
        self.assertEqual(resolved.profile["style"], "private agent style")

    def test_personal_role_overrides_global_agent_and_registry_persona(self) -> None:
        registry_data = json.loads(json.dumps(self.registry_data))
        reviewer = next(agent for agent in registry_data["agents"] if agent["agent_id"] == "reviewer")
        reviewer["persona"] = profile("embedded_style", "embedded registry style")
        registry = AgentRegistry.from_dict(registry_data)
        global_settings = settings(
            "global",
            profiles={
                "global_agent": profile("global_agent", "shared agent style"),
                "global_role": profile("global_role", "shared role style"),
            },
            agents={"reviewer": "global_agent"},
            roles={"reviewer": "global_role"},
        )
        personal_settings = settings(
            "personal",
            profiles={"private_role": profile("private_role", "private role style")},
            roles={"reviewer": "private_role"},
        )

        resolved = PersonaLayers(registry, global_settings, personal_settings).resolve(
            registry.get("reviewer")
        )

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.source, "personal-role")
        self.assertEqual(resolved.profile["style"], "private role style")

    def test_global_agent_precedes_registry_persona_and_global_role(self) -> None:
        registry_data = json.loads(json.dumps(self.registry_data))
        reviewer = next(agent for agent in registry_data["agents"] if agent["agent_id"] == "reviewer")
        reviewer["persona"] = profile("embedded_style", "embedded registry style")
        registry = AgentRegistry.from_dict(registry_data)
        global_settings = settings(
            "global",
            profiles={
                "global_agent": profile("global_agent", "shared agent style"),
                "global_role": profile("global_role", "shared role style"),
            },
            agents={"reviewer": "global_agent"},
            roles={"reviewer": "global_role"},
        )

        resolved = PersonaLayers(registry, global_settings).resolve(registry.get("reviewer"))

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.source, "global-agent")

    def test_personal_default_precedes_shared_agent_assignment(self) -> None:
        global_settings = settings(
            "global",
            profiles={"global_agent": profile("global_agent", "shared agent style")},
            agents={"reviewer": "global_agent"},
        )
        personal_settings = settings(
            "personal",
            profiles={"private_default": profile("private_default", "personal default style")},
            default_profile_id="private_default",
        )

        resolved = PersonaLayers(self.registry, global_settings, personal_settings).resolve(
            self.registry.get("reviewer")
        )

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.source, "personal-default")
        self.assertEqual(resolved.profile["style"], "personal default style")

    def test_registry_persona_precedes_global_role_and_global_default(self) -> None:
        registry_data = json.loads(json.dumps(self.registry_data))
        reviewer = next(agent for agent in registry_data["agents"] if agent["agent_id"] == "reviewer")
        reviewer["persona"] = profile("embedded_style", "embedded registry style")
        registry = AgentRegistry.from_dict(registry_data)
        global_settings = settings(
            "global",
            profiles={
                "global_role": profile("global_role", "shared role style"),
                "global_default": profile("global_default", "shared default style"),
            },
            default_profile_id="global_default",
            roles={"reviewer": "global_role"},
        )

        resolved = PersonaLayers(registry, global_settings).resolve(registry.get("reviewer"))

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.source, "registry-agent")
        self.assertEqual(resolved.profile["style"], "embedded registry style")

    def test_global_role_precedes_default(self) -> None:
        global_settings = settings(
            "global",
            profiles={
                "global_role": profile("global_role", "shared role style"),
                "global_default": profile("global_default", "shared default style"),
            },
            default_profile_id="global_default",
            roles={"reviewer": "global_role"},
        )

        resolved = PersonaLayers(self.registry, global_settings).resolve(self.registry.get("reviewer"))

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.source, "global-role")

    def test_global_default_is_last_fallback(self) -> None:
        global_settings = settings(
            "global",
            profiles={"global_default": profile("global_default", "shared default style")},
            default_profile_id="global_default",
        )

        resolved = PersonaLayers(self.registry, global_settings).resolve(self.registry.get("reviewer"))

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.source, "global-default")

    def test_rejects_authorization_fields_and_cross_scope_id_collisions(self) -> None:
        with self.assertRaises(PersonaSettingsError):
            settings(
                "personal",
                profiles={
                    "private_profile": {
                        **profile("private_profile", "style"),
                        "permissions": ["write_external"],
                    }
                },
            )

        with self.assertRaises(PersonaSettingsError):
            PersonaLayers(
                self.registry,
                settings("global", profiles={"same_profile": profile("same_profile", "shared")}),
                settings("personal", profiles={"same_profile": profile("same_profile", "private")}),
            )

    def test_personal_file_must_be_outside_repo_and_owner_only(self) -> None:
        with self.assertRaisesRegex(PersonaSettingsError, "outside the repository"):
            load_persona_layers(
                self.registry,
                global_path=ROOT / ".agents/persona-settings.json",
                personal_path=ROOT / ".agents/persona-settings.personal.json",
                repo_root=ROOT,
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "persona-settings.json"
            path.write_text(json.dumps({"bad": "settings"}), encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaisesRegex(PersonaSettingsError, "owner-only"):
                load_persona_layers(
                    self.registry,
                    global_path=ROOT / ".agents/persona-settings.json",
                    personal_path=path,
                    repo_root=ROOT,
                )

    @unittest.skipUnless(os.name == "posix", "POSIX permission modes are platform-specific")
    def test_initializer_creates_private_empty_file_and_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings" / "persona-settings.json"
            created = initialize_personal_settings(path, repo_root=ROOT)

            self.assertEqual(created.resolve(), path.resolve())
            self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(created.parent.stat().st_mode), 0o700)
            initialized = PersonaSettings.from_dict(
                json.loads(created.read_text(encoding="utf-8")), expected_scope="personal"
            )
            self.assertEqual(initialized.scope, "personal")

            with self.assertRaisesRegex(PersonaSettingsError, "already exists"):
                initialize_personal_settings(path, repo_root=ROOT)


if __name__ == "__main__":
    unittest.main()
