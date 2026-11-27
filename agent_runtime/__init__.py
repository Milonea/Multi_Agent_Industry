"""Agent registry, routing, handoff, and optional presentation settings."""

from .registry import AgentDefinition, AgentRegistry, RegistryValidationError
from .routing import ConversationState, RouteDecision, resolve_route
from .handoff import HandoffOutcome, accept_handoff_result
from .persona import presentation_context, presentation_policy_context
from .project_overlay import (
    OverlayProject,
    ProjectOverlay,
    ProjectOverlayError,
    apply_project_overlay,
    initialize_project_overlay,
    load_project_overlay,
)
from .persona_layers import (
    PersonaLayers,
    PersonaSettings,
    PersonaSettingsError,
    initialize_personal_settings,
    load_persona_layers,
    merge_presentation_policies,
    validate_presentation_policy,
)

__all__ = [
    "AgentDefinition",
    "AgentRegistry",
    "RegistryValidationError",
    "ConversationState",
    "RouteDecision",
    "resolve_route",
    "HandoffOutcome",
    "accept_handoff_result",
    "presentation_context",
    "presentation_policy_context",
    "PersonaLayers",
    "PersonaSettings",
    "PersonaSettingsError",
    "initialize_personal_settings",
    "load_persona_layers",
    "merge_presentation_policies",
    "validate_presentation_policy",
    "OverlayProject",
    "ProjectOverlay",
    "ProjectOverlayError",
    "apply_project_overlay",
    "initialize_project_overlay",
    "load_project_overlay",
]
