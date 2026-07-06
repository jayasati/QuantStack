"""Data Quality Engine (Volume 2, Prompt 2.11).

Every collector output passes through this quality gate before it is trusted
downstream. Evaluates the spec's eight dimensions — freshness, completeness,
latency, schema validity, duplicate rate, missing values, API reliability,
and historical reliability — into a 0-100 quality score, a confidence
adjustment applied to every record, and a collector health status. Poor data
quality automatically reduces the influence of a collector's signals.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from app.collectors.base import BaseCollector
from app.collectors.schema import CollectorOutput
from app.core.logging import get_logger

logger = get_logger(__name__)

# Component weights — one per spec dimension (sum = 1.0).
WEIGHTS = {
    "freshness": 0.20,
    "completeness": 0.20,
    "latency": 0.10,
    "schema_validity": 0.10,
    "duplicates": 0.10,
    "missing_values": 0.10,
    "api_reliability": 0.10,
    "historical_reliability": 0.10,
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
        historical_reliability: float | None = None,
    ) -> QualityAssessment:
        """Assess one batch. ``historical_reliability`` (0-100) comes from
        persisted collector_health history; None falls back to the current
        process's API reliability."""
        api_reliability = self._score_api_reliability(collector)
        components = {
            "freshness": self._score_freshness(records),
            "completeness": self._score_completeness(records),
            "latency": self._score_latency(latency_ms),
            "schema_validity": self._score_schema_validity(collector),
            "duplicates": self._score_duplicates(collector.name, records),
            "missing_values": self._score_missing_values(records),
            "api_reliability": api_reliability,
            "historical_reliability": (
                historical_reliability
                if historical_reliability is not None
                else api_reliability
            ),
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

    def _score_schema_validity(self, collector: BaseCollector) -> float:
        """Share of collected records that survived the validate() stage."""
        collected = collector.health.extras.get("last_run_collected")
        dropped = collector.health.extras.get("last_run_validation_dropped", 0)
        if not collected:
            return 100.0  # nothing collected -> completeness handles the zero
        return 100.0 * (collected - dropped) / collected

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

    def _score_missing_values(self, records: list[CollectorOutput]) -> float:
        """Null fraction across record metadata fields (distinct from
        completeness, which only checks the primary normalized value)."""
        if not records:
            return 0.0
        total = 0
        missing = 0
        for record in records:
            for value in record.metadata.values():
                total += 1
                if value is None:
                    missing += 1
        if total == 0:
            return 100.0
        return 100.0 * (1 - missing / total)

    def _score_api_reliability(self, collector: BaseCollector) -> float:
        """Success rate of this collector's runs in the current process."""
        return 100.0 * (1 - collector.health.failure_rate)
