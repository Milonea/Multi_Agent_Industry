"""Evidence-gated handoff acceptance and speaker ownership."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .registry import AgentRegistry
from .routing import ConversationState


@dataclass(frozen=True)
class HandoffOutcome:
    status: str
    reported_agent_id: str | None
    speaker_owner_id: str
    conversation_state: ConversationState
    reason: str | None = None


def accept_handoff_result(
    registry: AgentRegistry,
    result: Mapping[str, Any],
) -> HandoffOutcome:
    """Accept only evidence-backed execution; return final speaker ownership to the coordinator."""

    if not isinstance(result, Mapping):
        raise ValueError("handoff result must be an object")
    required = {"agent_id", "status", "runtime_executed", "summary", "evidence"}
    if set(result) != required:
        raise ValueError("handoff result has missing or unsupported fields")
    agent_id, status = result["agent_id"], result["status"]
    executed, summary, evidence = result["runtime_executed"], result["summary"], result["evidence"]
    if not isinstance(agent_id, str) or registry.get(agent_id) is None:
        return HandoffOutcome(
            "blocked", None, registry.default_agent_id,
            ConversationState(registry.default_agent_id), "reported agent is not registered",
        )
    if not isinstance(status, str) or status not in {"completed", "partial", "blocked", "failed"}:
        raise ValueError("handoff status is invalid")
    if not isinstance(executed, bool):
        raise ValueError("runtime_executed must be boolean")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("handoff summary must be non-empty")
    if not isinstance(evidence, list) or not all(isinstance(item, str) and item.strip() for item in evidence):
        raise ValueError("handoff evidence must be an array of non-empty strings")
    if not executed:
        return HandoffOutcome(
            "blocked", None, registry.default_agent_id,
            ConversationState(registry.default_agent_id),
            "runtime did not execute; no agent result can be attributed",
        )
    if status in {"completed", "partial"} and not evidence:
        return HandoffOutcome(
            "blocked", None, registry.default_agent_id,
            ConversationState(registry.default_agent_id),
            "executed result has no evidence",
        )
    return HandoffOutcome(
        status,
        agent_id,
        registry.default_agent_id,
        ConversationState(registry.default_agent_id),
    )
