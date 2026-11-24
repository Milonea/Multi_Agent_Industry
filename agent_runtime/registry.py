"""Strict agent definitions with independent role, permission, and style fields."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping


_ID_RE = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
_PERMISSIONS = frozenset(
    {"read_workspace", "write_workspace", "read_external", "write_external", "deploy"}
)
_REGISTRY_KEYS = frozenset(
    {"schema_version", "default_agent_id", "limits", "agents", "project_routes"}
)
_AGENT_KEYS = frozenset(
    {
        "agent_id",
        "role",
        "capabilities",
        "responsibilities",
        "permissions",
        "validation_requirements",
        "runtime_binding",
        "escalation_policy",
        "persona",
    }
)


class RegistryValidationError(ValueError):
    """Raised when a registry is incomplete or grants an unsafe binding."""


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise RegistryValidationError(f"{field} must be a lowercase identifier")
    return value


def _string_list(value: Any, field: str, *, unique: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise RegistryValidationError(f"{field} must be an array of non-empty strings")
    if unique and len(value) != len(set(value)):
        raise RegistryValidationError(f"{field} must not contain duplicates")
    return tuple(value)


@dataclass(frozen=True)
class AgentDefinition:
    agent_id: str
    role: str
    capabilities: tuple[str, ...]
    responsibilities: tuple[str, ...]
    permissions: tuple[str, ...]
    validation_requirements: tuple[str, ...]
    runtime_provider: str | None
    runtime_agent_name: str | None
    runtime_enabled: bool
    escalation_policy: str
    persona: Mapping[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentDefinition":
        if not isinstance(data, Mapping):
            raise RegistryValidationError("agent must be an object")
        unexpected = sorted(str(key) for key in data if key not in _AGENT_KEYS)
        if unexpected:
            raise RegistryValidationError("agent contains unsupported fields: " + ", ".join(unexpected))
        required = _AGENT_KEYS - {"persona"}
        missing = sorted(required - set(data))
        if missing:
            raise RegistryValidationError("agent is missing fields: " + ", ".join(missing))

        agent_id = _identifier(data["agent_id"], "agent_id")
        role = _identifier(data["role"], "role")
        capabilities = _string_list(data["capabilities"], "capabilities", unique=True)
        responsibilities = _string_list(data["responsibilities"], "responsibilities")
        permissions = _string_list(data["permissions"], "permissions", unique=True)
        unknown_permissions = sorted(set(permissions) - _PERMISSIONS)
        if unknown_permissions:
            raise RegistryValidationError("unknown permission: " + ", ".join(unknown_permissions))
        validation_requirements = _string_list(
            data["validation_requirements"], "validation_requirements"
        )

        binding = data["runtime_binding"]
        if not isinstance(binding, Mapping) or set(binding) != {"provider", "agent_name", "enabled"}:
            raise RegistryValidationError("runtime_binding must contain provider, agent_name, and enabled")
        provider, runtime_agent_name = binding["provider"], binding["agent_name"]
        enabled = binding["enabled"]
        if provider is not None and (not isinstance(provider, str) or not provider.strip()):
            raise RegistryValidationError("runtime_binding.provider must be non-empty or null")
        if runtime_agent_name is not None and (
            not isinstance(runtime_agent_name, str) or not runtime_agent_name.strip()
        ):
            raise RegistryValidationError("runtime_binding.agent_name must be non-empty or null")
        if not isinstance(enabled, bool):
            raise RegistryValidationError("runtime_binding.enabled must be boolean")
        if enabled and (provider is None or runtime_agent_name is None):
            raise RegistryValidationError("enabled runtime binding requires a provider and agent name")

        escalation = data["escalation_policy"]
        if not isinstance(escalation, str) or not escalation.strip():
            raise RegistryValidationError("escalation_policy must be non-empty")
        persona = data.get("persona")
        if persona is not None:
            persona = validate_presentation_profile(persona)
        return cls(
            agent_id=agent_id,
            role=role,
            capabilities=capabilities,
            responsibilities=responsibilities,
            permissions=permissions,
            validation_requirements=validation_requirements,
            runtime_provider=provider,
            runtime_agent_name=runtime_agent_name,
            runtime_enabled=enabled,
            escalation_policy=escalation,
            persona=persona,
        )


def validate_presentation_profile(profile: Any) -> dict[str, Any]:
    """Accept style metadata only; authorization and routing fields are rejected."""

    allowed = {"profile_id", "locale", "style", "tone_tags"}
    if not isinstance(profile, Mapping):
        raise RegistryValidationError("persona must be a presentation profile object")
    unexpected = sorted(str(key) for key in profile if key not in allowed)
    if unexpected:
        raise RegistryValidationError(
            "persona is presentation-only and contains unsupported fields: " + ", ".join(unexpected)
        )
    if set(profile) != allowed:
        raise RegistryValidationError("persona profile is incomplete")
    profile_id = _identifier(profile["profile_id"], "persona.profile_id")
    locale = profile["locale"]
    style = profile["style"]
    if not isinstance(locale, str) or len(locale.strip()) < 2:
        raise RegistryValidationError("persona.locale must be non-empty")
    if not isinstance(style, str) or len(style) > 500:
        raise RegistryValidationError("persona.style must be a string of at most 500 characters")
    tone_tags = _string_list(profile["tone_tags"], "persona.tone_tags", unique=True)
    return {
        "profile_id": profile_id,
        "locale": locale.strip(),
        "style": style,
        "tone_tags": list(tone_tags),
    }


class AgentRegistry:
    def __init__(
        self,
        *,
        default_agent_id: str,
        agents: Mapping[str, AgentDefinition],
        max_registered_agents: int,
        max_concurrent_agents: int,
        project_routes: Mapping[str, str],
    ) -> None:
        self.default_agent_id = default_agent_id
        self.agents = dict(agents)
        self.max_registered_agents = max_registered_agents
        self.max_concurrent_agents = max_concurrent_agents
        self.project_routes = dict(project_routes)
        if self.default_agent_id not in self.agents:
            raise RegistryValidationError("default_agent_id is not registered")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentRegistry":
        if not isinstance(data, Mapping):
            raise RegistryValidationError("registry must be an object")
        unexpected = sorted(str(key) for key in data if key not in _REGISTRY_KEYS)
        if unexpected:
            raise RegistryValidationError("registry contains unsupported fields: " + ", ".join(unexpected))
        missing = sorted(_REGISTRY_KEYS - set(data))
        if missing:
            raise RegistryValidationError("registry is missing fields: " + ", ".join(missing))
        if data["schema_version"] != "1.0":
            raise RegistryValidationError("unsupported registry schema version")
        limits = data["limits"]
        if not isinstance(limits, Mapping) or set(limits) != {
            "max_registered_agents", "max_concurrent_agents", "external_write_approval_required"
        }:
            raise RegistryValidationError("limits are incomplete")
        maximum = limits["max_registered_agents"]
        concurrent = limits["max_concurrent_agents"]
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 1000:
            raise RegistryValidationError("max_registered_agents must be from 1 to 1000")
        if isinstance(concurrent, bool) or not isinstance(concurrent, int) or concurrent < 1:
            raise RegistryValidationError("max_concurrent_agents must be a positive integer")
        if concurrent > maximum or concurrent > 64:
            raise RegistryValidationError("max_concurrent_agents exceeds the configured registration cap")
        if limits["external_write_approval_required"] is not True:
            raise RegistryValidationError("external writes must require explicit approval")
        raw_agents = data["agents"]
        if not isinstance(raw_agents, list) or not raw_agents or len(raw_agents) > maximum:
            raise RegistryValidationError("agents must be a non-empty array within max_registered_agents")
        agents: dict[str, AgentDefinition] = {}
        for raw in raw_agents:
            agent = AgentDefinition.from_dict(raw)
            if agent.agent_id in agents:
                raise RegistryValidationError(f"duplicate agent_id: {agent.agent_id}")
            if "write_external" in agent.permissions and not limits["external_write_approval_required"]:
                raise RegistryValidationError("external write permission lacks an approval policy")
            agents[agent.agent_id] = agent

        default_agent_id = _identifier(data["default_agent_id"], "default_agent_id")
        routes = data["project_routes"]
        if not isinstance(routes, Mapping):
            raise RegistryValidationError("project_routes must be an object")
        project_routes: dict[str, str] = {}
        for project, agent_id in routes.items():
            project = _identifier(project, "project_id")
            agent_id = _identifier(agent_id, f"project_routes.{project}")
            if agent_id not in agents:
                raise RegistryValidationError(f"project route references an unknown agent: {agent_id}")
            project_routes[project] = agent_id
        return cls(
            default_agent_id=default_agent_id,
            agents=agents,
            max_registered_agents=maximum,
            max_concurrent_agents=concurrent,
            project_routes=project_routes,
        )

    def get(self, agent_id: str) -> AgentDefinition | None:
        return self.agents.get(agent_id)

    def for_role(self, role: str, *, enabled_only: bool = True) -> tuple[AgentDefinition, ...]:
        matches = [agent for agent in self.agents.values() if agent.role == role]
        if enabled_only:
            matches = [agent for agent in matches if agent.runtime_enabled]
        return tuple(sorted(matches, key=lambda agent: agent.agent_id))
