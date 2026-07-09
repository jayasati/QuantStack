"""End-to-end framework tests: lifecycle, pipeline, quality gate, registry."""

from app.collectors.base import BaseCollector, CollectionError, CollectorPipeline
from app.collectors.pipeline import DefaultCollectorPipeline
from app.collectors.registry import CollectorRegistry
from app.collectors.schema import CollectorCategory, CollectorOutput
from app.events.bus import Event, EventBus


class GoodCollector(BaseCollector):
    name = "good"
    category = CollectorCategory.MACRO
    source = "test"
    interval_seconds = 60

    async def collect(self) -> list[CollectorOutput]:
        return [
            CollectorOutput(
                collector_name=self.name,
                collector_category=self.category,
                source=self.source,
                instrument="MARKET",
                normalized_value=1.23,
                confidence=0.8,
            )
        ]


class FailingCollector(BaseCollector):
    name = "failing"
    category = CollectorCategory.NEWS
    source = "test"

    async def collect(self) -> list[CollectorOutput]:
        raise CollectionError("upstream down")


def make_pipeline() -> tuple[DefaultCollectorPipeline, EventBus]:
    bus = EventBus(base_backoff_seconds=0.001)
    return DefaultCollectorPipeline(bus=bus, session_factory=None), bus


async def test_successful_run_publishes_quality_scored_records() -> None:
    pipeline, bus = make_pipeline()
    seen: list[Event] = []

    async def listener(event: Event) -> None:
        seen.append(event)

    bus.subscribe("collector.macro.updated", listener)

    collector = GoodCollector()
    records = await collector.run_once(pipeline)

    assert len(records) == 1
    assert records[0].quality_score is not None and records[0].quality_score > 0
    assert records[0].confidence <= 0.8  # quality multiplier applied
    assert collector.health.status == "ok"
    assert collector.health.records_emitted == 1
    assert len(seen) == 1
    assert seen[0].payload["normalized_value"] == 1.23


async def test_failing_collector_never_raises_and_reports_failure() -> None:
    pipeline, bus = make_pipeline()
    failures: list[Event] = []

    async def listener(event: Event) -> None:
        failures.append(event)

    bus.subscribe("collector.failed", listener)

    collector = FailingCollector()
    records = await collector.run_once(pipeline)

    assert records == []
    assert collector.health.status == "degraded"
    assert collector.health.failure_count == 1
    assert "upstream down" in (collector.health.last_error or "")
    assert len(failures) == 1

    # Three consecutive failures escalate to failed
    await collector.run_once(pipeline)
    await collector.run_once(pipeline)
    assert collector.health.status == "failed"


async def test_circuit_breaker_opens_after_failed_status_and_skips_next_run() -> None:
    """Chapter 13 gap-fill: past the point BaseCollector marks itself
    "failed", the breaker should also be open and fail the next scheduled
    run fast instead of hitting collect() again."""
    pipeline, _ = make_pipeline()
    collector = FailingCollector()

    for _ in range(3):  # matches the existing "3 consecutive failures" cutoff
        await collector.run_once(pipeline)
    assert collector.health.status == "failed"
    assert collector.circuit_breaker.state.value == "open"

    run_count_before = collector.health.run_count
    records = await collector.run_once(pipeline)

    assert records == []
    assert collector.health.status == "circuit_open"
    assert collector.health.run_count == run_count_before  # skipped, not attempted


async def test_registry_disable_enable_and_status() -> None:
    pipeline, _ = make_pipeline()
    registry = CollectorRegistry(pipeline)
    registry.register(GoodCollector())

    assert registry.list_collectors()[0]["name"] == "good"

    registry.disable("good")
    await registry.run_collector("good")  # no-op while disabled
    disabled_health = registry.health_of("good")
    assert disabled_health is not None and disabled_health["run_count"] == 0

    registry.enable("good")
    await registry.run_collector("good")
    health = registry.health_of("good")
    assert health is not None
    assert health["run_count"] == 1
    assert health["status"] == "ok"


async def test_registry_discovers_market_collectors() -> None:
    class NullPipeline(CollectorPipeline):
        async def process(self, collector, records, latency_ms):
            return records

        async def record_failure(self, collector, error):
            pass

    registry = CollectorRegistry(NullPipeline())
    found = registry.discover()
    names = {c["name"] for c in registry.list_collectors()}
    assert found >= 2
    assert {"live_market", "historical_candles"} <= names


class DependentCollector(BaseCollector):
    name = "dependent"
    category = CollectorCategory.OPTIONS
    source = "test"
    priority = 1  # higher urgency than its dependency
    depends_on = ("good",)

    async def collect(self) -> list[CollectorOutput]:
        return []


async def test_resolution_order_puts_dependencies_first() -> None:
    pipeline, _ = make_pipeline()
    registry = CollectorRegistry(pipeline)
    registry.register(DependentCollector())  # registered first, priority 1
    registry.register(GoodCollector())  # priority 100
    order = [c.name for c in registry.resolution_order()]
    assert order.index("good") < order.index("dependent")
    assert registry.validate_dependencies() == []


async def test_unknown_dependency_reported() -> None:
    class Orphan(BaseCollector):
        name = "orphan"
        category = CollectorCategory.NEWS
        source = "test"
        depends_on = ("does_not_exist",)

        async def collect(self) -> list[CollectorOutput]:
            return []

    pipeline, _ = make_pipeline()
    registry = CollectorRegistry(pipeline)
    registry.register(Orphan())
    problems = registry.validate_dependencies()
    assert problems == ["orphan depends on unknown 'does_not_exist'"]


async def test_dependency_cycle_detected() -> None:
    class A(BaseCollector):
        name = "cycle_a"
        category = CollectorCategory.NEWS
        source = "test"
        depends_on = ("cycle_b",)

        async def collect(self) -> list[CollectorOutput]:
            return []

    class B(BaseCollector):
        name = "cycle_b"
        category = CollectorCategory.NEWS
        source = "test"
        depends_on = ("cycle_a",)

        async def collect(self) -> list[CollectorOutput]:
            return []

    pipeline, _ = make_pipeline()
    registry = CollectorRegistry(pipeline)
    registry.register(A())
    registry.register(B())
    import pytest

    with pytest.raises(ValueError, match="dependency cycle"):
        registry.resolution_order()


async def test_disable_reports_active_dependents() -> None:
    pipeline, _ = make_pipeline()
    registry = CollectorRegistry(pipeline)
    registry.register(GoodCollector())
    registry.register(DependentCollector())
    dependents = registry.disable("good")
    assert dependents == ["dependent"]


async def test_interval_override_from_config(monkeypatch) -> None:
    monkeypatch.setenv("COLLECTOR_INTERVALS", '{"good": 300}')
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        pipeline, _ = make_pipeline()
        registry = CollectorRegistry(pipeline)
        collector = GoodCollector()
        registry.register(collector)
        assert registry.effective_interval(collector) == 300
        listing = registry.list_collectors()[0]
        assert listing["interval_seconds"] == 300
        assert listing["default_interval_seconds"] == 60
    finally:
        get_settings.cache_clear()
