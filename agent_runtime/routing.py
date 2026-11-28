"""Deterministic conversation routing and minimal handoff state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .registry import AgentDefinition, AgentRegistry, RegistryValidationError, _identifier


# Agents holding any of these permissions are implementation-capable and are refused
# while the active project is on hold.
IMPLEMENTATION_PERMISSIONS = frozenset({"write_workspace", "write_external", "deploy"})


HOLD_REFUSAL_REASON = "project is on hold; implementation-capable agents are refused"


def _implementation_capable(agent: AgentDefinition) -> bool:
    return bool(IMPLEMENTATION_PERMISSIONS & set(agent.permissions))


def _target_project_id(
    project_id: str | None,
    previous: "ConversationState | None",
    working_directory_project_id: str | None,
) -> str | None:
    """Active project precedence: explicit project, previous state, working directory."""

    if project_id is not None:
        return project_id
    if previous is not None and previous.active_project_id is not None:
        return previous.active_project_id
    return working_directory_project_id


def hold_refuses_any(
    registry: AgentRegistry,
    agent_ids: Iterable[str],
    *,
    project_ids: Iterable[str | None],
    previous: "ConversationState | None" = None,
    working_directory_project_id: str | None = None,
) -> bool:
    """Return True when any candidate agent is implementation-capable on a hold target.

    Callers that drop conflicting or ambiguous selections use this so that a request
    naming a write-capable agent on a hold project is still refused, even when that
    selection is otherwise ignored. Every combination of candidate project and agent is
    checked; unknown agents are skipped because they cannot run.
    """

    agents = [registry.get(agent_id) for agent_id in agent_ids]
    capable = [agent for agent in agents if agent is not None and _implementation_capable(agent)]
    if not capable:
        return False
    for project_id in project_ids:
        target = _target_project_id(project_id, previous, working_directory_project_id)
        if target is not None and registry.project_status(target) == "hold":
            return True
    return False


@dataclass(frozen=True)
class ConversationState:
    active_agent_id: str
    active_project_id: str | None = None
    supporting_agent_ids: tuple[str, ...] = field(default_factory=tuple)
    version: str = "1.0"

    def __post_init__(self) -> None:
        _identifier(self.active_agent_id, "active_agent_id")
        if self.active_project_id is not None:
            _identifier(self.active_project_id, "active_project_id")
        if not isinstance(self.supporting_agent_ids, tuple) or not all(
            isinstance(item, str) for item in self.supporting_agent_ids
        ):
            raise ValueError("supporting_agent_ids must be a tuple of strings")
        if len(set(self.supporting_agent_ids)) != len(self.supporting_agent_ids):
            raise ValueError("supporting_agent_ids must be unique")
        for item in self.supporting_agent_ids:
            _identifier(item, "supporting_agent_id")
        if self.active_agent_id in self.supporting_agent_ids:
            raise ValueError("active agent cannot also be a supporting agent")
        if self.version != "1.0":
            raise ValueError("unsupported conversation state version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "active_agent_id": self.active_agent_id,
            "active_project_id": self.active_project_id,
            "supporting_agent_ids": list(self.supporting_agent_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ConversationState":
        fields = {"version", "active_agent_id", "active_project_id", "supporting_agent_ids"}
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError("conversation state has missing or unsupported fields")
        supporting = value["supporting_agent_ids"]
        if not isinstance(supporting, list):
            raise ValueError("supporting_agent_ids must be an array")
        return cls(
            version=value["version"],
            active_agent_id=value["active_agent_id"],
            active_project_id=value["active_project_id"],
            supporting_agent_ids=tuple(supporting),
        )


@dataclass(frozen=True)
class RouteDecision:
    status: str
    state: ConversationState | None
    route_source: str
    reason: str | None = None

    @property
    def allowed(self) -> bool:
        return self.status == "routed" and self.state is not None


def resolve_route(
    registry: AgentRegistry,
    *,
    explicit_agent_id: str | None = None,
    project_id: str | None = None,
    requested_role: str | None = None,
    supporting_agent_ids: tuple[str, ...] = tuple(),
    previous: ConversationState | None = None,
    working_directory_project_id: str | None = None,
) -> RouteDecision:
    """Resolve a route without executing an agent or elevating its permissions.

    Precedence, first match wins:

    1. explicit agent (``@agent:<id>``)
    2. explicit role (``role:<role>``), only when exactly one enabled agent has it
    3. explicit project (``project:<id>``) route
    4. continuity from the previous state of the same conversation scope
    5. working-directory project: the registered project whose declared path
       contains the runtime working directory (resolved by the caller)
    6. fallback to the registry default agent (``route_source="coordinator"``)

    The active project follows the same order: explicit project, previous state,
    then working-directory project. Nothing here grants permissions.

    Project status (private overlay only) narrows the result: an ``inventory_only``
    project is never routed, and while the active project is on ``hold`` its route
    selects the hold agent and any implementation-capable agent (``write_workspace``,
    ``write_external`` or ``deploy``) is refused, including explicit agents, roles,
    continuity and supporting agents.
    """

    if not isinstance(registry, AgentRegistry):
        return RouteDecision("blocked", previous, "invalid_registry", "registry is unavailable")
    for field_name, value in (
        ("explicit_agent_id", explicit_agent_id),
        ("project_id", project_id),
        ("requested_role", requested_role),
        ("working_directory_project_id", working_directory_project_id),
    ):
        if value is not None:
            try:
                _identifier(value, field_name)
            except RegistryValidationError as exc:
                return RouteDecision("blocked", previous, "invalid_request", str(exc))
    if not isinstance(supporting_agent_ids, tuple) or not all(
        isinstance(item, str) for item in supporting_agent_ids
    ):
        return RouteDecision("blocked", previous, "invalid_request", "supporting agents are invalid")

    if project_id is not None and project_id not in registry.project_routes:
        if registry.project_status(project_id) == "inventory_only":
            return RouteDecision("blocked", previous, "project", "project is inventory-only and is not routed")
        return RouteDecision("blocked", previous, "project", "project is not registered")
    if (
        working_directory_project_id is not None
        and working_directory_project_id not in registry.project_routes
    ):
        return RouteDecision(
            "blocked", previous, "working_directory", "working-directory project is not registered"
        )

    target_project = _target_project_id(project_id, previous, working_directory_project_id)
    target_status = registry.project_status(target_project) if target_project is not None else None
    if target_status == "inventory_only":
        return RouteDecision("blocked", previous, "project", "project is inventory-only and is not routed")
    on_hold = target_status == "hold"

    target_agent: str | None = None
    source = "coordinator"
    if explicit_agent_id is not None:
        target_agent, source = explicit_agent_id, "explicit_agent"
    elif requested_role is not None:
        matches = registry.for_role(requested_role)
        if len(matches) != 1:
            reason = "role has no enabled runtime binding" if not matches else "role maps to multiple agents; choose an agent_id"
            return RouteDecision("blocked", previous, "role", reason)
        target_agent, source = matches[0].agent_id, "role"
    elif project_id is not None:
        target_agent, source = registry.project_routes[project_id], "project"
    elif previous is not None:
        target_agent, source = previous.active_agent_id, "continuity"
    elif working_directory_project_id is not None:
        target_agent = registry.project_routes[working_directory_project_id]
        source = "working_directory"
    else:
        target_agent = registry.default_agent_id

    agent = registry.get(target_agent)
    if agent is None:
        return RouteDecision("blocked", previous, source, "agent is not registered")
    if not agent.runtime_enabled:
        return RouteDecision("blocked", previous, source, "agent runtime binding is disabled")
    if agent.runtime_provider is None or agent.runtime_agent_name is None:
        return RouteDecision("blocked", previous, source, "agent runtime binding is incomplete")
    if on_hold and _implementation_capable(agent):
        return RouteDecision(
            "blocked", previous, source, HOLD_REFUSAL_REASON
        )

    if len(supporting_agent_ids) != len(set(supporting_agent_ids)):
        return RouteDecision("blocked", previous, source, "supporting agents contain duplicates")
    supports: list[str] = []
    for support_id in supporting_agent_ids:
        support = registry.get(support_id)
        if support is None or not support.runtime_enabled:
            return RouteDecision("blocked", previous, source, f"supporting agent is unavailable: {support_id}")
        if support_id == target_agent:
            return RouteDecision("blocked", previous, source, "active agent cannot also be supporting")
        if on_hold and _implementation_capable(support):
            return RouteDecision(
                "blocked", previous, source, HOLD_REFUSAL_REASON
            )
        supports.append(support_id)

    state = ConversationState(
        active_agent_id=target_agent,
        active_project_id=target_project,
        supporting_agent_ids=tuple(supports),
    )
    return RouteDecision("routed", state, source)
