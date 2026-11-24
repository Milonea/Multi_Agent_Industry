"""Capability and validation-policy registries with fail-closed resolution."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping


READ_ACTIONS = frozenset({"read", "analyze", "plan", "validate"})
WRITE_ACTIONS = frozenset({"write", "create", "edit", "publish", "delete", "deploy"})
ACTION_VALUES = READ_ACTIONS | WRITE_ACTIONS
REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
MATURITIES = frozenset({"experimental", "verified", "production"})
VALIDATION_STATES = frozenset({"PASS", "HOLD_AUTO", "HOLD_MANUAL", "FAIL_TERMINAL", "NEEDS_MANUAL"})
ALLOWLIST_FINDINGS = frozenset(
    {"lint", "formatter", "deterministic_test", "schema_import", "generated_drift", "documentation_mismatch", "safe_local_compile"}
)
DENYLIST_FINDINGS = frozenset(
    {"credential", "oauth_scope", "privacy", "deletion", "migration", "deployment", "cost", "external_write", "destructive_git", "manual_host_application", "platform_trust", "ambiguous_product_decision"}
)


class RegistryError(ValueError):
    """Raised when a registry entry or binding violates its contract."""


@dataclass(frozen=True)
class CapabilitySpec:
    capability_id: str
    provider_repository: str
    supported_actions: tuple[str, ...]
    access: str
    required_approval: bool
    allowed_tools: tuple[str, ...]
    input_schema: str
    output_schema: str
    validator: str
    rollback_strategy: str
    maturity: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilitySpec":
        if not isinstance(data, Mapping):
            raise RegistryError("capability must be an object")
        fields = (
            "capability_id",
            "provider_repository",
            "supported_actions",
            "access",
            "required_approval",
            "allowed_tools",
            "input_schema",
            "output_schema",
            "validator",
            "rollback_strategy",
            "maturity",
        )
        unexpected = sorted(str(field) for field in data if field not in fields)
        if unexpected:
            raise RegistryError("capability contains unsupported fields: " + ", ".join(unexpected))
        missing = [field for field in fields if field not in data]
        if missing:
            raise RegistryError(f"capability is missing: {', '.join(missing)}")
        if not isinstance(data["capability_id"], str) or not data["capability_id"].strip():
            raise RegistryError("capability_id must be non-empty")
        if not isinstance(data["access"], str) or data["access"] not in {"read", "write"}:
            raise RegistryError("access must be read or write")
        if not isinstance(data["maturity"], str) or data["maturity"] not in MATURITIES:
            raise RegistryError("maturity is invalid")
        if not isinstance(data["required_approval"], bool):
            raise RegistryError("required_approval must be boolean")
        for field in ("supported_actions", "allowed_tools"):
            if not isinstance(data[field], list) or not all(isinstance(item, str) and item for item in data[field]):
                raise RegistryError(f"{field} must be a non-empty string array")
            if not data[field] or len(data[field]) != len(set(data[field])):
                raise RegistryError(f"{field} must be a non-empty unique string array")
        if not data["supported_actions"]:
            raise RegistryError("supported_actions must not be empty")
        unsupported_actions = sorted(set(data["supported_actions"]) - ACTION_VALUES)
        if unsupported_actions:
            raise RegistryError(
                "supported_actions contains unknown actions: "
                + ", ".join(unsupported_actions)
            )
        if data["access"] == "read" and set(data["supported_actions"]) & WRITE_ACTIONS:
            raise RegistryError("read capability cannot support write actions")
        for field in ("provider_repository", "input_schema", "output_schema", "validator", "rollback_strategy"):
            if not isinstance(data[field], str) or not data[field].strip():
                raise RegistryError(f"{field} must be non-empty")
        if not REPOSITORY_RE.fullmatch(data["provider_repository"]):
            raise RegistryError("provider_repository must be an owner/name string")
        if data["access"] == "write" and not data["required_approval"]:
            raise RegistryError("write capability must require explicit approval")
        return cls(
            capability_id=data["capability_id"],
            provider_repository=data["provider_repository"],
            supported_actions=tuple(data["supported_actions"]),
            access=data["access"],
            required_approval=data["required_approval"],
            allowed_tools=tuple(data["allowed_tools"]),
            input_schema=data["input_schema"],
            output_schema=data["output_schema"],
            validator=data["validator"],
            rollback_strategy=data["rollback_strategy"],
            maturity=data["maturity"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "provider_repository": self.provider_repository,
            "supported_actions": list(self.supported_actions),
            "access": self.access,
            "required_approval": self.required_approval,
            "allowed_tools": list(self.allowed_tools),
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "validator": self.validator,
            "rollback_strategy": self.rollback_strategy,
            "maturity": self.maturity,
        }


@dataclass(frozen=True)
class CapabilityResolution:
    capability_id: str
    allowed: bool
    reason: str
    access: str | None
    provider_repository: str | None
    contract_version: str = "1.0"


class CapabilityRegistry:
    def __init__(self, specs: Mapping[str, CapabilitySpec]) -> None:
        self._specs = dict(specs)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilityRegistry":
        if not isinstance(data, Mapping):
            raise RegistryError("invalid capability registry envelope")
        if data.get("schema_version") != "0.1" or not isinstance(data.get("capabilities"), list):
            raise RegistryError("invalid capability registry envelope")
        specs: dict[str, CapabilitySpec] = {}
        for item in data["capabilities"]:
            spec = CapabilitySpec.from_dict(item)
            if spec.capability_id in specs:
                raise RegistryError(f"duplicate capability: {spec.capability_id}")
            specs[spec.capability_id] = spec
        return cls(specs)

    def resolve(
        self,
        capability_id: str,
        *,
        action: str = "read",
        requested_tools: tuple[str, ...] = tuple(),
        approval: bool = False,
    ) -> CapabilityResolution:
        if not isinstance(capability_id, str) or not capability_id.strip():
            return CapabilityResolution(str(capability_id), False, "capability identifier is invalid", None, None)
        spec = self._specs.get(capability_id)
        if spec is None:
            return CapabilityResolution(capability_id, False, "unknown capability", None, None)
        if not isinstance(action, str) or action not in ACTION_VALUES or action not in spec.supported_actions:
            return CapabilityResolution(capability_id, False, "action is not supported", spec.access, spec.provider_repository)
        if not isinstance(requested_tools, tuple) or not all(isinstance(tool, str) for tool in requested_tools):
            return CapabilityResolution(capability_id, False, "requested tools are invalid", spec.access, spec.provider_repository)
        if not isinstance(approval, bool):
            return CapabilityResolution(capability_id, False, "approval flag is invalid", spec.access, spec.provider_repository)
        if any(tool not in spec.allowed_tools for tool in requested_tools):
            return CapabilityResolution(capability_id, False, "requested tool exceeds provider binding", spec.access, spec.provider_repository)
        if spec.access == "read" and (
            action in WRITE_ACTIONS
            or any(
                tool.startswith(("write:", "delete:", "publish:", "deploy:"))
                for tool in requested_tools
            )
        ):
            return CapabilityResolution(capability_id, False, "read capability cannot bind a write endpoint", spec.access, spec.provider_repository)
        if spec.required_approval and not approval:
            return CapabilityResolution(capability_id, False, "explicit approval is required", spec.access, spec.provider_repository)
        return CapabilityResolution(capability_id, True, "resolved", spec.access, spec.provider_repository)

    def get(self, capability_id: str) -> CapabilitySpec:
        try:
            return self._specs[capability_id]
        except KeyError as exc:
            raise RegistryError(f"unknown capability: {capability_id}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": "0.1", "capabilities": [self._specs[key].to_dict() for key in sorted(self._specs)]}


@dataclass(frozen=True)
class ValidationPolicy:
    project: str
    work_class: str
    max_cycles: int = 3
    allowlist: frozenset[str] = ALLOWLIST_FINDINGS
    denylist: frozenset[str] = DENYLIST_FINDINGS

    def classify(self, finding: str) -> str:
        if finding in self.allowlist:
            return "AUTO_REWORK"
        if finding in self.denylist:
            return "BLOCKED"
        return "BLOCKED"

    def decide(
        self,
        finding: str,
        *,
        cycle: int,
        previous_head_sha: str | None,
        current_head_sha: str | None,
    ) -> dict[str, Any]:
        classification = self.classify(finding)
        if classification == "BLOCKED":
            return {"state": "HOLD_MANUAL", "classification": classification, "reason": "finding is outside automatic rework boundary"}
        if cycle >= self.max_cycles:
            return {"state": "FAIL_TERMINAL", "classification": classification, "reason": "maximum automatic recovery cycles exceeded"}
        if previous_head_sha and current_head_sha == previous_head_sha:
            return {"state": "HOLD_MANUAL", "classification": classification, "reason": "a new head SHA is required before revalidation"}
        return {"state": "HOLD_AUTO", "classification": classification, "reason": "scoped automatic rework is allowed"}


class ValidationPolicyRegistry:
    def __init__(self, policies: Mapping[tuple[str, str], ValidationPolicy]) -> None:
        self._policies = dict(policies)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ValidationPolicyRegistry":
        if not isinstance(data, Mapping) or data.get("schema_version") != "0.1" or not isinstance(data.get("policies"), list):
            raise RegistryError("invalid validation policy registry envelope")
        policies: dict[tuple[str, str], ValidationPolicy] = {}
        for item in data["policies"]:
            if not isinstance(item, Mapping):
                raise RegistryError("validation policy must be an object")
            project = item.get("project")
            work_class = item.get("work_class")
            if not isinstance(project, str) or not project.strip() or not isinstance(work_class, str) or not work_class.strip():
                raise RegistryError("policy project/work_class must be strings")
            max_cycles = item.get("max_cycles", 3)
            if type(max_cycles) is not int or max_cycles < 1:
                raise RegistryError("policy max_cycles must be positive")
            key = (project, work_class)
            if key in policies:
                raise RegistryError(f"duplicate validation policy: {key}")
            policies[key] = ValidationPolicy(project, work_class, max_cycles)
        return cls(policies)

    def get(self, project: str, work_class: str) -> ValidationPolicy:
        try:
            return self._policies[(project, work_class)]
        except KeyError as exc:
            raise RegistryError(f"unknown validation policy: {project}/{work_class}") from exc


def _spec(capability_id: str, repository: str, actions: tuple[str, ...], *, access: str = "read", approval: bool = False, tools: tuple[str, ...] = ("read:mock",), maturity: str = "verified") -> CapabilitySpec:
    return CapabilitySpec(
        capability_id=capability_id,
        provider_repository=repository,
        supported_actions=actions,
        access=access,
        required_approval=approval,
        allowed_tools=tools,
        input_schema=f"schemas/{capability_id}.input.json",
        output_schema=f"schemas/{capability_id}.output.json",
        validator="portfolio_control_plane.contracts.validate_run_result",
        rollback_strategy="disable-capability-and-preserve-existing-records",
        maturity=maturity,
    )


def default_capability_registry() -> CapabilityRegistry:
    specs = {
        item.capability_id: item
        for item in (
            _spec("agent.route", "framework/core", ("read", "plan"), tools=("read:registry",)),
            _spec("task.contract.validate", "framework/core", ("read", "validate"), tools=("read:contracts",)),
            _spec("portfolio.reality.scan", "framework/core", ("read", "analyze"), tools=("read:local-git",)),
            _spec("portfolio.work.select", "framework/core", ("read", "analyze"), tools=("read:portfolio-state",)),
            _spec("workflow.plan", "framework/core", ("read", "plan"), tools=("read:workflow-input",)),
        )
    }
    return CapabilityRegistry(specs)


def default_validation_policy_registry() -> ValidationPolicyRegistry:
    return ValidationPolicyRegistry(
        {
            ("framework", "core"): ValidationPolicy("framework", "core"),
        }
    )
