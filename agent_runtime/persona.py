"""Optional presentation helpers. Profiles cannot alter routing or permissions."""

from __future__ import annotations

from typing import Any, Mapping

from .registry import AgentDefinition, RegistryValidationError, validate_presentation_profile


def presentation_context(
    agent: AgentDefinition,
    profile: Mapping[str, Any] | None = None,
) -> str:
    """Build a short style hint while leaving the agent's role and permissions intact."""

    if not isinstance(agent, AgentDefinition):
        raise RegistryValidationError("agent definition is required")
    selected = validate_presentation_profile(profile) if profile is not None else agent.persona
    if selected is None:
        return ""
    style = selected["style"].strip()
    if not style:
        return ""
    tags = ", ".join(selected["tone_tags"])
    suffix = f" Tone tags: {tags}." if tags else ""
    return f"presentation preference only ({selected['locale']}): {style}.{suffix}"
