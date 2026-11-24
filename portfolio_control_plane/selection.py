"""Deterministic, explainable work-selection recommendations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


STATE_QUALITIES = frozenset({"fresh", "known", "partial", "stale", "unknown"})


@dataclass(frozen=True)
class WorkCandidate:
    id: str
    repository: str
    blocker_removal: int = 0
    user_impact: int = 0
    urgency: int = 0
    validation_confidence: int = 0
    dependency_unlock: int = 0
    risk: int = 0
    manual_cost: int = 0
    implementation_cost: int = 0
    state_quality: str = "known"
    blocker: str | None = None
    deadline: str | None = None
    manual_gate: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("candidate id is required")
        if not isinstance(self.repository, str) or not self.repository.strip():
            raise ValueError("candidate repository is required")
        for field in (
            "blocker_removal",
            "user_impact",
            "urgency",
            "validation_confidence",
            "dependency_unlock",
            "risk",
            "manual_cost",
            "implementation_cost",
        ):
            value = getattr(self, field)
            if type(value) is not int or not 0 <= value <= 10:
                raise ValueError(f"candidate {field} must be an integer from 0 to 10")
        if not isinstance(self.state_quality, str) or self.state_quality not in STATE_QUALITIES:
            raise ValueError(f"candidate state_quality is invalid: {self.state_quality!r}")
        for field in ("blocker", "deadline", "manual_gate"):
            value = getattr(self, field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"candidate {field} must be a string or null")

    @classmethod
    def from_dict(cls, item: Mapping[str, Any]) -> "WorkCandidate":
        if not isinstance(item, Mapping):
            raise ValueError("candidate must be an object")
        if (
            not isinstance(item.get("id"), str)
            or not item["id"].strip()
            or not isinstance(item.get("repository"), str)
            or not item["repository"].strip()
        ):
            raise ValueError("candidate id and repository are required")
        values: dict[str, Any] = {field: item.get(field, getattr(cls, field, 0)) for field in (
            "blocker_removal", "user_impact", "urgency", "validation_confidence", "dependency_unlock", "risk", "manual_cost", "implementation_cost"
        )}
        for field, value in values.items():
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 10:
                raise ValueError(f"candidate {field} must be an integer from 0 to 10")
        state_quality = item.get("state_quality", "known")
        if not isinstance(state_quality, str) or state_quality not in STATE_QUALITIES:
            raise ValueError(f"candidate state_quality is invalid: {state_quality!r}")
        for field in ("blocker", "deadline", "manual_gate"):
            value = item.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"candidate {field} must be a string or null")
        return cls(
            id=item["id"],
            repository=item["repository"],
            **values,
            state_quality=state_quality,
            blocker=item.get("blocker"),
            deadline=item.get("deadline"),
            manual_gate=item.get("manual_gate"),
        )

    def score(self) -> tuple[float, dict[str, float]]:
        components = {
            "blocker_removal": self.blocker_removal * 5,
            "user_impact": self.user_impact * 3,
            "urgency": self.urgency * 3,
            "validation_confidence": self.validation_confidence * 2,
            "dependency_unlock": self.dependency_unlock * 2,
            "risk": self.risk * -3,
            "manual_cost": self.manual_cost * -2,
            "implementation_cost": self.implementation_cost * -1,
        }
        if self.state_quality in {"unknown", "partial", "stale"}:
            components["state_uncertainty"] = -8
        return sum(components.values()), components


class WorkSelectionEngine:
    """Recommendation-only engine; it never executes or mutates candidates."""

    def select(self, candidates: Iterable[Mapping[str, Any] | WorkCandidate]) -> list[dict[str, Any]]:
        ranked: list[dict[str, Any]] = []
        for item in candidates:
            candidate = item if isinstance(item, WorkCandidate) else WorkCandidate.from_dict(item)
            score, components = candidate.score()
            reasons: list[str] = []
            if candidate.blocker:
                reasons.append(f"blocker: {candidate.blocker}")
            if candidate.deadline:
                reasons.append(f"deadline: {candidate.deadline}")
            if candidate.dependency_unlock:
                reasons.append(f"unlocks {candidate.dependency_unlock} downstream task(s)")
            if candidate.manual_gate:
                reasons.append(f"manual gate: {candidate.manual_gate}")
            if candidate.state_quality in {"unknown", "partial", "stale"}:
                reasons.append("state is uncertain; recommendation is penalized")
            ranked.append(
                {
                    "id": candidate.id,
                    "repository": candidate.repository,
                    "score": score,
                    "score_components": components,
                    "reasons": reasons,
                    "recommendation_only": True,
                }
            )
        return sorted(ranked, key=lambda item: (-item["score"], item["id"], item["repository"]))
