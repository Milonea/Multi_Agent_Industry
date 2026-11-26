"""Codex UserPromptSubmit adapter for explicit, registry-backed routing.

The hook stores only route identifiers in a private local state file. It never
stores prompt text, transcripts, tool output, or credentials.
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

from agent_runtime.registry import AgentRegistry  # noqa: E402
from agent_runtime.persona import presentation_context  # noqa: E402
from agent_runtime.persona_layers import PersonaSettingsError, load_persona_layers  # noqa: E402
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


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise HookError("route state directory must not be a symlink")
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
    if not path.is_dir():
        raise HookError("route state path is not a directory")
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise HookError("route state directory permissions are too broad")


def _state_path(event: dict[str, Any]) -> Path:
    session_id = event.get("session_id")
    cwd = event.get("cwd")
    if not isinstance(session_id, str) or not session_id.strip():
        raise HookError("Codex did not provide a session identifier")
    if not isinstance(cwd, str) or not cwd.strip():
        raise HookError("Codex did not provide a working directory")
    scope = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
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


def handle(event: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise HookError("hook input must be a JSON object")
    if event.get("hook_event_name") != "UserPromptSubmit":
        raise HookError("unexpected Codex hook event")
    prompt = event.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HookError("hook prompt is missing")

    registry_path = ROOT / ".agents" / "registry.json"
    try:
        registry = AgentRegistry.from_dict(json.loads(registry_path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise HookError("agent registry could not be validated") from exc
    try:
        persona_layers = load_persona_layers(
            registry,
            global_path=ROOT / ".agents" / "persona-settings.json",
            repo_root=ROOT,
        )
    except PersonaSettingsError as exc:
        raise HookError("persona settings could not be validated") from exc

    explicit_agent = _single_token(_AGENT_TOKEN, prompt, "agent")
    project_id = _single_token(_PROJECT_TOKEN, prompt, "project")
    role = _single_token(_ROLE_TOKEN, prompt, "role")
    support_ids = tuple(sorted({value.lower() for value in _SUPPORT_TOKEN.findall(prompt)}))
    state_path = _state_path(event)
    previous = _load_state(state_path)
    decision = resolve_route(
        registry,
        explicit_agent_id=explicit_agent,
        project_id=project_id,
        requested_role=role,
        supporting_agent_ids=support_ids,
        previous=previous,
    )
    if not decision.allowed or decision.state is None:
        return {"decision": "block", "reason": decision.reason or "route could not be resolved"}
    _save_state(state_path, decision.state)
    agent = registry.get(decision.state.active_agent_id)
    assert agent is not None
    selected_persona = persona_layers.resolve(agent)
    persona_context = presentation_context(
        agent,
        selected_persona.profile if selected_persona is not None else None,
    )

    context = (
        "Registry route resolved. "
        f"active_agent_id={agent.agent_id}; role={agent.role}; "
        f"runtime_binding={agent.runtime_provider}/{agent.runtime_agent_name}; "
        f"declared_permissions={','.join(agent.permissions)}; "
        f"project_id={decision.state.active_project_id or 'none'}; "
        f"supporting_agent_ids={','.join(decision.state.supporting_agent_ids) or 'none'}; "
        f"route_source={decision.route_source}. "
        "Routing is selection, not execution or a permission grant. When work is assigned to a non-coordinator Agent, "
        "use the registered native runtime binding if available; if execution cannot start, report a blocked or planned "
        "state. Respect the actual runtime sandbox and keep the coordinator as final report owner. "
        "Accept completion only when the runtime actually starts the Agent and provides evidence."
    )
    if persona_context:
        context += " " + persona_context
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
