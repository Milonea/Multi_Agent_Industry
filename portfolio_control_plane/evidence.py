"""Safe unified evidence reports and target-specific minimal renderers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import ip_address
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from .contracts import validate_run_result


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:xox[baprs]-|gh[pousr]_)[A-Za-z0-9_-]+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]+"),
)
_PRIVATE_KEYS = frozenset(
    {"credential", "token", "secret", "password", "raw_conversation", "event_raw", "message_transcript"}
)
_FRESHNESS_VALUES = frozenset({"fresh", "partial", "stale", "unknown"})


def redact_text(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _safe_summary(value: Any, *, key: str = "") -> Any:
    normalized = key.lower()
    if normalized in _PRIVATE_KEYS or any(part in normalized for part in ("token", "secret", "password", "credential", "raw_")):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(k): _safe_summary(v, key=str(k)) for k, v in value.items() if str(k).lower() not in _PRIVATE_KEYS}
    if isinstance(value, list):
        return [_safe_summary(item, key=key) for item in value]
    return value


def _safe_evidence_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        hostname = parsed.hostname
        if not hostname:
            return None
        normalized_hostname = hostname.casefold().rstrip(".")
        if normalized_hostname in {"localhost", "local"} or normalized_hostname.endswith(
            (".localhost", ".local", ".internal", ".home.arpa")
        ):
            return None
        try:
            address = ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            return None
    except ValueError:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc}{parsed.path or '/'}"


@dataclass(frozen=True)
class EvidenceLink:
    label: str
    url: str
    freshness: str = "unknown"

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("evidence link label must be non-empty")
        safe_url = _safe_evidence_url(self.url)
        if safe_url is None:
            raise ValueError("evidence link URL must be a public HTTP(S) URL")
        if not isinstance(self.freshness, str) or self.freshness not in _FRESHNESS_VALUES:
            raise ValueError("evidence link freshness is invalid")
        object.__setattr__(self, "label", self.label.strip())
        object.__setattr__(self, "url", safe_url)

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label, "url": self.url, "freshness": self.freshness}


@dataclass(frozen=True)
class EvidenceReport:
    run_id: str
    project: str
    capability: str
    agent: str
    base_sha: str
    head_sha: str
    tests: tuple[dict[str, Any], ...]
    validation_cycles: tuple[dict[str, Any], ...]
    manual_gates: tuple[dict[str, Any], ...]
    evidence_links: tuple[EvidenceLink, ...]
    final_state: str
    changed_paths: tuple[str, ...]
    external_writes: dict[str, Any]
    risks: tuple[str, ...]
    rollback: str

    @classmethod
    def from_envelope(cls, envelope: Mapping[str, Any], *, agent: str) -> "EvidenceReport":
        data = validate_run_result(envelope)
        run = data["run"]
        result = data["result"]
        validation = data["validation"]
        if not isinstance(agent, str) or not agent.strip():
            raise ValueError("agent is required")
        links = []
        for index, item in enumerate(result.get("evidence_links", [])):
            if not isinstance(item, Mapping):
                raise ValueError(f"result.evidence_links[{index}] must be an object")
            freshness = item.get("freshness", "unknown")
            if not isinstance(freshness, str):
                raise ValueError(f"result.evidence_links[{index}].freshness is invalid")
            try:
                links.append(EvidenceLink(item.get("label", ""), item.get("url", ""), freshness))
            except ValueError as exc:
                raise ValueError(f"result.evidence_links[{index}] is invalid") from exc
        return cls(
            run_id=run["id"],
            project=run["project"],
            capability=run["capability"],
            agent=redact_text(agent),
            base_sha=run["base_sha"],
            head_sha=run["head_sha"],
            tests=tuple(_safe_summary(item) for item in result["tests"]),
            validation_cycles=(
                {
                    "validator": validation["validator"],
                    "cycle": validation["cycle"],
                    "findings": _safe_summary(validation["findings"]),
                    "next_action": _safe_summary(validation["next_action"]),
                },
            ),
            manual_gates=tuple(_safe_summary(item) for item in result.get("manual_gates", [])),
            evidence_links=tuple(links),
            final_state=result["status"],
            changed_paths=tuple(result["changed_paths"]),
            external_writes=dict(result["external_writes"]),
            risks=tuple(_safe_summary(item) for item in result["risks"]),
            rollback=redact_text(result["rollback"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "0.1",
            "run_id": self.run_id,
            "project": self.project,
            "capability": self.capability,
            "agent": self.agent,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "tests": list(self.tests),
            "validation_cycles": list(self.validation_cycles),
            "manual_gates": list(self.manual_gates),
            "evidence_links": [link.to_dict() for link in self.evidence_links],
            "final_state": self.final_state,
            "changed_paths": list(self.changed_paths),
            "external_writes": dict(self.external_writes),
            "risks": list(self.risks),
            "rollback": self.rollback,
        }

    def stale_links(self, *, now: datetime | None = None) -> tuple[str, ...]:
        current = now or datetime.now(timezone.utc)
        _ = current  # Reserved for future expiry metadata without changing the contract.
        return tuple(link.label for link in self.evidence_links if link.freshness != "fresh")

    def render(self, target: str) -> str:
        if not isinstance(target, str) or re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", target) is None:
            raise ValueError("target must be a lowercase integration identifier")
        lines = [
            f"Run {self.run_id} · {self.project} · {self.final_state}",
            f"Capability: {self.capability} · head: {self.head_sha}",
            f"Tests: {', '.join(f'{item.get('name')}={item.get('status')}' for item in self.tests) or 'none'}",
            f"Changed paths: {len(self.changed_paths)} · external writes: {self.external_writes.get('count', 0)}",
        ]
        if self.stale_links():
            lines.append("Stale evidence: " + ", ".join(self.stale_links()))
        if self.manual_gates:
            lines.append("Manual gates: " + str(len(self.manual_gates)))
        return "\n".join(lines)
