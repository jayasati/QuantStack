"""Tests for the Event Calendar Collector (Prompt 2.9)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.collectors.base import CollectionError
from app.collectors.domains.events import (
    EVENT_PROFILES,
    EventCalendarCollector,
    EventCalendarSource,
)
from app.collectors.schema import CollectorOutput, Direction

NOW = datetime(2026, 7, 5, 9, 0, tzinfo=UTC)


class FakeEventSource(EventCalendarSource):
    """Critical event 2 hours away (inside pre-window) + low-impact one 5 days out."""

    def __init__(self, extra: list[dict[str, Any]] | None = None) -> None:
        self.extra = extra or []

    async def fetch_events(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "RBI Monetary Policy Decision",
                "kind": "RBI",
                "scheduled_at": (NOW + timedelta(hours=2)).isoformat(),
                "country": "IN",
            },
            {
                "name": "INFY Dividend Record Date",
                "kind": "DIVIDEND",
                "scheduled_at": (NOW + timedelta(days=5)).isoformat(),
                "country": "IN",
                "instrument": "INFY",
            },
            *self.extra,
        ]


def make_collector(source: EventCalendarSource | None = None) -> EventCalendarCollector:
    return EventCalendarCollector(event_source=source, now=lambda: NOW)


def summary_of(records: list[CollectorOutput]) -> CollectorOutput:
    summaries = [r for r in records if r.metadata.get("record_type") == "summary"]
    assert len(summaries) == 1
    return summaries[0]


def event_by_kind(records: list[CollectorOutput], kind: str) -> CollectorOutput:
    matches = [r for r in records if r.metadata.get("kind") == kind]
    assert len(matches) == 1
    return matches[0]


async def test_critical_event_inside_pre_window() -> None:
    records = await make_collector(FakeEventSource()).collect()

    rbi = event_by_kind(records, "RBI")
    assert rbi.instrument == "MARKET"
    assert rbi.direction is Direction.UNKNOWN
    assert rbi.normalized_value == pytest.approx(2.0)
    assert rbi.metadata["in_pre_event_window"] is True
    assert rbi.metadata["expected_impact"] == "critical"
    assert rbi.metadata["trading_freeze"] is True


async def test_distant_low_impact_event_outside_pre_window() -> None:
    records = await make_collector(FakeEventSource()).collect()

    dividend = event_by_kind(records, "DIVIDEND")
    assert dividend.instrument == "INFY"
    assert dividend.normalized_value == pytest.approx(120.0)
    assert dividend.metadata["in_pre_event_window"] is False
    assert dividend.metadata["expected_impact"] == "low"
    assert dividend.metadata["trading_freeze"] is False


async def test_summary_surfaces_freeze_and_confidence_reduction() -> None:
    records = await make_collector(FakeEventSource()).collect()

    summary = summary_of(records)
    assert summary.instrument == "MARKET"
    assert summary.metadata["trading_freeze_recommended"] is True
    assert summary.metadata["max_active_impact"] == "critical"
    # Only the RBI window is active; the dividend is 5 days out.
    expected = EVENT_PROFILES["RBI"]["confidence_reduction"]
    assert summary.metadata["total_confidence_reduction"] == pytest.approx(expected)
    assert summary.metadata["active_event_kinds"] == ["RBI"]


async def test_confidence_reduction_aggregates_across_active_windows() -> None:
    extra = [
        {
            "name": "US CPI Print",
            "kind": "US_CPI",
            "scheduled_at": (NOW + timedelta(hours=3)).isoformat(),
            "country": "US",
        }
    ]
    records = await make_collector(FakeEventSource(extra=extra)).collect()

    summary = summary_of(records)
    expected = (
        EVENT_PROFILES["RBI"]["confidence_reduction"]
        + EVENT_PROFILES["US_CPI"]["confidence_reduction"]
    )
    assert summary.metadata["total_confidence_reduction"] == pytest.approx(expected)
    assert summary.metadata["active_event_count"] == 2
    assert summary.metadata["trading_freeze_recommended"] is True


async def test_unconfigured_source_raises() -> None:
    from app.collectors.domains.events import UnconfiguredEventSource

    collector = EventCalendarCollector(
        event_source=UnconfiguredEventSource(), now=lambda: NOW
    )
    with pytest.raises(CollectionError, match="event calendar source not configured"):
        await collector.collect()


def test_default_source_is_nse() -> None:
    from app.collectors.sources.nse_events import NseEventCalendarSource

    assert isinstance(EventCalendarCollector()._event_source, NseEventCalendarSource)
