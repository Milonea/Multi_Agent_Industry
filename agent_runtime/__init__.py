"""Persona-independent agent registry, routing, and handoff contracts."""

from .registry import AgentDefinition, AgentRegistry, RegistryValidationError
from .routing import ConversationState, RouteDecision, resolve_route
from .handoff import HandoffOutcome, accept_handoff_result
from .persona import presentation_context

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
]
