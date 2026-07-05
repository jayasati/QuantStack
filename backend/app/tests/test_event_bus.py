from app.events.bus import Event, EventBus


async def test_publish_reaches_subscriber() -> None:
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("price.updated", handler)
    await bus.publish(Event(type="price.updated", payload={"symbol": "NIFTY"}, source="test"))

    assert len(received) == 1
    assert received[0].payload["symbol"] == "NIFTY"


async def test_handler_error_does_not_break_others() -> None:
    bus = EventBus()
    received: list[str] = []

    async def bad_handler(event: Event) -> None:
        raise RuntimeError("boom")

    async def good_handler(event: Event) -> None:
        received.append(event.type)

    bus.subscribe("x", bad_handler)
    bus.subscribe("x", good_handler)
    await bus.publish(Event(type="x"))

    assert received == ["x"]


async def test_publish_without_subscribers_is_noop() -> None:
    bus = EventBus()
    await bus.publish(Event(type="nobody.listens"))
