"""Private, user-scoped project overlay.

The overlay maps a user's real projects onto the generic registered roles. It lives
only in a private file outside the repository. The public framework ships the schema,
this loader, a synthetic example and tests; the shared registry keeps
``project_routes`` empty.

An overlay can only *select* registered agents and *narrow* work: it never grants
permissions, capabilities, runtime bindings or commands to an agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePath
import re
from typing import Any, Mapping

from .path_identity import PathIdentity
from .persona_layers import _PERSONA_DATA_BLOCKED_KEYS
from .private_files import configured_private_path, create_private_json, read_private_json
from .registry import AgentRegistry, RegistryValidationError
from .routing import IMPLEMENTATION_PERMISSIONS


PROJECT_OVERLAY_ENV = "MULTI_AGENT_INDUSTRY_PROJECT_OVERLAY"
PROJECT_OVERLAY_FILE_NAME = "project-overlay.json"
PROJECT_OVERLAY_TEMPLATE = {"schema_version": "1.0", "scope": "personal", "projects": {}}

PROJECT_STATUSES = ("active", "hold", "inventory_only")
RUNTIME_PROVIDERS = ("orca",)
RUNTIME_AGENT_KINDS = ("codex", "claude")
RUNTIME_WORKSPACE_POLICIES = ("new-top-level", "existing")

_ID_RE = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}$")
_ORCA_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:/_.@-]{0,199}$")
_TOP_KEYS = frozenset({"schema_version", "scope", "projects"})
_PROJECT_REQUIRED_KEYS = frozenset({"owner_agent_id", "status"})
_PROJECT_KEYS = _PROJECT_REQUIRED_KEYS | {
    "path",
    "worktree_roots",
    "repository",
    "hold_agent_id",
    "persona_assignments",
    "constraints",
    "validation",
    "manual_gates",
    "runtime",
}
_PERSONA_ASSIGNMENT_KEYS = frozenset({"agents", "roles"})
_RUNTIME_REQUIRED_KEYS = frozenset({"provider", "agent"})
_RUNTIME_KEYS = _RUNTIME_REQUIRED_KEYS | {"orca_project_id", "workspace"}
# Keys that are blocked elsewhere but are explicit, validated fields of this schema.
_ALLOWED_BLOCKED_KEY_PATHS = frozenset({("projects", "*", "persona_assignments", "roles")})
_MAX_PROJECTS = 200
_MAX_PATH_LENGTH = 4096
_MAX_WORKTREE_ROOTS = 16
_MAX_TEXT_ITEMS = 32
_MAX_TEXT_CHARS = 300
_MAX_VALIDATION_COMMANDS = 16
_MAX_ARGV_ITEMS = 64
_MAX_ARGV_CHARS = 1000


class ProjectOverlayError(ValueError):
    """Raised when a private project overlay is unsafe or invalid.

    Messages name the failing field but never echo file contents.
    """


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ProjectOverlayError(f"{field_name} must be a lowercase identifier")
    return value


def _absolute_path(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ProjectOverlayError(f"{field_name} must be a non-empty path without surrounding whitespace")
    if len(value) > _MAX_PATH_LENGTH or "\x00" in value:
        raise ProjectOverlayError(f"{field_name} is not a supported path")
    if value.startswith("~"):
        raise ProjectOverlayError(f"{field_name} must not rely on home-directory expansion")
    if not Path(value).is_absolute():
        raise ProjectOverlayError(f"{field_name} must be an absolute path")
    if ".." in PurePath(value).parts:
        raise ProjectOverlayError(f"{field_name} must not contain parent-directory segments")
    return value


def _short_text_list(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > _MAX_TEXT_ITEMS:
        raise ProjectOverlayError(f"{field_name} must be a list of at most {_MAX_TEXT_ITEMS} strings")
    result: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item.strip()
            or len(item) > _MAX_TEXT_CHARS
            or any(ord(char) < 32 or ord(char) == 127 for char in item)
        ):
            raise ProjectOverlayError(f"{field_name} items must be short single-line strings")
        text = item.strip()
        if text in result:
            raise ProjectOverlayError(f"{field_name} must not contain duplicates")
        result.append(text)
    return tuple(result)


def _validation_commands(value: Any, field_name: str) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list) or len(value) > _MAX_VALIDATION_COMMANDS:
        raise ProjectOverlayError(f"{field_name} must be a list of at most {_MAX_VALIDATION_COMMANDS} argv lists")
    commands: list[tuple[str, ...]] = []
    for argv in value:
        if not isinstance(argv, list) or not argv or len(argv) > _MAX_ARGV_ITEMS:
            raise ProjectOverlayError(f"{field_name} entries must be non-empty argv lists")
        for arg in argv:
            if not isinstance(arg, str) or not arg or len(arg) > _MAX_ARGV_CHARS or "\x00" in arg:
                raise ProjectOverlayError(f"{field_name} argv items must be non-empty strings")
        if not argv[0].strip():
            raise ProjectOverlayError(f"{field_name} argv must start with a program name")
        commands.append(tuple(argv))
    return tuple(commands)


def _reject_blocked_keys(node: Any, path: tuple[str, ...] = ()) -> None:
    """Reject authority keys anywhere, except the explicitly allowed schema fields."""

    if isinstance(node, Mapping):
        for key, item in node.items():
            if not isinstance(key, str):
                raise ProjectOverlayError("project overlay keys must be strings")
            child = path + (key,)
            pattern = child
            if len(child) >= 2 and child[0] == "projects":
                pattern = ("projects", "*") + child[2:]
            if key.casefold() in _PERSONA_DATA_BLOCKED_KEYS and pattern not in _ALLOWED_BLOCKED_KEY_PATHS:
                raise ProjectOverlayError("project overlay contains execution or authority fields")
            _reject_blocked_keys(item, child)
    elif isinstance(node, list):
        for item in node:
            _reject_blocked_keys(item, path + ("[]",))


@dataclass(frozen=True)
class ProjectRuntime:
    provider: str
    agent: str
    orca_project_id: str | None = None
    workspace: str | None = None


@dataclass(frozen=True)
class OverlayProject:
    project_id: str
    owner_agent_id: str
    status: str
    path: str | None = None
    worktree_roots: tuple[str, ...] = field(default_factory=tuple)
    repository: str | None = None
    hold_agent_id: str | None = None
    persona_agent_assignments: Mapping[str, str] = field(default_factory=dict)
    persona_role_assignments: Mapping[str, str] = field(default_factory=dict)
    constraints: tuple[str, ...] = field(default_factory=tuple)
    validation: tuple[tuple[str, ...], ...] = field(default_factory=tuple)
    manual_gates: tuple[str, ...] = field(default_factory=tuple)
    runtime: ProjectRuntime | None = None

    @property
    def routed(self) -> bool:
        return self.status != "inventory_only"

    @property
    def route_agent_id(self) -> str:
        """Agent selected by a project route: the hold agent while on hold."""

        if self.status == "hold":
            assert self.hold_agent_id is not None
            return self.hold_agent_id
        return self.owner_agent_id

    def assigned_profile_ids(self) -> set[str]:
        return set(self.persona_agent_assignments.values()) | set(self.persona_role_assignments.values())


def _parse_runtime(value: Any, field_name: str) -> ProjectRuntime:
    if not isinstance(value, Mapping) or set(value) - _RUNTIME_KEYS or not _RUNTIME_REQUIRED_KEYS <= set(value):
        raise ProjectOverlayError(f"{field_name} must contain provider and agent with optional orca_project_id and workspace")
    provider = value["provider"]
    agent = value["agent"]
    if provider not in RUNTIME_PROVIDERS:
        raise ProjectOverlayError(f"{field_name}.provider is unsupported")
    if agent not in RUNTIME_AGENT_KINDS:
        raise ProjectOverlayError(f"{field_name}.agent is unsupported")
    orca_project_id = value.get("orca_project_id")
    if orca_project_id is not None and (
        not isinstance(orca_project_id, str) or _ORCA_PROJECT_ID_RE.fullmatch(orca_project_id) is None
    ):
        raise ProjectOverlayError(f"{field_name}.orca_project_id is invalid")
    workspace = value.get("workspace")
    if workspace is not None and workspace not in RUNTIME_WORKSPACE_POLICIES:
        raise ProjectOverlayError(f"{field_name}.workspace is unsupported")
    return ProjectRuntime(provider=provider, agent=agent, orca_project_id=orca_project_id, workspace=workspace)


def _parse_project(project_id: str, index: int, raw: Any, registry: AgentRegistry) -> OverlayProject:
    # Messages use a positional index only: never the private project id, keys or values.
    prefix = f"project entry #{index}"
    if not isinstance(raw, Mapping):
        raise ProjectOverlayError(f"{prefix} must be an object")
    if any(key not in _PROJECT_KEYS for key in raw):
        raise ProjectOverlayError(f"{prefix} has unsupported fields")
    if not _PROJECT_REQUIRED_KEYS <= set(raw):
        raise ProjectOverlayError(f"{prefix} is missing owner_agent_id or status")

    owner = _identifier(raw["owner_agent_id"], f"{prefix}.owner_agent_id")
    if registry.get(owner) is None:
        raise ProjectOverlayError(f"{prefix}.owner_agent_id references an unknown agent")
    status = raw["status"]
    if status not in PROJECT_STATUSES:
        raise ProjectOverlayError(f"{prefix}.status must be one of: " + ", ".join(PROJECT_STATUSES))

    hold_agent_id: str | None = None
    if status == "hold":
        if "hold_agent_id" not in raw:
            raise ProjectOverlayError(f"{prefix}.hold_agent_id is required while status is hold")
        hold_agent_id = _identifier(raw["hold_agent_id"], f"{prefix}.hold_agent_id")
        hold_agent = registry.get(hold_agent_id)
        if hold_agent is None:
            raise ProjectOverlayError(f"{prefix}.hold_agent_id references an unknown agent")
        if IMPLEMENTATION_PERMISSIONS & set(hold_agent.permissions):
            raise ProjectOverlayError(f"{prefix}.hold_agent_id must not be implementation-capable")
    elif "hold_agent_id" in raw:
        raise ProjectOverlayError(f"{prefix}.hold_agent_id is only allowed while status is hold")

    path = _absolute_path(raw["path"], f"{prefix}.path") if "path" in raw else None
    worktree_roots: tuple[str, ...] = tuple()
    if "worktree_roots" in raw:
        roots = raw["worktree_roots"]
        if not isinstance(roots, list) or len(roots) > _MAX_WORKTREE_ROOTS:
            raise ProjectOverlayError(f"{prefix}.worktree_roots must be a short list of absolute paths")
        worktree_roots = tuple(_absolute_path(root, f"{prefix}.worktree_roots") for root in roots)

    repository = raw.get("repository")
    if repository is not None and (
        not isinstance(repository, str) or _REPOSITORY_RE.fullmatch(repository) is None
    ):
        raise ProjectOverlayError(f"{prefix}.repository must be an owner/name string")

    agent_assignments: dict[str, str] = {}
    role_assignments: dict[str, str] = {}
    if "persona_assignments" in raw:
        assignments = raw["persona_assignments"]
        if not isinstance(assignments, Mapping) or set(assignments) - _PERSONA_ASSIGNMENT_KEYS:
            raise ProjectOverlayError(f"{prefix}.persona_assignments may contain only agents and roles")
        known_roles = {agent.role for agent in registry.agents.values()}
        for kind, target in (("agents", agent_assignments), ("roles", role_assignments)):
            entries = assignments.get(kind, {})
            if not isinstance(entries, Mapping):
                raise ProjectOverlayError(f"{prefix}.persona_assignments.{kind} must be an object")
            for key, profile_id in entries.items():
                key_id = _identifier(key, f"{prefix}.persona_assignments.{kind} key")
                if kind == "agents" and registry.get(key_id) is None:
                    raise ProjectOverlayError(f"{prefix}.persona_assignments references an unknown agent")
                if kind == "roles" and key_id not in known_roles:
                    raise ProjectOverlayError(f"{prefix}.persona_assignments references an unknown role")
                target[key_id] = _identifier(profile_id, f"{prefix}.persona_assignments profile id")

    constraints = _short_text_list(raw["constraints"], f"{prefix}.constraints") if "constraints" in raw else ()
    validation = _validation_commands(raw["validation"], f"{prefix}.validation") if "validation" in raw else ()
    manual_gates = _short_text_list(raw["manual_gates"], f"{prefix}.manual_gates") if "manual_gates" in raw else ()
    runtime = _parse_runtime(raw["runtime"], f"{prefix}.runtime") if "runtime" in raw else None
    return OverlayProject(
        project_id=project_id,
        owner_agent_id=owner,
        status=status,
        path=path,
        worktree_roots=worktree_roots,
        repository=repository,
        hold_agent_id=hold_agent_id,
        persona_agent_assignments=agent_assignments,
        persona_role_assignments=role_assignments,
        constraints=constraints,
        validation=validation,
        manual_gates=manual_gates,
        runtime=runtime,
    )


@dataclass(frozen=True)
class ProjectOverlay:
    projects: Mapping[str, OverlayProject]

    @classmethod
    def empty(cls) -> "ProjectOverlay":
        return cls(projects={})

    @classmethod
    def from_dict(cls, raw: Any, *, registry: AgentRegistry) -> "ProjectOverlay":
        if not isinstance(registry, AgentRegistry):
            raise ProjectOverlayError("agent registry is required")
        if not isinstance(raw, Mapping):
            raise ProjectOverlayError("project overlay must be an object")
        _reject_blocked_keys(raw)
        if any(key not in _TOP_KEYS for key in raw):
            raise ProjectOverlayError("project overlay has unsupported top-level fields")
        if not _TOP_KEYS <= set(raw):
            raise ProjectOverlayError("project overlay is missing schema_version, scope or projects")
        if raw["schema_version"] != "1.0" or raw["scope"] != "personal":
            raise ProjectOverlayError("project overlay version or scope is invalid")
        raw_projects = raw["projects"]
        if not isinstance(raw_projects, Mapping) or len(raw_projects) > _MAX_PROJECTS:
            raise ProjectOverlayError("project overlay projects must be an object")
        projects: dict[str, OverlayProject] = {}
        for index, (raw_id, raw_project) in enumerate(raw_projects.items(), start=1):
            project_id = _identifier(raw_id, f"project entry #{index} id")
            projects[project_id] = _parse_project(project_id, index, raw_project, registry)
        return cls(projects=projects)

    def counts(self) -> dict[str, int]:
        result = {status: 0 for status in PROJECT_STATUSES}
        for project in self.projects.values():
            result[project.status] += 1
        return result


def default_project_overlay_path() -> Path:
    """Return the private overlay path without creating or reading it."""

    return configured_private_path(
        PROJECT_OVERLAY_ENV,
        PROJECT_OVERLAY_FILE_NAME,
        label="project overlay",
        error=ProjectOverlayError,
    )


def load_project_overlay(
    registry: AgentRegistry,
    path: Path | None = None,
    *,
    repo_root: Path | None = None,
) -> ProjectOverlay:
    """Load the private overlay. A missing file is an empty overlay; invalid fails closed."""

    destination = path if path is not None else default_project_overlay_path()
    raw = read_private_json(destination, repo_root=repo_root, label="project overlay", error=ProjectOverlayError)
    if raw is None:
        return ProjectOverlay.empty()
    try:
        return ProjectOverlay.from_dict(raw, registry=registry)
    except RegistryValidationError as exc:
        raise ProjectOverlayError("project overlay could not be validated") from exc


def initialize_project_overlay(path: Path | None = None, *, repo_root: Path | None = None) -> Path:
    """Create an empty private overlay without overwriting existing user data."""

    destination = path if path is not None else default_project_overlay_path()
    return create_private_json(
        destination,
        PROJECT_OVERLAY_TEMPLATE,
        repo_root=repo_root,
        label="project overlay",
        error=ProjectOverlayError,
    )


def _resolved(raw_path: str, base_dir: Path) -> Path:
    declared = Path(raw_path)
    if not declared.is_absolute():
        declared = base_dir / declared
    try:
        return declared.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ProjectOverlayError("project path could not be resolved") from exc


def apply_project_overlay(
    registry: AgentRegistry,
    overlay: ProjectOverlay,
    *,
    base_dir: Path,
) -> AgentRegistry:
    """Return a registry whose project routes are the registry routes plus overlay projects.

    Duplicate project ids and duplicate resolved paths across (or within) layers fail
    closed; the overlay never silently shadows a shared route. Inventory-only projects
    are recorded for path matching but never become routes.
    """

    if not isinstance(registry, AgentRegistry) or not isinstance(overlay, ProjectOverlay):
        raise ProjectOverlayError("registry and project overlay are required")
    if not isinstance(base_dir, Path) or not base_dir.is_absolute():
        raise ProjectOverlayError("project path base directory must be absolute")
    if not overlay.projects:
        return registry
    if set(overlay.projects) & (set(registry.project_routes) | set(registry.inventory_project_paths)):
        raise ProjectOverlayError("project overlay duplicates a registered project id")

    # Identity is filesystem-aware (inode when the path exists, normalized spelling
    # otherwise), so two spellings of one directory collide and fail closed.
    seen: set[tuple[object, ...]] = set()

    def claim(raw_path: str, project_id: str) -> None:
        keys = PathIdentity.of(_resolved(raw_path, base_dir)).collision_keys()
        if keys & seen:
            raise ProjectOverlayError("project overlay declares a path that is already used by another entry")
        seen.update(keys)

    for project_id, raw_path in registry.project_paths.items():
        claim(raw_path, project_id)
    for mapping in (registry.project_worktree_roots, registry.inventory_project_paths):
        for project_id, roots in mapping.items():
            for root in roots:
                claim(root, project_id)

    routes = dict(registry.project_routes)
    paths = dict(registry.project_paths)
    worktree_roots = dict(registry.project_worktree_roots)
    statuses = dict(registry.project_statuses)
    hold_agents = dict(registry.project_hold_agents)
    constraints = dict(registry.project_constraints)
    inventory = dict(registry.inventory_project_paths)
    for project_id, project in overlay.projects.items():
        declared = ([project.path] if project.path is not None else []) + list(project.worktree_roots)
        for raw_path in declared:
            claim(raw_path, project_id)
        if not project.routed:
            inventory[project_id] = tuple(declared)
            continue
        routes[project_id] = project.route_agent_id
        if project.path is not None:
            paths[project_id] = project.path
        if project.worktree_roots:
            worktree_roots[project_id] = project.worktree_roots
        statuses[project_id] = project.status
        if project.hold_agent_id is not None:
            hold_agents[project_id] = project.hold_agent_id
        if project.constraints:
            constraints[project_id] = project.constraints
    try:
        return AgentRegistry(
            default_agent_id=registry.default_agent_id,
            agents=registry.agents,
            max_registered_agents=registry.max_registered_agents,
            max_concurrent_agents=registry.max_concurrent_agents,
            project_routes=routes,
            project_paths=paths,
            project_worktree_roots=worktree_roots,
            project_statuses=statuses,
            project_hold_agents=hold_agents,
            project_constraints=constraints,
            inventory_project_paths=inventory,
        )
    except RegistryValidationError as exc:
        raise ProjectOverlayError("project overlay could not be merged with the registry") from exc
