"""Privacy-preserving portfolio aggregate metrics."""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping


_PROJECT_STATES = frozenset({"unknown", "planned", "in_progress", "blocked", "verified", "stale"})
_SOURCE_STATUSES = frozenset({"complete", "fresh", "partial", "stale", "unknown", "unavailable"})
_MANUAL_GATE_STATUSES = frozenset({"not_required", "pending", "passed", "blocked"})
_RUN_STATES = frozenset({"PASS", "HOLD_AUTO", "HOLD_MANUAL", "FAIL_TERMINAL", "NEEDS_MANUAL"})


def _safe_records(items: Iterable[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], int]:
    """Materialize only object-shaped records and count rejected inputs."""

    try:
        raw_items = list(items)
    except Exception:
        return [], 1
    records = [item for item in raw_items if isinstance(item, Mapping)]
    return records, len(raw_items) - len(records)


def _has_repeated_hold_cycle(report: Mapping[str, Any]) -> tuple[bool, bool]:
    cycles = report.get("validation_cycles")
    if not isinstance(cycles, list):
        return False, True
    repeated = False
    malformed = False
    for cycle in cycles:
        if not isinstance(cycle, Mapping):
            malformed = True
            continue
        value = cycle.get("cycle", 0)
        if type(value) is not int or value < 0:
            malformed = True
            continue
        repeated = repeated or value > 0
    return repeated, malformed


def _valid_external_writes(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    attempted = value.get("attempted")
    count = value.get("count")
    targets = value.get("targets")
    if (
        not isinstance(attempted, bool)
        or type(count) is not int
        or count < 0
        or not isinstance(targets, list)
        or not all(isinstance(target, str) and target.strip() for target in targets)
    ):
        return False
    if not attempted and (count != 0 or targets):
        return False
    if attempted:
        return count > 0 and bool(targets)
    return count == 0 and not targets


def aggregate_observability(
    snapshots: Iterable[Mapping[str, Any]],
    reports: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate safe counts only; partial and unknown are never healthy."""

    snapshot_items, malformed_snapshots = _safe_records(snapshots)
    report_items, malformed_reports = _safe_records(reports)
    statuses = Counter(str(item.get("project_state", "unknown")) for item in snapshot_items)
    source_statuses = Counter(str(item.get("status", "unknown")) for item in snapshot_items)
    final_states = Counter(str(item.get("final_state", "unknown")) for item in report_items)
    stale_repositories: list[str] = []
    malformed_snapshot_fields = 0
    for item in snapshot_items:
        repository = item.get("repository")
        repository_name = repository.strip() if isinstance(repository, str) and repository.strip() else "<unknown>"
        source_status = item.get("status")
        head_sha = item.get("head_sha")
        manual_gate = item.get("manual_gate")
        project_state_valid = isinstance(item.get("project_state"), str) and item.get("project_state") in _PROJECT_STATES
        source_status_valid = isinstance(source_status, str) and source_status in _SOURCE_STATUSES
        manual_gate_valid = True
        if manual_gate is not None:
            manual_gate_valid = (
                isinstance(manual_gate, Mapping)
                and isinstance(manual_gate.get("status"), str)
                and manual_gate.get("status") in _MANUAL_GATE_STATUSES
            )
        if (
            not project_state_valid
            or not source_status_valid
            or not (head_sha is None or isinstance(head_sha, str))
            or not manual_gate_valid
        ):
            malformed_snapshot_fields += 1
        if (
            item.get("project_state") == "stale"
            or not source_status_valid
            or source_status in {"partial", "unknown", "unavailable", "stale"}
            or not isinstance(head_sha, str)
            or not head_sha
            or not project_state_valid
            or not manual_gate_valid
        ):
            stale_repositories.append(repository_name)
    pending_manual_gates = sum(
        1
        for item in snapshot_items
        if isinstance(item.get("manual_gate"), Mapping)
        and isinstance(item["manual_gate"].get("status"), str)
        and item["manual_gate"].get("status") in {"pending", "blocked"}
    )
    repeated_hold_runs = 0
    malformed_report_fields = 0
    for item in report_items:
        repeated, malformed = _has_repeated_hold_cycle(item)
        final_state = item.get("final_state")
        malformed_report_fields += int(
            malformed
            or not isinstance(final_state, str)
            or final_state not in _RUN_STATES
            or not _valid_external_writes(item.get("external_writes"))
        )
        if isinstance(final_state, str) and final_state in {"HOLD_AUTO", "HOLD_MANUAL", "NEEDS_MANUAL"} and repeated:
            repeated_hold_runs += 1
    external_write_attempts = sum(
        1
        for item in report_items
        if isinstance(item.get("external_writes"), Mapping) and item["external_writes"].get("attempted") is True
    )
    return {
        "schema_version": "0.1",
        "project_count": len(snapshot_items),
        "malformed_snapshot_count": malformed_snapshots,
        "malformed_snapshot_field_count": malformed_snapshot_fields,
        "malformed_report_count": malformed_reports,
        "malformed_report_field_count": malformed_report_fields,
        "project_state_counts": dict(sorted(statuses.items())),
        "source_status_counts": dict(sorted(source_statuses.items())),
        "run_state_counts": dict(sorted(final_states.items())),
        "stale_repositories": stale_repositories,
        "pending_manual_gates": pending_manual_gates,
        "repeated_hold_runs": repeated_hold_runs,
        "external_write_attempts": external_write_attempts,
        "healthy": (
            not stale_repositories
            and not pending_manual_gates
            and not any(source_statuses[key] for key in ("partial", "unknown"))
            and not malformed_snapshots
            and not malformed_snapshot_fields
            and not malformed_reports
            and not malformed_report_fields
        ),
        "retention": {"raw_conversation": False, "personal_event_data": False, "credentials": False},
    }
