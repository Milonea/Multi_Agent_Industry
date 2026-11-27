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

from agent_runtime.registry import AgentRegistry, RegistryValidationError  # noqa: E402
from agent_runtime.persona import presentation_context, presentation_policy_context  # noqa: E402
from agent_runtime.persona_layers import PersonaSettingsError, load_persona_layers  # noqa: E402
from agent_runtime.project_overlay import (  # noqa: E402
    ProjectOverlayError,
    apply_project_overlay,
    load_project_overlay,
)
from agent_runtime.routing import ConversationState, resolve_route  # noqa: E402


_AGENT_TOKEN = re.compile(r"(?<![\w.-])@agent:([^\s,;]+)")
_PROJECT_TOKEN = re.compile(r"(?<![\w.-])project:([^\s,;]+)")
_ROLE_TOKEN = re.compile(r"(?<![\w.-])role:([^\s,;]+)")
_SUPPORT_TOKEN = re.compile(r"(?<![\w.-])support:agent:([^\s,;]+)")
_MAX_INPUT_BYTES = 1_000_000


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


def _single_token(pattern: re.Pattern[str], prompt: str, label: str) -> str | None:
    values = pattern.findall(prompt)
    normalized = {value.lower() for value in values}
    if len(normalized) > 1:
        raise HookError(f"prompt names more than one {label}")
    return next(iter(normalized), None)


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

    explicit_agent = _single_token(_AGENT_TOKEN, prompt, "agent")
    project_id = _single_token(_PROJECT_TOKEN, prompt, "project")
    role = _single_token(_ROLE_TOKEN, prompt, "role")
    support_ids = tuple(sorted({value.lower() for value in _SUPPORT_TOKEN.findall(prompt)}))
    cwd = _working_directory(event)
    try:
        cwd_project = registry.project_for_working_directory(cwd, base_dir=ROOT)
    except RegistryValidationError as exc:
        raise HookError("working-directory project route is ambiguous or invalid") from exc
    state_path = _state_path(event, cwd)
    previous = _load_state(state_path)
    decision = resolve_route(
        registry,
        explicit_agent_id=explicit_agent,
        project_id=project_id,
        requested_role=role,
        supporting_agent_ids=support_ids,
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
