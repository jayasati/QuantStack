"""Event calendar collector (Volume 2, Chapter 14, Prompt 2.9).

Tracks scheduled market-moving events (central banks, macro prints, index
rebalances, corporate actions) and emits a per-event risk profile plus one
aggregate risk-state summary. Events are never fabricated: without a
configured source the collector fails loudly with ``CollectionError``.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.collectors.base import BaseCollector, CollectionError
from app.collectors.schema import CollectorCategory, CollectorOutput, Direction

#: Risk profile per event kind. Values are institutional defaults and can be
#: tuned without touching collector logic.
EVENT_PROFILES: dict[str, dict[str, Any]] = {
    "RBI": {
        "expected_impact": "critical",
        "expected_volatility_multiplier": 3.0,
        "pre_event_window_hours": 24,
        "post_event_window_hours": 6,
        "confidence_reduction": 0.5,
        "trading_freeze": True,
    },
    "FED": {
        "expected_impact": "critical",
        "expected_volatility_multiplier": 3.0,
        "pre_event_window_hours": 24,
        "post_event_window_hours": 6,
        "confidence_reduction": 0.5,
        "trading_freeze": True,
    },
    "ECB": {
        "expected_impact": "high",
        "expected_volatility_multiplier": 2.0,
        "pre_event_window_hours": 12,
        "post_event_window_hours": 4,
        "confidence_reduction": 0.3,
        "trading_freeze": False,
    },
    "BOJ": {
        "expected_impact": "high",
        "expected_volatility_multiplier": 2.0,
        "pre_event_window_hours": 12,
        "post_event_window_hours": 4,
        "confidence_reduction": 0.3,
        "trading_freeze": False,
    },
    "US_CPI": {
        "expected_impact": "high",
        "expected_volatility_multiplier": 2.5,
        "pre_event_window_hours": 6,
        "post_event_window_hours": 2,
        "confidence_reduction": 0.35,
        "trading_freeze": False,
    },
    "INDIA_CPI": {
        "expected_impact": "high",
        "expected_volatility_multiplier": 2.0,
        "pre_event_window_hours": 6,
        "post_event_window_hours": 2,
        "confidence_reduction": 0.3,
        "trading_freeze": False,
    },
    "GDP": {
        "expected_impact": "medium",
        "expected_volatility_multiplier": 1.5,
        "pre_event_window_hours": 4,
        "post_event_window_hours": 2,
        "confidence_reduction": 0.2,
        "trading_freeze": False,
    },
    "PMI": {
        "expected_impact": "medium",
        "expected_volatility_multiplier": 1.3,
        "pre_event_window_hours": 2,
        "post_event_window_hours": 1,
        "confidence_reduction": 0.15,
        "trading_freeze": False,
    },
    "BUDGET": {
        "expected_impact": "critical",
        "expected_volatility_multiplier": 3.5,
        "pre_event_window_hours": 48,
        "post_event_window_hours": 24,
        "confidence_reduction": 0.6,
        "trading_freeze": True,
    },
    "ELECTION": {
        "expected_impact": "critical",
        "expected_volatility_multiplier": 3.0,
        "pre_event_window_hours": 72,
        "post_event_window_hours": 48,
        "confidence_reduction": 0.6,
        "trading_freeze": True,
    },
    "MSCI_REBALANCE": {
        "expected_impact": "high",
        "expected_volatility_multiplier": 1.8,
        "pre_event_window_hours": 24,
        "post_event_window_hours": 4,
        "confidence_reduction": 0.25,
        "trading_freeze": False,
    },
    "FTSE_REBALANCE": {
        "expected_impact": "high",
        "expected_volatility_multiplier": 1.8,
        "pre_event_window_hours": 24,
        "post_event_window_hours": 4,
        "confidence_reduction": 0.25,
        "trading_freeze": False,
    },
    "FNO_EXPIRY": {
        "expected_impact": "high",
        "expected_volatility_multiplier": 1.6,
        "pre_event_window_hours": 6,
        "post_event_window_hours": 2,
        "confidence_reduction": 0.2,
        "trading_freeze": False,
    },
    "IPO": {
        "expected_impact": "low",
        "expected_volatility_multiplier": 1.1,
        "pre_event_window_hours": 2,
        "post_event_window_hours": 2,
        "confidence_reduction": 0.05,
        "trading_freeze": False,
    },
    "RESULTS": {
        "expected_impact": "high",
        "expected_volatility_multiplier": 2.0,
        "pre_event_window_hours": 12,
        "post_event_window_hours": 4,
        "confidence_reduction": 0.3,
        "trading_freeze": False,
    },
    "DIVIDEND": {
        "expected_impact": "low",
        "expected_volatility_multiplier": 1.05,
        "pre_event_window_hours": 1,
        "post_event_window_hours": 1,
        "confidence_reduction": 0.05,
        "trading_freeze": False,
    },
    "BONUS": {
        "expected_impact": "low",
        "expected_volatility_multiplier": 1.1,
        "pre_event_window_hours": 2,
        "post_event_window_hours": 1,
        "confidence_reduction": 0.05,
        "trading_freeze": False,
    },
    "SPLIT": {
        "expected_impact": "low",
        "expected_volatility_multiplier": 1.1,
        "pre_event_window_hours": 2,
        "post_event_window_hours": 1,
        "confidence_reduction": 0.05,
        "trading_freeze": False,
    },
}

#: Conservative fallback for event kinds without a curated profile.
_DEFAULT_PROFILE: dict[str, Any] = {
    "expected_impact": "medium",
    "expected_volatility_multiplier": 1.5,
    "pre_event_window_hours": 4,
    "post_event_window_hours": 2,
    "confidence_reduction": 0.2,
    "trading_freeze": False,
}

_IMPACT_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}

_UPCOMING_HORIZON = timedelta(days=7)


class EventCalendarSource(ABC):
    """Injectable async provider of raw calendar events.

    Each event dict must contain ``name``, ``kind``, ``scheduled_at`` (ISO
    datetime) and ``country``; ``instrument`` is optional.
    """

    @abstractmethod
    async def fetch_events(self) -> list[dict[str, Any]]: ...


class UnconfiguredEventSource(EventCalendarSource):
    """Default source: fails loudly instead of fabricating events."""

    async def fetch_events(self) -> list[dict[str, Any]]:
        raise CollectionError("event calendar source not configured")


class EventCalendarCollector(BaseCollector):
    """Emit per-event risk profiles plus an aggregate market risk summary."""

    name = "event_calendar"
    category = CollectorCategory.ECONOMIC_CALENDAR
    source = "event_calendar"
    interval_seconds = 1800
    priority = 30

    def __init__(
        self,
        event_source: EventCalendarSource | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__()
        if event_source is None:
            from app.collectors.sources.nse_events import NseEventCalendarSource

            event_source = NseEventCalendarSource()
        self._event_source = event_source
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(UTC))

    async def cleanup(self) -> None:
        closer = getattr(self._event_source, "close", None)
        if closer is not None:
            await closer()

    async def collect(self) -> list[CollectorOutput]:
        events = await self._event_source.fetch_events()
        now = self._ensure_aware(self._now())

        records: list[CollectorOutput] = []
        active_impacts: list[str] = []
        active_reductions: list[float] = []
        freeze_recommended = False
        active_kinds: list[str] = []

        for event in events:
            kind, scheduled_at = self._parse_event(event)
            profile = EVENT_PROFILES.get(kind, _DEFAULT_PROFILE)
            pre_window = timedelta(hours=profile["pre_event_window_hours"])
            post_window = timedelta(hours=profile["post_event_window_hours"])
            in_pre_window = scheduled_at - pre_window <= now <= scheduled_at
            window_active = scheduled_at - pre_window <= now <= scheduled_at + post_window

            if window_active:
                active_impacts.append(profile["expected_impact"])
                active_reductions.append(float(profile["confidence_reduction"]))
                freeze_recommended = freeze_recommended or bool(profile["trading_freeze"])
                active_kinds.append(kind)

            if not now <= scheduled_at <= now + _UPCOMING_HORIZON:
                continue

            hours_until = (scheduled_at - now).total_seconds() / 3600.0
            records.append(
                CollectorOutput(
                    collector_name=self.name,
                    collector_category=self.category,
                    source=self.source,
                    instrument=str(event.get("instrument") or "MARKET"),
                    raw_value=event,
                    normalized_value=hours_until,
                    direction=Direction.UNKNOWN,
                    confidence=max(0.0, 1.0 - float(profile["confidence_reduction"]))
                    if in_pre_window
                    else 0.9,
                    metadata={
                        "record_type": "event",
                        "event_name": event["name"],
                        "kind": kind,
                        "country": event.get("country"),
                        "scheduled_at": scheduled_at.isoformat(),
                        "hours_until_event": hours_until,
                        "in_pre_event_window": in_pre_window,
                        **profile,
                    },
                )
            )

        records.append(self._summary_record(now, active_impacts, active_reductions,
                                             freeze_recommended, active_kinds))
        return records

    def _summary_record(
        self,
        now: datetime,
        active_impacts: list[str],
        active_reductions: list[float],
        freeze_recommended: bool,
        active_kinds: list[str],
    ) -> CollectorOutput:
        max_impact = (
            max(active_impacts, key=lambda impact: _IMPACT_RANK.get(impact, 0))
            if active_impacts
            else None
        )
        total_reduction = min(1.0, sum(active_reductions))
        return CollectorOutput(
            collector_name=self.name,
            collector_category=self.category,
            source=self.source,
            instrument="MARKET",
            raw_value=len(active_kinds),
            normalized_value=total_reduction,
            direction=Direction.UNKNOWN,
            confidence=max(0.0, 1.0 - total_reduction),
            metadata={
                "record_type": "summary",
                "as_of": now.isoformat(),
                "active_event_count": len(active_kinds),
                "active_event_kinds": active_kinds,
                "max_active_impact": max_impact,
                "trading_freeze_recommended": freeze_recommended,
                "total_confidence_reduction": total_reduction,
            },
        )

    @staticmethod
    def _parse_event(event: dict[str, Any]) -> tuple[str, datetime]:
        """Extract kind and an aware scheduled_at, failing loudly on bad data."""
        try:
            kind = str(event["kind"])
            _ = event["name"]
            raw = event["scheduled_at"]
            scheduled_at = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
        except (KeyError, TypeError, ValueError) as exc:
            raise CollectionError(f"malformed calendar event: {event!r}") from exc
        return kind, EventCalendarCollector._ensure_aware(scheduled_at)

    @staticmethod
    def _ensure_aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
