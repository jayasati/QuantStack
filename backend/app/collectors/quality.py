"""Data Quality Engine (Volume 2, Prompt 2.11).

Every collector output passes through this quality gate before it is trusted
downstream. Produces a 0-100 quality score, a confidence adjustment, and a
collector health status. Poor data quality automatically reduces the
influence of a collector's signals.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from app.collectors.base import BaseCollector
from app.collectors.schema import CollectorOutput
from app.core.logging import get_logger

logger = get_logger(__name__)

# Component weights (sum = 1.0)
WEIGHTS = {
    "freshness": 0.25,
    "completeness": 0.25,
    "latency": 0.15,
    "schema_validity": 0.15,
    "duplicates": 0.10,
    "reliability": 0.10,
}


@dataclass(frozen=True)
class QualityAssessment:
    quality_score: float  # 0-100
    confidence_multiplier: float  # applied to record confidence
    health_status: str  # healthy | degraded | unhealthy
    components: dict[str, float]


class DataQualityEngine:
    def __init__(
        self,
        freshness_target_seconds: float = 120.0,
        latency_target_ms: float = 5_000.0,
    ) -> None:
        self._freshness_target = freshness_target_seconds
        self._latency_target = latency_target_ms
        self._seen_fingerprints: dict[str, set[str]] = {}

    def assess(
        self,
        collector: BaseCollector,
        records: list[CollectorOutput],
        latency_ms: float,
    ) -> QualityAssessment:
        components = {
            "freshness": self._score_freshness(records),
            "completeness": self._score_completeness(records),
            "latency": self._score_latency(latency_ms),
            "schema_validity": 100.0,  # records already passed pydantic validation
            "duplicates": self._score_duplicates(collector.name, records),
            "reliability": self._score_reliability(collector),
        }
        score = sum(components[name] * weight for name, weight in WEIGHTS.items())
        multiplier = max(0.1, min(1.0, score / 100.0))
        status = "healthy" if score >= 75 else "degraded" if score >= 40 else "unhealthy"
        return QualityAssessment(
            quality_score=round(score, 2),
            confidence_multiplier=round(multiplier, 4),
            health_status=status,
            components={k: round(v, 2) for k, v in components.items()},
        )

    def apply(
        self, records: list[CollectorOutput], assessment: QualityAssessment
    ) -> list[CollectorOutput]:
        for record in records:
            record.quality_score = assessment.quality_score
            record.confidence = round(
                record.confidence * assessment.confidence_multiplier, 4
            )
        return records

    # --- component scores -------------------------------------------------------

    def _score_freshness(self, records: list[CollectorOutput]) -> float:
        if not records:
            return 0.0
        now = datetime.now(UTC)
        ages = [
            record.freshness_seconds
            if record.freshness_seconds is not None
            else max(0.0, (now - record.timestamp).total_seconds())
            for record in records
        ]
        avg_age = sum(ages) / len(ages)
        return max(0.0, 100.0 * (1 - avg_age / (self._freshness_target * 4)))

    def _score_completeness(self, records: list[CollectorOutput]) -> float:
        if not records:
            return 0.0
        filled = sum(1 for r in records if r.normalized_value is not None)
        return 100.0 * filled / len(records)

    def _score_latency(self, latency_ms: float) -> float:
        return max(0.0, 100.0 * (1 - latency_ms / (self._latency_target * 4)))

    def _score_duplicates(self, collector_name: str, records: list[CollectorOutput]) -> float:
        if not records:
            return 0.0
        seen = self._seen_fingerprints.setdefault(collector_name, set())
        duplicates = 0
        for record in records:
            fingerprint = (
                f"{record.instrument}|{record.timestamp.isoformat()}|{record.normalized_value}"
            )
            if fingerprint in seen:
                duplicates += 1
            seen.add(fingerprint)
        if len(seen) > 50_000:  # bound memory
            self._seen_fingerprints[collector_name] = set(list(seen)[-10_000:])
        return 100.0 * (1 - duplicates / len(records))

    def _score_reliability(self, collector: BaseCollector) -> float:
        return 100.0 * (1 - collector.health.failure_rate)
