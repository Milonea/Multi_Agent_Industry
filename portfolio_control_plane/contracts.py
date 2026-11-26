"""Versioned run/result/validation envelope and strict validation helpers."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping


CONTRACT_VERSION = "1.0"
STATUS_VALUES = frozenset(
    {"PASS", "HOLD_AUTO", "HOLD_MANUAL", "FAIL_TERMINAL", "NEEDS_MANUAL"}
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TEST_STATUS_VALUES = frozenset({"PASS", "FAIL", "SKIP", "UNKNOWN"})
_ENVELOPE_FIELDS = frozenset({"schema_version", "run", "result", "validation"})
_RUN_FIELDS = frozenset(
    {"id", "project", "capability", "request_summary", "base_sha", "head_sha"}
)
_RESULT_FIELDS = frozenset(
    {
        "status",
        "summary",
        "changed_paths",
        "external_writes",
        "tests",
        "risks",
        "rollback",
        "manual_gates",
        "evidence_links",
    }
)
_EXTERNAL_WRITE_FIELDS = frozenset({"attempted", "count", "targets"})
_VALIDATION_FIELDS = frozenset({"validator", "cycle", "findings", "next_action"})
_EVIDENCE_LINK_FIELDS = frozenset({"label", "url", "freshness"})
_EVIDENCE_FRESHNESS_VALUES = frozenset({"fresh", "partial", "stale", "unknown"})


class ContractError(ValueError):
    """Raised when a run contract is malformed or unsafe to consume."""


def _required(mapping: Mapping[str, Any], names: tuple[str, ...], scope: str) -> None:
    missing = [name for name in names if name not in mapping]
    if missing:
        raise ContractError(f"{scope} is missing required fields: {', '.join(missing)}")


def _no_unexpected(mapping: Mapping[str, Any], allowed: frozenset[str], scope: str) -> None:
    unexpected = sorted(
        (str(name) for name in mapping if name not in allowed),
        key=str,
    )
    if unexpected:
        raise ContractError(f"{scope} contains unsupported fields: {', '.join(unexpected)}")


def _sha(value: Any, field: str) -> None:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise ContractError(f"{field} must be a 40-character lowercase SHA")


def _string_list(value: Any, field: str, *, unique: bool = False) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ContractError(f"{field} must be an array of strings")
    if unique and len(value) != len(set(value)):
        raise ContractError(f"{field} must not contain duplicates")


def _validate_external_writes(value: Any) -> None:
    if not isinstance(value, dict):
        raise ContractError("result.external_writes must be an object")
    _no_unexpected(value, _EXTERNAL_WRITE_FIELDS, "result.external_writes")
    _required(value, ("attempted", "count", "targets"), "result.external_writes")
    if not isinstance(value["attempted"], bool):
        raise ContractError("result.external_writes.attempted must be boolean")
    if not isinstance(value["count"], int) or isinstance(value["count"], bool):
        raise ContractError("result.external_writes.count must be a non-negative integer")
    if value["count"] < 0:
        raise ContractError("result.external_writes.count must be a non-negative integer")
    _string_list(value["targets"], "result.external_writes.targets", unique=True)
    if not value["attempted"] and (value["count"] != 0 or value["targets"]):
        raise ContractError(
            "external_writes must be empty when attempted is false"
        )
    if value["attempted"] and (value["count"] == 0 or not value["targets"]):
        raise ContractError(
            "external_writes must name at least one target when attempted is true"
        )


def _validate_result_metadata(result: Mapping[str, Any]) -> None:
    for index, item in enumerate(result.get("manual_gates", [])):
        if not isinstance(item, Mapping):
            raise ContractError(f"result.manual_gates[{index}] must be an object")
    for index, item in enumerate(result.get("evidence_links", [])):
        if not isinstance(item, Mapping):
            raise ContractError(f"result.evidence_links[{index}] must be an object")
        _no_unexpected(item, _EVIDENCE_LINK_FIELDS, f"result.evidence_links[{index}]")
        _required(item, ("label", "url"), f"result.evidence_links[{index}]")
        for field in ("label", "url"):
            if not isinstance(item[field], str) or not item[field].strip():
                raise ContractError(f"result.evidence_links[{index}].{field} must be non-empty")
        if "freshness" in item and (
            not isinstance(item["freshness"], str)
            or item["freshness"] not in _EVIDENCE_FRESHNESS_VALUES
        ):
            raise ContractError(f"result.evidence_links[{index}].freshness is invalid")


def validate_run_result(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a defensive copy of a v1 run/result envelope."""

    if not isinstance(envelope, Mapping):
        raise ContractError("run result must be an object")
    _no_unexpected(envelope, _ENVELOPE_FIELDS, "envelope")
    _required(envelope, ("schema_version", "run", "result", "validation"), "envelope")
    if envelope["schema_version"] != CONTRACT_VERSION:
        raise ContractError(f"unsupported contract version: {envelope['schema_version']!r}")

    run = envelope["run"]
    if not isinstance(run, Mapping):
        raise ContractError("run must be an object")
    _no_unexpected(run, _RUN_FIELDS, "run")
    _required(
        run,
        ("id", "project", "capability", "request_summary", "base_sha", "head_sha"),
        "run",
    )
    for field in ("id", "project", "capability", "request_summary"):
        if not isinstance(run[field], str) or not run[field].strip():
            raise ContractError(f"run.{field} must be a non-empty string")
    _sha(run["base_sha"], "run.base_sha")
    _sha(run["head_sha"], "run.head_sha")

    result = envelope["result"]
    if not isinstance(result, Mapping):
        raise ContractError("result must be an object")
    _no_unexpected(result, _RESULT_FIELDS, "result")
    _required(
        result,
        (
            "status",
            "summary",
            "changed_paths",
            "external_writes",
            "tests",
            "risks",
            "rollback",
        ),
        "result",
    )
    if not isinstance(result["status"], str) or result["status"] not in STATUS_VALUES:
        raise ContractError(f"unsupported result.status: {result['status']!r}")
    for field in ("summary", "rollback"):
        if not isinstance(result[field], str) or not result[field].strip():
            raise ContractError(f"result.{field} must be a non-empty string")
    _string_list(result["changed_paths"], "result.changed_paths", unique=True)
    _string_list(result["risks"], "result.risks")
    _validate_external_writes(result["external_writes"])
    for field in ("manual_gates", "evidence_links"):
        if field in result and not isinstance(result[field], list):
            raise ContractError(f"result.{field} must be an array")
    _validate_result_metadata(result)
    if not isinstance(result["tests"], list):
        raise ContractError("result.tests must be an array")
    for index, test in enumerate(result["tests"]):
        if not isinstance(test, Mapping):
            raise ContractError(f"result.tests[{index}] must be an object")
        _required(test, ("name", "status"), f"result.tests[{index}]")
        if not isinstance(test["name"], str) or not test["name"].strip():
            raise ContractError(f"result.tests[{index}].name must be non-empty")
        if not isinstance(test["status"], str) or test["status"] not in _TEST_STATUS_VALUES:
            raise ContractError(f"result.tests[{index}].status is invalid")

    validation = envelope["validation"]
    if not isinstance(validation, Mapping):
        raise ContractError("validation must be an object")
    _no_unexpected(validation, _VALIDATION_FIELDS, "validation")
    _required(validation, ("validator", "cycle", "findings", "next_action"), "validation")
    if not isinstance(validation["validator"], str) or not validation["validator"].strip():
        raise ContractError("validation.validator must be a non-empty string")
    if not isinstance(validation["cycle"], int) or isinstance(validation["cycle"], bool):
        raise ContractError("validation.cycle must be a non-negative integer")
    if validation["cycle"] < 0:
        raise ContractError("validation.cycle must be a non-negative integer")
    if not isinstance(validation["findings"], list):
        raise ContractError("validation.findings must be an array")
    if not isinstance(validation["next_action"], str) or not validation["next_action"].strip():
        raise ContractError("validation.next_action must be a non-empty string")

    return deepcopy(dict(envelope))


def make_run_result(
    *,
    run: Mapping[str, Any],
    result: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> dict[str, Any]:
    """Build and validate a v1 envelope in one operation."""

    envelope = {
        "schema_version": CONTRACT_VERSION,
        "run": dict(run),
        "result": dict(result),
        "validation": dict(validation),
    }
    return validate_run_result(envelope)
