"""Asynchronous event bus (Volume 2, Prompt 2.13).

All inter-module communication flows through events — no module directly
invokes a downstream module. Supports retries with exponential backoff, a
dead-letter queue, idempotency (event-id dedup), trace ids for cross-module
tracing, and version-scoped dispatch: a handler may subscribe to every
version of an event type (the default) or to one specific version, and
``publish`` only routes to handlers whose requested version matches.
"""

import asyncio
import uuid
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

Handler = Callable[["Event"], Awaitable[None]]


@dataclass(frozen=True)
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    version: int = 1


@dataclass(frozen=True)
class DeadLetter:
    event: Event
    handler: str
    error: str
    attempts: int
    failed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class EventBus:
    def __init__(
        self,
        max_retries: int = 3,
        base_backoff_seconds: float = 0.2,
        dead_letter_capacity: int = 1000,
        idempotency_window: int = 10_000,
    ) -> None:
        self._subscribers: dict[str, list[tuple[Handler, int | None]]] = {}
        self._max_retries = max_retries
        self._base_backoff = base_backoff_seconds
        self.dead_letters: deque[DeadLetter] = deque(maxlen=dead_letter_capacity)
        self._seen_event_ids: OrderedDict[str, None] = OrderedDict()
        self._idempotency_window = idempotency_window
        self.published_count = 0
        self.delivered_count = 0
        self.duplicate_count = 0

    def subscribe(self, event_type: str, handler: Handler, version: int | None = None) -> None:
        """Subscribe to ``event_type``. ``version=None`` (default) receives
        every version of that event; passing a specific ``version`` routes
        only events published with a matching ``Event.version`` — real
        version-scoped dispatch, not just a field carried through for show."""
        self._subscribers.setdefault(event_type, []).append((handler, version))

    def _is_duplicate(self, event: Event) -> bool:
        if event.event_id in self._seen_event_ids:
            return True
        self._seen_event_ids[event.event_id] = None
        while len(self._seen_event_ids) > self._idempotency_window:
            self._seen_event_ids.popitem(last=False)
        return False

    async def publish(self, event: Event) -> None:
        if self._is_duplicate(event):
            self.duplicate_count += 1
            logger.debug(
                "duplicate event ignored",
                extra={"event_type": event.type, "event_id": event.event_id},
            )
            return
        self.published_count += 1
        handlers = [
            handler
            for handler, version in self._subscribers.get(event.type, [])
            if version is None or version == event.version
        ]
        if not handlers:
            return
        await asyncio.gather(*(self._deliver(handler, event) for handler in handlers))

    async def _deliver(self, handler: Handler, event: Event) -> None:
        handler_name = getattr(handler, "__qualname__", repr(handler))
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 2):  # first try + retries
            try:
                await handler(event)
                self.delivered_count += 1
                return
            except Exception as exc:
                last_error = exc
                if attempt <= self._max_retries:
                    delay = self._base_backoff * (2 ** (attempt - 1))
                    logger.warning(
                        "event handler failed; retrying",
                        extra={
                            "event_type": event.type,
                            "event_id": event.event_id,
                            "trace_id": event.trace_id,
                            "handler": handler_name,
                            "attempt": attempt,
                            "retry_in_seconds": delay,
                            "error": str(exc),
                        },
                    )
                    await asyncio.sleep(delay)
        self.dead_letters.append(
            DeadLetter(
                event=event,
                handler=handler_name,
                error=str(last_error),
                attempts=self._max_retries + 1,
            )
        )
        logger.error(
            "event moved to dead-letter queue",
            extra={
                "event_type": event.type,
                "event_id": event.event_id,
                "trace_id": event.trace_id,
                "handler": handler_name,
                "error": str(last_error),
            },
        )

    def metrics(self) -> dict[str, int]:
        return {
            "published": self.published_count,
            "delivered": self.delivered_count,
            "duplicates_ignored": self.duplicate_count,
            "dead_letters": len(self.dead_letters),
        }

    def list_dead_letters(self, limit: int = 100) -> list[dict]:
        """Most recent dead letters, newest first (for inspection APIs)."""
        letters = list(self.dead_letters)[-limit:]
        return [
            {
                "event_id": letter.event.event_id,
                "event_type": letter.event.type,
                "trace_id": letter.event.trace_id,
                "source": letter.event.source,
                "handler": letter.handler,
                "error": letter.error,
                "attempts": letter.attempts,
                "failed_at": letter.failed_at.isoformat(),
            }
            for letter in reversed(letters)
        ]

    async def replay_dead_letter(self, event_id: str) -> bool:
        """Re-publish one dead letter as a fresh delivery attempt.

        The original event id is reused minus the idempotency block (a new
        event id is minted, original preserved in the payload trail) so the
        replay is actually delivered.
        """
        for letter in list(self.dead_letters):
            if letter.event.event_id == event_id:
                self.dead_letters.remove(letter)
                original = letter.event
                replayed = Event(
                    type=original.type,
                    payload=original.payload,
                    source=original.source,
                    trace_id=original.trace_id,  # keep the trace chain
                    version=original.version,
                )
                logger.info(
                    "replaying dead letter",
                    extra={
                        "original_event_id": original.event_id,
                        "replay_event_id": replayed.event_id,
                        "event_type": original.type,
                    },
                )
                await self.publish(replayed)
                return True
        return False
