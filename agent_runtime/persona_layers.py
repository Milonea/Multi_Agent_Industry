"""Layer shared persona defaults with private, presentation-only user choices."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Mapping

from .registry import AgentDefinition, AgentRegistry, RegistryValidationError, validate_presentation_profile


_ID_RE = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_PROFILE_FIELDS = {"profile_id", "locale", "style", "tone_tags"}
_PERSONAL_PROFILE_FIELDS = {"identity_data", "source"}
_PERSONA_DATA_BLOCKED_KEYS = {
    "permission",
    "permissions",
    "capabilities",
    "runtime_binding",
    "runtime_agent",
    "runtime_config",
    "project_routes",
    "route",
    "routes",
    "routing",
    "assignments",
    "work_persona",
    "authority",
    "allowed_actions",
    "prohibited_actions",
    "tool_access",
    "tools",
    "command",
    "commands",
    "execution_mode",
    "work_contract",
    "role",
    "roles",
    "primary_role",
}
_PERSONA_DATA_MAX_BYTES = 25_000
PERSONAL_SETTINGS_ENV = "MULTI_AGENT_INDUSTRY_PERSONA_SETTINGS"
PERSONAL_SETTINGS_TEMPLATE = {
    "schema_version": "1.0",
    "scope": "personal",
    "profiles": {},
    "default_profile_id": None,
    "assignments": {"agents": {}, "roles": {}},
}


class PersonaSettingsError(ValueError):
    """Raised when shared or personal presentation settings are invalid."""


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise PersonaSettingsError(f"{field} must be a lowercase identifier")
    return value


def validate_persona_settings_profile(
    raw_profile: Any,
    *,
    allow_personal_fields: bool,
) -> dict[str, Any]:
    """Validate a style profile and optional private identity data."""

    allowed_fields = _PROFILE_FIELDS | (_PERSONAL_PROFILE_FIELDS if allow_personal_fields else set())
    if not isinstance(raw_profile, Mapping) or set(raw_profile) - allowed_fields:
        raise PersonaSettingsError("persona profile has unsupported fields")
    core_fields = {key: raw_profile[key] for key in _PROFILE_FIELDS if key in raw_profile}
    try:
        profile = validate_presentation_profile(core_fields)
    except RegistryValidationError as exc:
        raise PersonaSettingsError("persona profile is invalid") from exc
    if "identity_data" in raw_profile:
        profile["identity_data"] = _validate_identity_data(raw_profile["identity_data"])
    if "source" in raw_profile:
        profile["source"] = _validate_persona_source(raw_profile["source"])
    return profile


def _validate_identity_data(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PersonaSettingsError("persona identity data must be an object")
    nodes = 0

    def validate_node(node: Any, depth: int = 0) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > 10_000 or depth > 20:
            raise PersonaSettingsError("persona identity data exceeds structural limits")
        if isinstance(node, Mapping):
            if len(node) > 1_000 or any(not isinstance(key, str) for key in node):
                raise PersonaSettingsError("persona identity data has invalid object keys")
            if any(key.casefold() in _PERSONA_DATA_BLOCKED_KEYS for key in node):
                raise PersonaSettingsError("persona identity data contains execution or authority fields")
            return {key: validate_node(item, depth + 1) for key, item in node.items()}
        if isinstance(node, list):
            if len(node) > 2_000:
                raise PersonaSettingsError("persona identity data contains an oversized list")
            return [validate_node(item, depth + 1) for item in node]
        if isinstance(node, str):
            if len(node) > 12_000:
                raise PersonaSettingsError("persona identity text is too long")
            return node
        if node is None or isinstance(node, (bool, int)):
            return node
        if isinstance(node, float):
            if node != node or node in {float("inf"), float("-inf")}:
                raise PersonaSettingsError("persona identity data contains a non-finite number")
            return node
        raise PersonaSettingsError("persona identity data must contain JSON values")

    identity_data = validate_node(value)
    try:
        size = len(json.dumps(identity_data, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PersonaSettingsError("persona identity data is not valid JSON") from exc
    if size > _PERSONA_DATA_MAX_BYTES:
        raise PersonaSettingsError("persona identity data exceeds the supported size")
    return identity_data


def _validate_persona_source(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"project", "records"}:
        raise PersonaSettingsError("persona source metadata has missing or unsupported fields")
    project = value["project"]
    records = value["records"]
    if not isinstance(project, str) or not project.strip() or len(project) > 128:
        raise PersonaSettingsError("persona source project is invalid")
    if not isinstance(records, list) or not records or len(records) > 64:
        raise PersonaSettingsError("persona source records are invalid")
    normalized: list[dict[str, str]] = []
    for record in records:
        if not isinstance(record, Mapping) or set(record) != {"path", "record_id", "sha256"}:
            raise PersonaSettingsError("persona source record has missing or unsupported fields")
        path = record["path"]
        record_id = record["record_id"]
        digest = record["sha256"]
        if (
            not isinstance(path, str)
            or not path.strip()
            or path.startswith(("/", "\\"))
            or re.match(r"^[a-zA-Z]:", path) is not None
        ):
            raise PersonaSettingsError("persona source path must be relative")
        if any(part in {".", ".."} for part in path.replace("\\", "/").split("/")):
            raise PersonaSettingsError("persona source path must not traverse directories")
        if not isinstance(record_id, str) or not record_id.strip() or len(record_id) > 128:
            raise PersonaSettingsError("persona source record identifier is invalid")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise PersonaSettingsError("persona source digest must be SHA-256")
        normalized.append({"path": path, "record_id": record_id, "sha256": digest})
    return {"project": project.strip(), "records": normalized}


@dataclass(frozen=True)
class PersonaSettings:
    scope: str
    profiles: Mapping[str, Mapping[str, Any]]
    default_profile_id: str | None
    agent_assignments: Mapping[str, str]
    role_assignments: Mapping[str, str]

    @classmethod
    def empty(cls, scope: str) -> "PersonaSettings":
        raw: dict[str, Any] = {
            "schema_version": "1.0",
            "scope": scope,
            "profiles": {},
            "default_profile_id": None,
            "assignments": {"agents": {}, "roles": {}},
        }
        return cls.from_dict(raw, expected_scope=scope)

    @classmethod
    def from_dict(cls, raw: Any, *, expected_scope: str) -> "PersonaSettings":
        if expected_scope not in {"global", "personal"}:
            raise PersonaSettingsError("persona settings scope is unsupported")
        keys = {"schema_version", "scope", "profiles", "default_profile_id", "assignments"}
        if not isinstance(raw, Mapping) or set(raw) != keys:
            raise PersonaSettingsError(f"{expected_scope} persona settings have missing or unsupported fields")
        if raw["schema_version"] != "1.0" or raw["scope"] != expected_scope:
            raise PersonaSettingsError(f"{expected_scope} persona settings version or scope is invalid")

        raw_profiles = raw["profiles"]
        if not isinstance(raw_profiles, Mapping):
            raise PersonaSettingsError("persona profiles must be an object")
        profiles: dict[str, dict[str, Any]] = {}
        for raw_id, raw_profile in raw_profiles.items():
            profile_id = _identifier(raw_id, "profile_id")
            try:
                profile = validate_persona_settings_profile(
                    raw_profile,
                    allow_personal_fields=expected_scope == "personal",
                )
            except PersonaSettingsError as exc:
                raise PersonaSettingsError("persona profile is invalid") from exc
            if profile["profile_id"] != profile_id:
                raise PersonaSettingsError("persona profile key must match profile_id")
            profiles[profile_id] = profile

        raw_assignments = raw["assignments"]
        if not isinstance(raw_assignments, Mapping) or set(raw_assignments) != {"agents", "roles"}:
            raise PersonaSettingsError("persona assignments must contain agents and roles")
        agent_assignments = cls._assignment_map(raw_assignments["agents"], "agent")
        role_assignments = cls._assignment_map(raw_assignments["roles"], "role")

        raw_default = raw["default_profile_id"]
        default_profile_id = (
            _identifier(raw_default, "default_profile_id") if raw_default is not None else None
        )
        if expected_scope == "global" and default_profile_id is not None:
            if default_profile_id not in profiles:
                raise PersonaSettingsError("global default references an unknown profile")

        return cls(
            scope=expected_scope,
            profiles=profiles,
            default_profile_id=default_profile_id,
            agent_assignments=agent_assignments,
            role_assignments=role_assignments,
        )

    @staticmethod
    def _assignment_map(raw: Any, field: str) -> dict[str, str]:
        if not isinstance(raw, Mapping):
            raise PersonaSettingsError(f"persona {field} assignments must be an object")
        result: dict[str, str] = {}
        for target, profile_id in raw.items():
            target_id = _identifier(target, f"{field}_id")
            result[target_id] = _identifier(profile_id, "assigned_profile_id")
        return result


@dataclass(frozen=True)
class ResolvedPersona:
    profile_id: str
    profile: Mapping[str, Any]
    source: str


class PersonaLayers:
    """Validated shared and personal settings with deterministic precedence."""

    def __init__(
        self,
        registry: AgentRegistry,
        global_settings: PersonaSettings,
        personal_settings: PersonaSettings | None = None,
    ) -> None:
        if not isinstance(registry, AgentRegistry):
            raise PersonaSettingsError("agent registry is required")
        if not isinstance(global_settings, PersonaSettings) or global_settings.scope != "global":
            raise PersonaSettingsError("global persona settings are required")
        personal = personal_settings or PersonaSettings.empty("personal")
        if not isinstance(personal, PersonaSettings) or personal.scope != "personal":
            raise PersonaSettingsError("personal persona settings have the wrong scope")
        if set(global_settings.profiles) & set(personal.profiles):
            raise PersonaSettingsError("global and personal profile identifiers must be unique")

        known_roles = {agent.role for agent in registry.agents.values()}
        for settings in (global_settings, personal):
            if set(settings.agent_assignments) - set(registry.agents):
                raise PersonaSettingsError(f"{settings.scope} assignment references an unknown agent")
            if set(settings.role_assignments) - known_roles:
                raise PersonaSettingsError(f"{settings.scope} assignment references an unknown role")
            available_profiles = set(global_settings.profiles)
            if settings.scope == "personal":
                available_profiles |= set(personal.profiles)
            assigned_profiles = set(settings.agent_assignments.values()) | set(settings.role_assignments.values())
            if assigned_profiles - available_profiles:
                raise PersonaSettingsError(f"{settings.scope} assignment references an unknown profile")
            if settings.default_profile_id is not None and settings.default_profile_id not in available_profiles:
                raise PersonaSettingsError(f"{settings.scope} default references an unknown profile")

        self.registry = registry
        self.global_settings = global_settings
        self.personal_settings = personal

    def resolve(self, agent: AgentDefinition) -> ResolvedPersona | None:
        if not isinstance(agent, AgentDefinition) or self.registry.get(agent.agent_id) != agent:
            raise PersonaSettingsError("persona target must be a registered agent")

        candidates = (
            (self.personal_settings.agent_assignments.get(agent.agent_id), "personal-agent"),
            (self.personal_settings.role_assignments.get(agent.role), "personal-role"),
            (self.personal_settings.default_profile_id, "personal-default"),
            (self.global_settings.agent_assignments.get(agent.agent_id), "global-agent"),
            (
                agent.persona.get("profile_id") if agent.persona is not None else None,
                "registry-agent",
            ),
            (self.global_settings.role_assignments.get(agent.role), "global-role"),
            (self.global_settings.default_profile_id, "global-default"),
        )
        for profile_id, source in candidates:
            if profile_id is None:
                continue
            if source == "registry-agent":
                profile = agent.persona
            else:
                profile = self.personal_settings.profiles.get(profile_id)
                if profile is None:
                    profile = self.global_settings.profiles.get(profile_id)
            if profile is None:
                raise PersonaSettingsError("resolved persona profile is missing")
            return ResolvedPersona(profile_id=profile_id, profile=profile, source=source)
        return None


def default_personal_settings_path() -> Path:
    """Return the user-scoped settings path without creating or reading it."""

    configured = os.environ.get(PERSONAL_SETTINGS_ENV)
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise PersonaSettingsError("personal settings override must be an absolute path")
        return path
    if os.name == "nt":
        app_data = os.environ.get("APPDATA")
        root = Path(app_data) if app_data else Path.home() / "AppData" / "Roaming"
        return root / "MultiAgentIndustry" / "persona-settings.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MultiAgentIndustry" / "persona-settings.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return root / "multi-agent-industry" / "persona-settings.json"


def load_persona_layers(
    registry: AgentRegistry,
    *,
    global_path: Path,
    personal_path: Path | None = None,
    repo_root: Path | None = None,
    include_personal: bool = True,
) -> PersonaLayers:
    """Load versioned shared defaults and, when enabled, private user settings."""

    try:
        raw_global = json.loads(Path(global_path).read_text(encoding="utf-8"))
        global_settings = PersonaSettings.from_dict(raw_global, expected_scope="global")
    except (OSError, UnicodeError, json.JSONDecodeError, PersonaSettingsError) as exc:
        raise PersonaSettingsError("global persona settings could not be validated") from exc

    personal_settings = PersonaSettings.empty("personal")
    if include_personal:
        destination = personal_path if personal_path is not None else default_personal_settings_path()
        personal_settings = _load_personal_settings(destination, repo_root=repo_root)
    return PersonaLayers(registry, global_settings, personal_settings)


def _load_personal_settings(path: Path, *, repo_root: Path | None) -> PersonaSettings:
    destination = Path(path).expanduser()
    if not destination.is_absolute():
        raise PersonaSettingsError("personal settings path must be absolute")
    destination = _outside_repository(destination, repo_root)
    if destination.is_symlink():
        raise PersonaSettingsError("personal settings file must not be a symlink")
    if not destination.exists():
        return PersonaSettings.empty("personal")
    if not destination.is_file():
        raise PersonaSettingsError("personal settings path must be a regular file")
    if os.name == "posix":
        file_info = destination.stat()
        directory_info = destination.parent.stat()
        if stat.S_IMODE(file_info.st_mode) & 0o077:
            raise PersonaSettingsError("personal settings file permissions must be owner-only")
        if stat.S_IMODE(directory_info.st_mode) & 0o077:
            raise PersonaSettingsError("personal settings directory permissions must be owner-only")
        if file_info.st_uid != os.geteuid() or directory_info.st_uid != os.geteuid():
            raise PersonaSettingsError("personal settings must be owned by the current user")
    try:
        raw = json.loads(destination.read_text(encoding="utf-8"))
        return PersonaSettings.from_dict(raw, expected_scope="personal")
    except (OSError, UnicodeError, json.JSONDecodeError, PersonaSettingsError) as exc:
        raise PersonaSettingsError("personal persona settings could not be validated") from exc


def _outside_repository(path: Path, repo_root: Path | None) -> Path:
    if path.is_symlink():
        raise PersonaSettingsError("personal settings file must not be a symlink")
    resolved = path.resolve()
    if repo_root is not None:
        try:
            resolved.relative_to(Path(repo_root).resolve())
        except ValueError:
            pass
        else:
            raise PersonaSettingsError("personal settings must be stored outside the repository")
    return resolved


def initialize_personal_settings(
    path: Path | None = None,
    *,
    repo_root: Path | None = None,
) -> Path:
    """Create an empty private config without overwriting existing user data."""

    destination = path if path is not None else default_personal_settings_path()
    destination = Path(destination).expanduser()
    if not destination.is_absolute():
        raise PersonaSettingsError("personal settings path must be absolute")
    destination = _outside_repository(destination, repo_root)
    if destination.exists() or destination.is_symlink():
        raise PersonaSettingsError("personal settings file already exists")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            directory_info = destination.parent.stat()
            if stat.S_IMODE(directory_info.st_mode) & 0o077:
                raise PersonaSettingsError("personal settings directory permissions must be owner-only")
            if directory_info.st_uid != os.geteuid():
                raise PersonaSettingsError("personal settings directory must be owned by the current user")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(destination, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            json.dump(PERSONAL_SETTINGS_TEMPLATE, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except PersonaSettingsError:
        raise
    except OSError as exc:
        raise PersonaSettingsError("personal settings file could not be initialized") from exc
    return destination
