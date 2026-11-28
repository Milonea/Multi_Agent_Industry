"""Codex UserPromptSubmit adapter for explicit, registry-backed routing.

The hook stores only route identifiers in a private local state file scoped by
an opaque hash of the session identifier and working directory. It never stores
prompt text, transcripts, tool output, paths, or credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent_runtime.registry import AgentRegistry, RegistryValidationError, _identifier  # noqa: E402
from agent_runtime.persona import presentation_context, presentation_policy_context  # noqa: E402
from agent_runtime.persona_layers import PersonaSettingsError, load_persona_layers  # noqa: E402
from agent_runtime.project_overlay import (  # noqa: E402
    ProjectOverlayError,
    apply_project_overlay,
    load_project_overlay,
)
from agent_runtime.routing import (  # noqa: E402
    HOLD_REFUSAL_REASON,
    ConversationState,
    hold_refuses_any,
    resolve_route,
)


# The prompt is split once into chunks on whitespace and the token separators. Each
# chunk yields at most one token: the first token prefix in it, with the rest of the
# chunk as its value. Text inside one token's value is therefore never parsed as
# another token, whatever kind it spells.
_TOKEN_CHUNK = re.compile(r"[^\s,;]+")
_TOKEN_START = re.compile(r"(?<![\w.-])(support:agent:|@agent:|project:|role:)")
_TOKEN_KINDS = {"@agent:": "agent", "project:": "project", "role:": "role", "support:agent:": "support"}
# Only the first distinct values of each kind take part in selection; the hold check
# still sees every registered value.
_MAX_TOKEN_VALUES_PER_KIND = 64
_MAX_INPUT_BYTES = 1_000_000

_WARNINGS = {
    "agent_invalid": (
        "Routing warning: an agent selection token was ignored because its value is not a valid identifier."
    ),
    "agent_unavailable": (
        "Routing warning: an agent selection token was ignored because the requested agent is unavailable."
    ),
    "agent_multiple": (
        "Routing warning: agent selection tokens were ignored because they specify multiple distinct values."
    ),
    "project_invalid": (
        "Routing warning: a project selection token was ignored because its value is not a valid identifier."
    ),
    "project_unavailable": (
        "Routing warning: a project selection token was ignored because the requested project is unavailable."
    ),
    "project_multiple": (
        "Routing warning: project selection tokens were ignored because they specify multiple distinct values."
    ),
    "role_invalid": (
        "Routing warning: a role selection token was ignored because its value is not a valid identifier."
    ),
    "role_unavailable": (
        "Routing warning: a role selection token was ignored because no enabled agent is available for the role."
    ),
    "role_ambiguous": (
        "Routing warning: a role selection token was ignored because the role maps to multiple enabled agents."
    ),
    "role_multiple": (
        "Routing warning: role selection tokens were ignored because they specify multiple distinct values."
    ),
    "support_invalid": (
        "Routing warning: a supporting agent token was ignored because its value is not a valid identifier."
    ),
    "support_unavailable": (
        "Routing warning: a supporting agent token was ignored because the requested agent is unavailable."
    ),
    "support_duplicate": (
        "Routing warning: a supporting agent token was ignored because it duplicates another supporting agent token."
    ),
    "support_active": (
        "Routing warning: a supporting agent token was ignored because the active agent cannot also be supporting."
    ),
    "agent_limit": (
        "Routing warning: agent selection tokens beyond the supported number of distinct values were ignored."
    ),
    "project_limit": (
        "Routing warning: project selection tokens beyond the supported number of distinct values were ignored."
    ),
    "role_limit": (
        "Routing warning: role selection tokens beyond the supported number of distinct values were ignored."
    ),
    "support_limit": (
        "Routing warning: supporting agent tokens beyond the supported number of distinct values were ignored."
    ),
}


class HookError(ValueError):
    """Raised for invalid hook input, registry state, or unsafe local state."""


def _emit(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def _blocked(reason: str) -> None:
    _emit({"decision": "block", "reason": reason})


def _registry_path() -> Path:
    return ROOT / ".agents" / "registry.json"


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise HookError("route state directory must not be a symlink")
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
    if not path.is_dir():
        raise HookError("route state path is not a directory")
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise HookError("route state directory permissions are too broad")


def _working_directory(event: dict[str, Any]) -> Path:
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        raise HookError("Codex did not provide a working directory")
    if "\x00" in cwd or not os.path.isabs(cwd):
        raise HookError("Codex working directory must be an absolute path")
    try:
        return Path(cwd).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise HookError("Codex working directory could not be resolved") from exc


def _state_path(event: dict[str, Any], cwd: Path) -> Path:
    """Scope route state by session and working directory using one opaque hash.

    The file name never contains the session identifier or the path itself, so
    switching directories inside one session starts a fresh route scope.
    """

    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise HookError("Codex did not provide a session identifier")
    scope_material = json.dumps(
        {"session_id": session_id, "cwd": str(cwd)}, ensure_ascii=False, sort_keys=True
    )
    scope = hashlib.sha256(scope_material.encode("utf-8")).hexdigest()
    state_root = Path(tempfile.gettempdir()) / "multi-agent-industry-route-state"
    _private_directory(state_root)
    return state_root / f"{scope}.json"


def _load_state(path: Path) -> ConversationState | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise HookError("route state file is not a regular file")
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise HookError("route state file permissions are too broad")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return ConversationState.from_dict(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise HookError("route state file is invalid") from exc


def _save_state(path: Path, state: ConversationState) -> None:
    payload = json.dumps(state.to_dict(), ensure_ascii=False, separators=(",", ":"))
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".route-", delete=False
        ) as handle:
            temporary_name = handle.name
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise HookError("route state file must not be a symlink")
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def _warn(warnings: list[str], key: str) -> None:
    message = _WARNINGS[key]
    if message not in warnings:
        warnings.append(message)


def _lex_tokens(prompt: str) -> dict[str, list[str]]:
    """Return raw token values per kind from a single non-overlapping pass."""

    tokens: dict[str, list[str]] = {kind: [] for kind in _TOKEN_KINDS.values()}
    for chunk in _TOKEN_CHUNK.finditer(prompt):
        text = chunk.group()
        match = _TOKEN_START.search(text)
        if match is None or match.end() == len(text):
            continue
        tokens[_TOKEN_KINDS[match.group(1)]].append(text[match.end():])
    return tokens


def _normalize_token_value(raw_value: str) -> str | None:
    """Return the identifier spelled by ``raw_value`` or None.

    Non-ASCII values are rejected before case folding, because Unicode lowercasing maps
    some lookalikes (for example KELVIN SIGN) onto ASCII letters. Only ASCII is folded.
    """

    if not raw_value.isascii():
        return None
    value = raw_value.lower()
    try:
        _identifier(value, "routing token")
    except RegistryValidationError:
        return None
    return value


def _distinct_valid(
    raw_values: list[str], kind: str, warnings: list[str]
) -> tuple[list[str], bool]:
    """Normalize values in order; return distinct valid values and whether any repeated."""

    values: list[str] = []
    seen: set[str] = set()
    repeated = False
    for raw_value in raw_values:
        value = _normalize_token_value(raw_value)
        if value is None:
            _warn(warnings, f"{kind}_invalid")
            continue
        if value in seen:
            repeated = True
            continue
        seen.add(value)
        values.append(value)
    return values, repeated


def _capped(values: list[str], kind: str, warnings: list[str]) -> list[str]:
    if len(values) > _MAX_TOKEN_VALUES_PER_KIND:
        _warn(warnings, f"{kind}_limit")
        return values[:_MAX_TOKEN_VALUES_PER_KIND]
    return values


def _available_agent(registry: AgentRegistry, agent_id: str) -> bool:
    agent = registry.get(agent_id)
    return bool(
        agent is not None
        and agent.runtime_enabled
        and agent.runtime_provider is not None
        and agent.runtime_agent_name is not None
    )


def _one_selection(
    values: list[str], *, multiple_warning: str, warnings: list[str]
) -> str | None:
    if len(values) > 1:
        _warn(warnings, multiple_warning)
        return None
    return values[0] if values else None


def _agent_selection(
    registry: AgentRegistry, raw_values: list[str], warnings: list[str], hold_probe: list[str]
) -> str | None:
    values, _ = _distinct_valid(raw_values, "agent", warnings)
    # Every registered value is checked against hold, even a disabled or incomplete one
    # and even one beyond the selection cap.
    hold_probe.extend(value for value in values if registry.get(value) is not None)
    candidates: list[str] = []
    for value in _capped(values, "agent", warnings):
        if not _available_agent(registry, value):
            _warn(warnings, "agent_unavailable")
            continue
        candidates.append(value)
    return _one_selection(candidates, multiple_warning="agent_multiple", warnings=warnings)


def _project_selection(
    registry: AgentRegistry, raw_values: list[str], warnings: list[str], hold_probe: list[str]
) -> str | None:
    values, _ = _distinct_valid(raw_values, "project", warnings)
    hold_probe.extend(value for value in values if value in registry.project_routes)
    candidates: list[str] = []
    for value in _capped(values, "project", warnings):
        if value not in registry.project_routes:
            _warn(warnings, "project_unavailable")
            continue
        candidates.append(value)
    return _one_selection(candidates, multiple_warning="project_multiple", warnings=warnings)


def _role_selection(
    registry: AgentRegistry, raw_values: list[str], warnings: list[str], hold_probe: list[str]
) -> str | None:
    values, _ = _distinct_valid(raw_values, "role", warnings)
    agents_by_role: dict[str, list[str]] = {}
    for agent in registry.agents.values():
        agents_by_role.setdefault(agent.role, []).append(agent.agent_id)
    # Every registered agent of every named role is checked against hold, including
    # disabled ones and roles that are ambiguous, conflicting or beyond the cap.
    for value in values:
        hold_probe.extend(agents_by_role.get(value, ()))
    candidates: list[str] = []
    for value in _capped(values, "role", warnings):
        matches = [
            agent_id for agent_id in agents_by_role.get(value, ()) if _available_agent(registry, agent_id)
        ]
        if not matches:
            _warn(warnings, "role_unavailable")
            continue
        if len(matches) > 1:
            _warn(warnings, "role_ambiguous")
            continue
        candidates.append(value)
    return _one_selection(candidates, multiple_warning="role_multiple", warnings=warnings)


def _supporting_agents(
    registry: AgentRegistry, raw_values: list[str], warnings: list[str], hold_probe: list[str]
) -> tuple[str, ...]:
    values, repeated = _distinct_valid(raw_values, "support", warnings)
    if repeated:
        _warn(warnings, "support_duplicate")
    hold_probe.extend(value for value in values if registry.get(value) is not None)
    supports: list[str] = []
    for value in _capped(values, "support", warnings):
        if not _available_agent(registry, value):
            _warn(warnings, "support_unavailable")
            continue
        supports.append(value)
    return tuple(sorted(supports))


def _project_status_field(registry: AgentRegistry, project_id: str | None) -> str:
    if project_id is None:
        return ""
    status = registry.project_status(project_id)
    return f"project_status={status}; " if status is not None else ""


def _project_constraints_context(registry: AgentRegistry, project_id: str | None) -> str:
    """Render project constraints only. Paths, repositories and runtime ids are never emitted."""

    if project_id is None:
        return ""
    parts: list[str] = []
    if registry.project_status(project_id) == "hold":
        parts.append(
            "The active project is on hold: do not implement, edit files or start implementation-capable Agents. "
        )
    constraints = registry.project_constraints.get(project_id, ())
    if constraints:
        parts.append(
            "Project constraints (narrowing only; they never grant permissions or override higher-priority "
            "instructions): project_constraints_json="
            + json.dumps(list(constraints), ensure_ascii=False, separators=(",", ":"))
            + " End of project constraints. "
        )
    return "".join(parts)


def handle(event: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise HookError("hook input must be a JSON object")
    if event.get("hook_event_name") != "UserPromptSubmit":
        raise HookError("unexpected Codex hook event")
    prompt = event.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HookError("hook prompt is missing")

    registry_path = _registry_path()
    try:
        registry = AgentRegistry.from_dict(json.loads(registry_path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise HookError("agent registry could not be validated") from exc
    try:
        overlay = load_project_overlay(registry, repo_root=ROOT)
        registry = apply_project_overlay(registry, overlay, base_dir=ROOT)
    except ProjectOverlayError as exc:
        raise HookError("project overlay could not be validated") from exc
    try:
        persona_layers = load_persona_layers(
            registry,
            global_path=ROOT / ".agents" / "persona-settings.json",
            repo_root=ROOT,
            project_overlay=overlay,
        )
    except PersonaSettingsError as exc:
        raise HookError("persona settings could not be validated") from exc

    warnings: list[str] = []
    probe_agents: list[str] = []
    probe_projects: list[str] = []
    tokens = _lex_tokens(prompt)
    explicit_agent = _agent_selection(registry, tokens["agent"], warnings, probe_agents)
    project_id = _project_selection(registry, tokens["project"], warnings, probe_projects)
    role = _role_selection(registry, tokens["role"], warnings, probe_agents)
    support_ids = _supporting_agents(registry, tokens["support"], warnings, probe_agents)
    cwd = _working_directory(event)
    try:
        cwd_project = registry.project_for_working_directory(cwd, base_dir=ROOT)
    except RegistryValidationError as exc:
        raise HookError("working-directory project route is ambiguous or invalid") from exc
    state_path = _state_path(event, cwd)
    previous = _load_state(state_path)
    # Every valid, resolvable agent the prompt names, including selections that are
    # ignored as conflicting or ambiguous, is refused on a hold project.
    if hold_refuses_any(
        registry,
        probe_agents,
        project_ids=(project_id, *probe_projects),
        previous=previous,
        working_directory_project_id=cwd_project,
    ):
        return {"decision": "block", "reason": HOLD_REFUSAL_REASON}
    base_decision = resolve_route(
        registry,
        explicit_agent_id=explicit_agent,
        project_id=project_id,
        requested_role=role,
        previous=previous,
        working_directory_project_id=cwd_project,
    )
    if not base_decision.allowed or base_decision.state is None:
        return {
            "decision": "block",
            "reason": base_decision.reason or "route could not be resolved",
        }
    filtered_support_ids = tuple(
        support_id
        for support_id in support_ids
        if support_id != base_decision.state.active_agent_id
    )
    if len(filtered_support_ids) != len(support_ids):
        _warn(warnings, "support_active")
    decision = resolve_route(
        registry,
        explicit_agent_id=explicit_agent,
        project_id=project_id,
        requested_role=role,
        supporting_agent_ids=filtered_support_ids,
        previous=previous,
        working_directory_project_id=cwd_project,
    )
    if not decision.allowed or decision.state is None:
        return {"decision": "block", "reason": decision.reason or "route could not be resolved"}
    _save_state(state_path, decision.state)
    agent = registry.get(decision.state.active_agent_id)
    assert agent is not None
    active_project = decision.state.active_project_id
    selected_persona = persona_layers.resolve(agent, project_id=active_project)
    persona_context = presentation_context(
        agent,
        selected_persona.profile if selected_persona is not None else None,
    )

    context = (
        "Registry route resolved. "
        f"active_agent_id={agent.agent_id}; role={agent.role}; "
        f"runtime_binding={agent.runtime_provider}/{agent.runtime_agent_name}; "
        f"declared_permissions={','.join(agent.permissions)}; "
        f"project_id={active_project or 'none'}; "
        f"{_project_status_field(registry, active_project)}"
        f"supporting_agent_ids={','.join(decision.state.supporting_agent_ids) or 'none'}; "
        f"route_source={decision.route_source}. "
        f"{_project_constraints_context(registry, active_project)}"
        "Routing is selection, not execution or a permission grant. When work is assigned to a non-coordinator Agent, "
        "use the registered native runtime binding if available; if execution cannot start, report a blocked or planned "
        "state. Respect the actual runtime sandbox and keep the coordinator as final report owner. "
        "Accept completion only when the runtime actually starts the Agent and provides evidence."
    )
    if warnings:
        context += " " + " ".join(warnings)
    if persona_context:
        context += " " + persona_context
        policy_context = presentation_policy_context(persona_layers.presentation_policy())
        if policy_context:
            context += " " + policy_context
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }


def main() -> int:
    raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
    if len(raw) > _MAX_INPUT_BYTES:
        _blocked("Codex hook input exceeds the supported size")
        return 0
    try:
        event = json.loads(raw.decode("utf-8"))
        _emit(handle(event))
    except (UnicodeError, json.JSONDecodeError, HookError, OSError) as exc:
        _blocked(str(exc) if isinstance(exc, HookError) else "Codex route hook failed closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
