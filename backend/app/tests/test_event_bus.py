from app.events.bus import Event, EventBus


def fast_bus(**kwargs) -> EventBus:
    return EventBus(base_backoff_seconds=0.001, **kwargs)


async def test_publish_reaches_subscriber() -> None:
    bus = fast_bus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("price.updated", handler)
    await bus.publish(Event(type="price.updated", payload={"symbol": "NIFTY"}, source="test"))

    assert len(received) == 1
    assert received[0].payload["symbol"] == "NIFTY"
    assert received[0].event_id
    assert received[0].trace_id
    assert received[0].version == 1


async def test_handler_error_does_not_break_others() -> None:
    bus = fast_bus(max_retries=1)
    received: list[str] = []

    async def bad_handler(event: Event) -> None:
        raise RuntimeError("boom")

    async def good_handler(event: Event) -> None:
        received.append(event.type)

    bus.subscribe("x", bad_handler)
    bus.subscribe("x", good_handler)
    await bus.publish(Event(type="x"))

    assert received == ["x"]


async def test_retry_then_success() -> None:
    bus = fast_bus(max_retries=2)
    attempts = {"n": 0}

    async def flaky(event: Event) -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("transient")

    bus.subscribe("x", flaky)
    await bus.publish(Event(type="x"))
    assert attempts["n"] == 3
    assert len(bus.dead_letters) == 0


async def test_exhausted_retries_go_to_dead_letter_queue() -> None:
    bus = fast_bus(max_retries=2)

    async def always_fails(event: Event) -> None:
        raise RuntimeError("permanent")

    bus.subscribe("x", always_fails)
    event = Event(type="x")
    await bus.publish(event)

    assert len(bus.dead_letters) == 1
    letter = bus.dead_letters[0]
    assert letter.event.event_id == event.event_id
    assert letter.attempts == 3
    assert "permanent" in letter.error


async def test_duplicate_events_are_ignored() -> None:
    bus = fast_bus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("x", handler)
    event = Event(type="x")
    await bus.publish(event)
    await bus.publish(event)  # same event_id

    assert len(received) == 1
    assert bus.metrics()["duplicates_ignored"] == 1


async def test_publish_without_subscribers_is_noop() -> None:
    bus = fast_bus()
    await bus.publish(Event(type="nobody.listens"))
