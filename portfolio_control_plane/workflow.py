"""Approval-bounded, domain-neutral workflow planning."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping
import uuid

from .registry import CapabilityRegistry, default_capability_registry


_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "plan_id",
        "created_at",
        "status",
        "preview",
        "approval_required",
        "approved_by",
        "approved_at",
        "approval_scope",
        "stages",
        "risk",
        "provenance",
    }
)
_STAGE_FIELDS = frozenset({"name", "status", "capability", "reason", "provenance"})
_RISK_FIELDS = frozenset({"external_writes", "manual_gates", "rollback"})


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _stage(
    name: str,
    status: str,
    capability: str,
    *,
    reason: str | None = None,
    provenance: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "capability": capability,
        "reason": reason,
        "provenance": list(provenance or []),
    }


def _validate_plan_shape(plan: Mapping[str, Any], *, approved: bool) -> None:
    if not isinstance(plan, Mapping):
        raise ValueError("workflow plan must be an object")
    unexpected = sorted(str(field) for field in plan if field not in _PLAN_FIELDS)
    if unexpected:
        raise ValueError("workflow plan contains unsupported fields: " + ", ".join(unexpected))
    required = {
        "schema_version",
        "plan_id",
        "created_at",
        "status",
        "preview",
        "approval_required",
        "stages",
        "risk",
        "provenance",
    }
    missing = sorted(field for field in required if field not in plan)
    if missing:
        raise ValueError("workflow plan is missing: " + ", ".join(missing))
    if plan["schema_version"] != "0.1":
        raise ValueError("unsupported workflow plan version")
    if not isinstance(plan["plan_id"], str) or not plan["plan_id"].strip():
        raise ValueError("workflow plan id must be non-empty")
    if not isinstance(plan["created_at"], str) or not plan["created_at"].strip():
        raise ValueError("workflow plan created_at must be non-empty")
    expected_status = "approved" if approved else "approval_required"
    if plan["status"] != expected_status:
        raise ValueError(f"workflow plan must be in {expected_status} state")
    if plan["preview"] is not (not approved) or plan["approval_required"] is not True:
        raise ValueError("workflow plan approval flags are invalid")
    if not isinstance(plan["stages"], list) or not plan["stages"]:
        raise ValueError("workflow plan stages must be a non-empty array")
    for index, stage in enumerate(plan["stages"]):
        if not isinstance(stage, Mapping):
            raise ValueError(f"workflow plan stages[{index}] must be an object")
        unexpected = sorted(str(field) for field in stage if field not in _STAGE_FIELDS)
        if unexpected:
            raise ValueError(
                f"workflow plan stages[{index}] contains unsupported fields: "
                + ", ".join(unexpected)
            )
        for field in ("name", "status", "capability"):
            if not isinstance(stage.get(field), str) or not stage[field].strip():
                raise ValueError(f"workflow plan stages[{index}].{field} is invalid")
        if stage["status"] not in {"ready", "verified"}:
            raise ValueError("workflow plan contains a blocked or incomplete stage")
        if stage.get("reason") is not None and not isinstance(stage.get("reason"), str):
            raise ValueError(f"workflow plan stages[{index}].reason is invalid")
        if not isinstance(stage.get("provenance"), list) or not all(
            isinstance(item, str) and item.strip() for item in stage["provenance"]
        ):
            raise ValueError(f"workflow plan stages[{index}].provenance is invalid")
    risk = plan["risk"]
    if not isinstance(risk, Mapping):
        raise ValueError("workflow plan risk must be an object")
    unexpected = sorted(str(field) for field in risk if field not in _RISK_FIELDS)
    if unexpected:
        raise ValueError("workflow plan risk contains unsupported fields: " + ", ".join(unexpected))
    if not isinstance(risk.get("rollback"), str) or not risk["rollback"].strip():
        raise ValueError("workflow plan risk.rollback is invalid")
    if risk.get("external_writes") != []:
        raise ValueError("workflow plan contains external writes")
    if not isinstance(risk.get("manual_gates"), list) or risk["manual_gates"]:
        raise ValueError("workflow plan has unresolved manual gates")
    if not isinstance(plan["provenance"], list) or not all(
        isinstance(item, str) and item.strip() for item in plan["provenance"]
    ):
        raise ValueError("workflow plan provenance must be an array of strings")
    if approved:
        for field in ("approved_by", "approval_scope", "approved_at"):
            if not isinstance(plan.get(field), str) or not plan[field].strip():
                raise ValueError(f"approved workflow plan requires {field}")


def build_cross_domain_plan(
    *,
    stages: list[Mapping[str, Any]],
    registry: CapabilityRegistry | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Build a preview-only plan from generic stages; never call an adapter."""

    if not isinstance(stages, list) or not stages:
        raise ValueError("stages must be a non-empty array")
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise ValueError("run_id must be a non-empty string or null")

    capability_registry = registry or default_capability_registry()
    normalized: list[dict[str, Any]] = []
    blockers: list[str] = []
    seen_names: set[str] = set()
    for index, item in enumerate(stages):
        if not isinstance(item, Mapping):
            raise ValueError(f"stages[{index}] must be an object")
        name = item.get("name")
        capability = item.get("capability")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"stages[{index}].name must be non-empty")
        if name in seen_names:
            raise ValueError(f"duplicate workflow stage: {name}")
        seen_names.add(name)
        if not isinstance(capability, str) or not capability.strip():
            raise ValueError(f"stages[{index}].capability must be non-empty")
        provenance = item.get("provenance", [])
        if not isinstance(provenance, list) or not all(
            isinstance(value, str) and value.strip() for value in provenance
        ):
            raise ValueError(f"stages[{index}].provenance must be a string array")
        action = item.get("action", "plan")
        tools = item.get("tools", [])
        if not isinstance(tools, list) or not all(isinstance(value, str) for value in tools):
            raise ValueError(f"stages[{index}].tools must be a string array")
        resolution = capability_registry.resolve(
            capability,
            action=action,
            requested_tools=tuple(tools),
            approval=False,
        )
        requested_status = item.get("status", "ready")
        if requested_status not in {"ready", "verified"}:
            blockers.append(f"stage {name} is not ready")
            normalized.append(
                _stage(name, "blocked", capability, reason="stage status is not promotable", provenance=provenance)
            )
            continue
        if not resolution.allowed:
            blockers.append(f"stage {name} capability is blocked")
            normalized.append(
                _stage(name, "blocked", capability, reason=resolution.reason, provenance=provenance)
            )
            continue
        normalized.append(_stage(name, requested_status, capability, provenance=provenance))

    plan = {
        "schema_version": "0.1",
        "plan_id": run_id or "plan-" + uuid.uuid4().hex,
        "created_at": _now(),
        "status": "HOLD_MANUAL" if blockers else "approval_required",
        "preview": True,
        "approval_required": True,
        "approved_by": None,
        "approved_at": None,
        "approval_scope": None,
        "stages": normalized,
        "risk": {
            "external_writes": [],
            "manual_gates": blockers,
            "rollback": "discard the plan; preserve source records",
        },
        "provenance": sorted({
            source
            for item in normalized
            for source in item["provenance"]
        }),
    }
    return plan


def approve_plan(plan: Mapping[str, Any], *, approved_by: str, scope: str) -> dict[str, Any]:
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ValueError("approved_by is required")
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("scope is required")
    _validate_plan_shape(plan, approved=False)
    updated = deepcopy(dict(plan))
    updated["status"] = "approved"
    updated["preview"] = False
    updated["approved_by"] = approved_by.strip()
    updated["approved_at"] = _now()
    updated["approval_scope"] = scope.strip()
    return updated


def execute_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Stop at the adapter boundary; approval alone never performs a write."""

    if not isinstance(plan, Mapping) or plan.get("status") != "approved":
        return {
            "status": "HOLD_MANUAL",
            "executed": False,
            "reason": "explicit approval is required",
            "external_writes": [],
        }
    try:
        _validate_plan_shape(plan, approved=True)
    except ValueError as exc:
        return {
            "status": "HOLD_MANUAL",
            "executed": False,
            "reason": str(exc),
            "external_writes": [],
        }
    return {
        "status": "NEEDS_MANUAL",
        "executed": False,
        "reason": "an explicitly configured adapter is required; no implicit execution is provided",
        "external_writes": [],
    }
