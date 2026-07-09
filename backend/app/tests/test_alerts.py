"""Alert stage unit tests (Volume 1, Chapter 13 gap-fill)."""

from app.core.alerts import Alert, AlertService, AlertSeverity, AlertSink
from app.events.bus import Event, EventBus


class RecordingSink(AlertSink):
    def __init__(self) -> None:
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> None:
        self.sent.append(alert)


class BrokenSink(AlertSink):
    async def send(self, alert: Alert) -> None:
        raise RuntimeError("sink is down")


async def test_fire_delivers_to_every_sink_and_records_history() -> None:
    sink = RecordingSink()
    service = AlertService(sinks=[sink])

    alert = await service.fire(
        "broker.angel_one", AlertSeverity.CRITICAL, "circuit opened", path="/quote"
    )

    assert sink.sent == [alert]
    assert alert.context["path"] == "/quote"
    recent = service.recent()
    assert len(recent) == 1
    assert recent[0]["message"] == "circuit opened"
    assert recent[0]["severity"] == "critical"


async def test_broken_sink_never_raises_into_caller() -> None:
    good = RecordingSink()
    service = AlertService(sinks=[BrokenSink(), good])

    await service.fire("collector.news", AlertSeverity.WARNING, "degraded")

    assert len(good.sent) == 1  # the healthy sink still received it


async def test_recent_returns_newest_first_and_respects_limit() -> None:
    service = AlertService(sinks=[])
    for i in range(5):
        await service.fire("x", AlertSeverity.INFO, f"alert-{i}")

    recent = service.recent(limit=2)
    assert [a["message"] for a in recent] == ["alert-4", "alert-3"]


async def test_event_bus_sink_publishes_system_alert() -> None:
    bus = EventBus()
    seen: list[Event] = []

    async def listener(event: Event) -> None:
        seen.append(event)

    bus.subscribe("system.alert", listener)

    from app.core.alerts import EventBusAlertSink

    service = AlertService(sinks=[EventBusAlertSink(bus)])
    await service.fire("collector.macro", AlertSeverity.CRITICAL, "circuit opened")

    assert len(seen) == 1
    assert seen[0].payload["message"] == "circuit opened"
    assert seen[0].source == "collector.macro"
