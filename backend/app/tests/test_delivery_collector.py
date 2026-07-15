from datetime import date, datetime, timedelta

import pytest

from app.collectors.base import CollectionError
from app.collectors.domains.delivery import DeliveryCollector
from app.collectors.sources.nse_delivery import IST, parse_bhavcopy

# Columns are looked up by name, so the sample keeps only the ones the
# parser needs (the real file carries 15).
SAMPLE_CSV = """SYMBOL, SERIES, DATE1, TTL_TRD_QNTY, DELIV_QTY, DELIV_PER
RELIANCE, EQ, 06-Jul-2026, 9500000, 5200000, 54.74
INFY, EQ, 06-Jul-2026, 6100000, 3965000, 65.00
RELIANCE, BE, 06-Jul-2026, 100, 100, 100.00
NEWIPO, EQ, 06-Jul-2026, 500000,  -,  -
HDFCBANK, EQ, 06-Jul-2026, 12000000, 7440000, 62.00
"""


def test_parse_bhavcopy_filters_series_and_symbols() -> None:
    records = parse_bhavcopy(SAMPLE_CSV, symbols={"RELIANCE", "INFY"})
    assert set(records) == {"RELIANCE", "INFY"}
    reliance = records["RELIANCE"]
    assert reliance["delivery_pct"] == 54.74
    assert reliance["traded_qty"] == 9_500_000
    assert reliance["delivered_qty"] == 5_200_000
    assert reliance["position_date"] == datetime(2026, 7, 6, tzinfo=IST)


def test_parse_bhavcopy_skips_dash_delivery_and_bad_input() -> None:
    records = parse_bhavcopy(SAMPLE_CSV)
    assert "NEWIPO" not in records  # '-' delivery (new listing)
    assert records["RELIANCE"]["delivery_pct"] == 54.74  # EQ row, not the BE row
    assert parse_bhavcopy("") == {}
    assert parse_bhavcopy("A,B,C\n1,2,3") == {}  # missing required columns


class FakeBhavcopySource:
    """Serves canned per-date delivery maps; None elsewhere (holiday/404)."""

    def __init__(self, days: dict[date, dict] | None = None) -> None:
        self.days = days or {}
        self.calls: list[date] = []

    async def fetch_day(self, session_date: date, symbols=None) -> dict | None:
        self.calls.append(session_date)
        return self.days.get(session_date)


def make_day_record(symbol: str, session: date, pct: float) -> dict:
    return {
        "symbol": symbol,
        "delivery_pct": pct,
        "traded_qty": 1_000_000.0,
        "delivered_qty": pct * 10_000.0,
        "position_date": datetime(session.year, session.month, session.day, tzinfo=IST),
    }


def make_collector(source: FakeBhavcopySource, has_history: bool) -> DeliveryCollector:
    collector = DeliveryCollector(delivery_source=source, symbols=["RELIANCE", "INFY"])
    collector._sessions = lambda: None  # type: ignore[method-assign]  # keep tests off the real DB

    async def fake_history() -> bool:
        return has_history

    collector._has_history = fake_history  # type: ignore[method-assign]
    return collector


async def test_incremental_mode_stops_at_latest_session() -> None:
    today = datetime.now(IST).date()
    latest = today - timedelta(days=1)
    older = today - timedelta(days=2)
    source = FakeBhavcopySource({
        latest: {"RELIANCE": make_day_record("RELIANCE", latest, 54.7)},
        older: {"RELIANCE": make_day_record("RELIANCE", older, 51.0)},
    })
    collector = make_collector(source, has_history=True)
    records = await collector.collect()
    assert [r.instrument for r in records] == ["RELIANCE"]
    assert records[0].normalized_value == 54.7
    assert older not in source.calls  # stopped after the first published session


async def test_backfill_mode_scans_full_window() -> None:
    today = datetime.now(IST).date()
    d1, d2 = today - timedelta(days=1), today - timedelta(days=4)
    source = FakeBhavcopySource({
        d1: {
            "RELIANCE": make_day_record("RELIANCE", d1, 54.7),
            "INFY": make_day_record("INFY", d1, 65.0),
        },
        d2: {"RELIANCE": make_day_record("RELIANCE", d2, 50.1)},
    })
    collector = make_collector(source, has_history=False)
    records = await collector.collect()
    assert len(source.calls) == DeliveryCollector.BACKFILL_DAYS
    assert len(records) == 3  # both sessions, every symbol present
    assert collector.health.extras["backfill_mode"] is True
    assert collector.health.extras["sessions_found"] == 2
    dates = {r.metadata["position_date"][:10] for r in records}
    assert dates == {d1.isoformat(), d2.isoformat()}


async def test_collect_raises_when_nothing_published() -> None:
    collector = make_collector(FakeBhavcopySource(), has_history=True)
    with pytest.raises(CollectionError):
        await collector.collect()


def test_collector_skips_index_symbols_from_watchlist() -> None:
    collector = DeliveryCollector(delivery_source=FakeBhavcopySource())
    assert "NIFTY" not in collector.symbols
    assert "BANKNIFTY" not in collector.symbols


def test_collector_is_after_hours_only() -> None:
    # Bhavcopy publishes end-of-day -- scheduled runs during market hours
    # would just find nothing (see test_collector_framework.py for the
    # shared after_hours_only gate mechanics).
    assert DeliveryCollector.after_hours_only is True
