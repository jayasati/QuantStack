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


async def test_version_scoped_subscriber_only_receives_matching_version() -> None:
    """Event.version now does real routing, not just carried metadata."""
    bus = fast_bus()
    v1_seen: list[Event] = []
    v2_seen: list[Event] = []
    any_seen: list[Event] = []

    async def v1_handler(event: Event) -> None:
        v1_seen.append(event)

    async def v2_handler(event: Event) -> None:
        v2_seen.append(event)

    async def any_handler(event: Event) -> None:
        any_seen.append(event)

    bus.subscribe("x", v1_handler, version=1)
    bus.subscribe("x", v2_handler, version=2)
    bus.subscribe("x", any_handler)  # version=None -> every version

    await bus.publish(Event(type="x", version=1))
    await bus.publish(Event(type="x", version=2))

    assert len(v1_seen) == 1 and v1_seen[0].version == 1
    assert len(v2_seen) == 1 and v2_seen[0].version == 2
    assert len(any_seen) == 2  # the version-agnostic handler saw both


async def test_dead_letter_inspection_and_replay() -> None:
    bus = fast_bus(max_retries=1)
    calls = {"n": 0}

    async def flaky_then_fine(event: Event) -> None:
        calls["n"] += 1
        if calls["n"] <= 2:  # fail first delivery (1 try + 1 retry)
            raise RuntimeError("downstream was down")

    bus.subscribe("x", flaky_then_fine)
    event = Event(type="x", payload={"k": 1}, source="test")
    await bus.publish(event)

    letters = bus.list_dead_letters()
    assert len(letters) == 1
    assert letters[0]["event_id"] == event.event_id
    assert letters[0]["trace_id"] == event.trace_id
    assert "downstream was down" in letters[0]["error"]

    # Replay: removed from DLQ, delivered fresh, trace preserved
    assert await bus.replay_dead_letter(event.event_id) is True
    assert bus.list_dead_letters() == []
    assert calls["n"] == 3  # replay delivered on first attempt

    # Unknown id -> False
    assert await bus.replay_dead_letter("nope") is False


async def test_replay_bypasses_idempotency_but_keeps_trace() -> None:
    bus = fast_bus(max_retries=0)
    seen: list[Event] = []

    fail_first = {"armed": True}

    async def handler(event: Event) -> None:
        if fail_first["armed"]:
            fail_first["armed"] = False
            raise RuntimeError("boom")
        seen.append(event)

    bus.subscribe("x", handler)
    original = Event(type="x")
    await bus.publish(original)
    assert await bus.replay_dead_letter(original.event_id) is True
    assert len(seen) == 1
    assert seen[0].event_id != original.event_id  # fresh id -> not deduped
    assert seen[0].trace_id == original.trace_id  # trace chain preserved
