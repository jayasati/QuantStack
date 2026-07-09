"""API tests for the Chapter 12/13 gap-fill: system metrics, circuit
breakers, and alert history."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import collectors as collectors_api
from app.api import health as health_api
from app.collectors.base import BaseCollector, CollectorPipeline
from app.collectors.pipeline import DefaultCollectorPipeline
from app.collectors.registry import CollectorRegistry
from app.collectors.schema import CollectorCategory, CollectorOutput
from app.core.alerts import AlertService, AlertSeverity
from app.core.circuit_breaker import CircuitBreakerRegistry
from app.core.container import container
from app.core.system_metrics import SystemMetricsSampler
from app.events.bus import EventBus


class FlakyCollector(BaseCollector):
    name = "flaky"
    category = CollectorCategory.NEWS
    source = "test"

    async def collect(self) -> list[CollectorOutput]:
        raise RuntimeError("down")


def make_client() -> TestClient:
    container.register(SystemMetricsSampler, SystemMetricsSampler)
    container.register(CircuitBreakerRegistry, lambda: CircuitBreakerRegistry())
    container.register(AlertService, AlertService)
    bus = EventBus()
    pipeline: CollectorPipeline = DefaultCollectorPipeline(bus=bus, session_factory=None)
    container.register(
        CollectorRegistry,
        lambda: CollectorRegistry(pipeline, alerts=container.resolve(AlertService)),
    )
    app = FastAPI()
    app.include_router(health_api.router)
    app.include_router(collectors_api.router)
    return TestClient(app)


def test_health_system_reports_cpu_and_memory() -> None:
    client = make_client()
    response = client.get("/health/system")
    assert response.status_code == 200
    body = response.json()
    assert body["process"]["memory_rss_mb"] > 0
    assert "cpu_percent" in body["system"]


async def test_circuit_breakers_endpoint_reflects_open_collector() -> None:
    client = make_client()
    registry = container.resolve(CollectorRegistry)
    registry.register(FlakyCollector())
    collector = registry.get("flaky")

    for _ in range(3):
        await registry.run_collector("flaky", force=True)

    response = client.get("/collectors/circuit-breakers")
    assert response.status_code == 200
    body = response.json()
    names = {row["name"]: row for row in body["collectors"]}
    assert names["collector.flaky"]["state"] == "open"
    assert collector.circuit_breaker.state.value == "open"


async def test_alerts_endpoint_lists_fired_alerts() -> None:
    client = make_client()
    alerts = container.resolve(AlertService)
    await alerts.fire("collector.flaky", AlertSeverity.CRITICAL, "circuit breaker opened")

    response = client.get("/collectors/alerts")
    assert response.status_code == 200
    body = response.json()
    assert body[0]["message"] == "circuit breaker opened"
    assert body[0]["severity"] == "critical"
