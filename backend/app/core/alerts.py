"""Alert stage (Volume 1, Chapter 13: ... -> Circuit Breaker -> Fallback ->
Alert).

The last stage of the error-handling escalation chain: something a human
should know about happened (a circuit breaker opened, a dependency stayed
down through fallback). Pluggable sinks keep this decoupled from any
specific delivery channel (Telegram ops alerts can plug in later as another
``AlertSink`` without changing callers) -- consistent with the "never
instantiate concrete classes inside services" rule in Chapter 8.
"""

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from app.core.logging import get_logger

logger = get_logger(__name__)


class AlertSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Alert:
    source: str
    severity: AlertSeverity
    message: str
    context: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "severity": self.severity.value,
            "message": self.message,
            "context": self.context,
            "timestamp": self.timestamp.isoformat(),
        }


class AlertSink(ABC):
    @abstractmethod
    async def send(self, alert: Alert) -> None: ...


class LoggingAlertSink(AlertSink):
    """Always-on baseline sink: structured log at a level matching severity."""

    async def send(self, alert: Alert) -> None:
        log_fn = {
            AlertSeverity.INFO: logger.info,
            AlertSeverity.WARNING: logger.warning,
            AlertSeverity.CRITICAL: logger.error,
        }[alert.severity]
        log_fn(alert.message, extra={"alert_source": alert.source, **alert.context})


class EventBusAlertSink(AlertSink):
    """Publishes onto the event bus as ``system.alert`` so any future
    subscriber (e.g. a Telegram ops-alert handler) can react without callers
    knowing it exists -- no module directly invokes downstream modules."""

    def __init__(self, bus) -> None:  # app.events.bus.EventBus, untyped to avoid an import cycle
        self._bus = bus

    async def send(self, alert: Alert) -> None:
        from app.events.bus import Event

        await self._bus.publish(
            Event(type="system.alert", payload=alert.to_dict(), source=alert.source)
        )


class AlertService:
    """Fans an alert out to every configured sink. A broken sink never
    prevents the caller's own error handling from completing."""

    def __init__(self, sinks: list[AlertSink] | None = None, history_capacity: int = 200) -> None:
        self._sinks = sinks if sinks is not None else [LoggingAlertSink()]
        self._history: deque[Alert] = deque(maxlen=history_capacity)

    async def fire(
        self, source: str, severity: AlertSeverity, message: str, **context
    ) -> Alert:
        alert = Alert(source=source, severity=severity, message=message, context=context)
        self._history.append(alert)
        for sink in self._sinks:
            try:
                await sink.send(alert)
            except Exception as exc:  # an alert sink must never raise into the caller
                logger.error(
                    "alert sink failed",
                    extra={"sink": type(sink).__name__, "error": str(exc)},
                )
        return alert

    def recent(self, limit: int = 50) -> list[dict]:
        return [a.to_dict() for a in list(self._history)[-limit:][::-1]]
