from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from agent_runtime.handoff import accept_handoff_result
from agent_runtime.persona import presentation_context
from agent_runtime.registry import AgentRegistry, RegistryValidationError
from agent_runtime.routing import ConversationState, resolve_route


ROOT = Path(__file__).resolve().parents[1]


def load_registry_data() -> dict:
    return json.loads((ROOT / ".agents" / "registry.json").read_text(encoding="utf-8"))


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = load_registry_data()
        self.registry = AgentRegistry.from_dict(self.raw)

    def test_registry_loads_without_registered_projects(self) -> None:
        self.assertEqual(len(self.registry.agents), 10)
        self.assertEqual(self.registry.project_routes, {})
        self.assertEqual(self.registry.max_registered_agents, 100)
        self.assertEqual(self.registry.max_concurrent_agents, 4)

    def test_direct_agent_role_and_continuity_routes(self) -> None:
        direct = resolve_route(self.registry, explicit_agent_id="reviewer")
        self.assertTrue(direct.allowed)
        self.assertEqual(direct.route_source, "explicit_agent")
        self.assertEqual(direct.state.active_agent_id, "reviewer")

        role = resolve_route(self.registry, requested_role="validator")
        self.assertEqual(role.state.active_agent_id, "validator")
        self.assertEqual(role.route_source, "role")

        continuation = resolve_route(self.registry, previous=direct.state)
        self.assertEqual(continuation.state.active_agent_id, "reviewer")
        self.assertEqual(continuation.route_source, "continuity")

    def test_project_switch_and_project_routes(self) -> None:
        configured = copy.deepcopy(self.raw)
        configured["project_routes"] = {"sample-project": "implementer"}
        registry = AgentRegistry.from_dict(configured)
        decision = resolve_route(registry, project_id="sample-project")
        self.assertEqual(decision.state.active_agent_id, "implementer")
        self.assertEqual(decision.state.active_project_id, "sample-project")
        self.assertEqual(decision.route_source, "project")
        self.assertEqual(
            resolve_route(registry, project_id="unregistered-project").status,
            "blocked",
        )

    def test_unregistered_or_disabled_agent_fails_closed(self) -> None:
        self.assertEqual(
            resolve_route(self.registry, explicit_agent_id="unknown-agent").status,
            "blocked",
        )
        configured = copy.deepcopy(self.raw)
        configured["agents"][1]["runtime_binding"]["enabled"] = False
        registry = AgentRegistry.from_dict(configured)
        self.assertEqual(resolve_route(registry, explicit_agent_id="architect").status, "blocked")

    def test_multiple_instances_of_one_role_are_supported_but_not_ambiguous(self) -> None:
        configured = copy.deepcopy(self.raw)
        extra = copy.deepcopy(configured["agents"][2])
        extra["agent_id"] = "implementer_secondary"
        extra["runtime_binding"]["agent_name"] = "implementer_secondary"
        configured["agents"].append(extra)
        registry = AgentRegistry.from_dict(configured)
        self.assertEqual(len(registry.for_role("implementer")), 2)
        self.assertEqual(resolve_route(registry, requested_role="implementer").status, "blocked")
        self.assertEqual(
            resolve_route(registry, explicit_agent_id="implementer_secondary").state.active_agent_id,
            "implementer_secondary",
        )

    def test_presentation_profile_cannot_grant_permissions_or_change_role(self) -> None:
        agent = self.registry.get("implementer")
        original_permissions = agent.permissions
        context = presentation_context(
            agent,
            {
                "profile_id": "neutral-example",
                "locale": "ko-KR",
                "style": "짧고 근거 중심으로 설명한다",
                "tone_tags": ["clear"],
            },
        )
        self.assertIn("presentation preference only", context)
        self.assertEqual(agent.role, "implementer")
        self.assertEqual(agent.permissions, original_permissions)
        self.assertNotIn("write_external", agent.permissions)

        with self.assertRaises(RegistryValidationError):
            presentation_context(
                agent,
                {
                    "profile_id": "unsafe-style",
                    "locale": "en-US",
                    "style": "style",
                    "tone_tags": [],
                    "permissions": ["write_external"],
                },
            )

    def test_handoff_requires_real_execution_and_evidence(self) -> None:
        no_run = accept_handoff_result(
            self.registry,
            {
                "agent_id": "implementer",
                "status": "completed",
                "runtime_executed": False,
                "summary": "not actually run",
                "evidence": [],
            },
        )
        self.assertEqual(no_run.status, "blocked")
        self.assertIsNone(no_run.reported_agent_id)
        self.assertEqual(no_run.speaker_owner_id, "coordinator")

        no_evidence = accept_handoff_result(
            self.registry,
            {
                "agent_id": "implementer",
                "status": "completed",
                "runtime_executed": True,
                "summary": "claimed completion",
                "evidence": [],
            },
        )
        self.assertEqual(no_evidence.status, "blocked")
        self.assertIsNone(no_evidence.reported_agent_id)

        accepted = accept_handoff_result(
            self.registry,
            {
                "agent_id": "implementer",
                "status": "completed",
                "runtime_executed": True,
                "summary": "validated change",
                "evidence": ["tests/test_agent_runtime.py::test_handoff"],
            },
        )
        self.assertEqual(accepted.status, "completed")
        self.assertEqual(accepted.reported_agent_id, "implementer")
        self.assertEqual(accepted.speaker_owner_id, "coordinator")

    def test_state_round_trip_is_strict_and_minimal(self) -> None:
        state = ConversationState(
            active_agent_id="reviewer",
            active_project_id="sample-project",
            supporting_agent_ids=("researcher",),
        )
        self.assertEqual(ConversationState.from_dict(state.to_dict()), state)
        with self.assertRaises(ValueError):
            ConversationState.from_dict({**state.to_dict(), "prompt": "must not persist"})

    def test_registry_rejects_permission_or_limit_escalation(self) -> None:
        configured = copy.deepcopy(self.raw)
        configured["limits"]["external_write_approval_required"] = False
        with self.assertRaises(RegistryValidationError):
            AgentRegistry.from_dict(configured)

    def test_release_incident_and_fallback_roles_are_bounded(self) -> None:
        self.assertEqual(self.registry.get("release_manager").permissions, ("read_workspace",))
        self.assertEqual(self.registry.get("incident_commander").permissions, ("read_workspace",))
        self.assertEqual(self.registry.get("fallback_investigator").permissions, ("read_workspace",))
        self.assertTrue(resolve_route(self.registry, requested_role="release_manager").allowed)
        self.assertTrue(resolve_route(self.registry, requested_role="incident_commander").allowed)
        configured = copy.deepcopy(self.raw)
        configured["limits"]["max_concurrent_agents"] = 101
        with self.assertRaises(RegistryValidationError):
            AgentRegistry.from_dict(configured)


if __name__ == "__main__":
    unittest.main()
