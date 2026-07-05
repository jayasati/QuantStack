from app.collectors.base import BaseCollector
from app.collectors.quality import DataQualityEngine
from app.collectors.schema import CollectorCategory, CollectorOutput


class DummyCollector(BaseCollector):
    name = "dummy"
    category = CollectorCategory.MACRO
    source = "test"

    async def collect(self) -> list[CollectorOutput]:
        return []


def make_record(value: float | None = 1.0) -> CollectorOutput:
    return CollectorOutput(
        collector_name="dummy",
        collector_category=CollectorCategory.MACRO,
        source="test",
        normalized_value=value,
        confidence=1.0,
    )


def test_fresh_complete_data_scores_high() -> None:
    engine = DataQualityEngine()
    collector = DummyCollector()
    records = [make_record(1.0), make_record(2.0)]
    assessment = engine.assess(collector, records, latency_ms=100.0)
    assert assessment.quality_score > 85
    assert assessment.health_status == "healthy"


def test_empty_batch_scores_low() -> None:
    engine = DataQualityEngine()
    collector = DummyCollector()
    assessment = engine.assess(collector, [], latency_ms=100.0)
    assert assessment.quality_score < 40
    assert assessment.health_status == "unhealthy"


def test_missing_values_reduce_completeness() -> None:
    engine = DataQualityEngine()
    collector = DummyCollector()
    full = engine.assess(collector, [make_record(1.0), make_record(2.0)], 100.0)
    engine2 = DataQualityEngine()
    partial = engine2.assess(collector, [make_record(1.0), make_record(None)], 100.0)
    assert partial.quality_score < full.quality_score
    assert partial.components["completeness"] == 50.0


def test_duplicates_detected_across_runs() -> None:
    engine = DataQualityEngine()
    collector = DummyCollector()
    record = make_record(1.0)
    first = engine.assess(collector, [record], 100.0)
    second = engine.assess(collector, [record], 100.0)  # same fingerprint
    assert first.components["duplicates"] == 100.0
    assert second.components["duplicates"] == 0.0


def test_apply_reduces_confidence_with_quality() -> None:
    engine = DataQualityEngine()
    collector = DummyCollector()
    collector.health.run_count = 10
    collector.health.failure_count = 5  # unreliable collector
    records = [make_record(1.0)]
    assessment = engine.assess(collector, records, latency_ms=15_000.0)
    engine.apply(records, assessment)
    assert records[0].quality_score == assessment.quality_score
    assert records[0].confidence < 1.0
