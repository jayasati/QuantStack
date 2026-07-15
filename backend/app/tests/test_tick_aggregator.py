"""Tests for the real-time tick -> candle aggregator (2026-07-16)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.collectors.tick_aggregator import TickCandleAggregator, floor_to_session_bucket
from app.database.tables import OhlcvCandle


def _ist(hour: int, minute: int, second: int = 0) -> datetime:
    # 2026-07-16 IST wall-clock time, expressed as its UTC-equivalent
    # instant (IST = UTC+5:30, no DST).
    return datetime(2026, 7, 16, hour, minute, second, tzinfo=UTC) - timedelta(hours=5, minutes=30)


def test_floor_to_session_bucket_anchors_to_0915_ist_not_clock_hour() -> None:
    # Verified live 2026-07-15: stored 1H bars land on 09:15/10:15/...,
    # not 09:00/10:00/... -- this must match, not the clock hour.
    assert floor_to_session_bucket(_ist(9, 47), 60) == _ist(9, 15)
    assert floor_to_session_bucket(_ist(10, 23), 60) == _ist(10, 15)
    assert floor_to_session_bucket(_ist(9, 15), 60) == _ist(9, 15)


def test_floor_to_session_bucket_matches_session_open_for_finer_intervals() -> None:
    assert floor_to_session_bucket(_ist(9, 22), 5) == _ist(9, 20)
    assert floor_to_session_bucket(_ist(9, 17), 3) == _ist(9, 15)
    assert floor_to_session_bucket(_ist(9, 15), 1) == _ist(9, 15)


@pytest.mark.db
async def test_ingest_batch_live_updates_the_forming_1m_bar(test_session_factory) -> None:
    """Two ticks in the same minute must update ONE row, not create two --
    and the row must reflect the latest state (live-updating forming bar,
    2026-07-16 decision), not just the first tick."""
    aggregator = TickCandleAggregator(test_session_factory)
    ts = _ist(9, 20, 5)
    await aggregator.ingest_batch([
        {"symbol": "HDFCBANK", "ltp": 100.0, "ts": ts, "data": {"volume": 1000}},
    ])
    await aggregator.ingest_batch([
        {"symbol": "HDFCBANK", "ltp": 102.0, "ts": ts + timedelta(seconds=20), "data": {"volume": 1200}},
    ])
    await aggregator.ingest_batch([
        {"symbol": "HDFCBANK", "ltp": 99.0, "ts": ts + timedelta(seconds=40), "data": {"volume": 1500}},
    ])

    async with test_session_factory() as session:
        result = await session.execute(
            select(OhlcvCandle).where(
                OhlcvCandle.symbol == "HDFCBANK", OhlcvCandle.timeframe == "1m"
            )
        )
        rows = result.scalars().all()

    assert len(rows) == 1
    row = rows[0]
    assert row.open == 100.0
    assert row.high == 102.0
    assert row.low == 99.0
    assert row.close == 99.0  # latest tick, live-updated
    assert row.volume == 500  # 1500 - baseline(1000)


@pytest.mark.db
async def test_ingest_batch_starts_a_new_bar_on_minute_boundary(test_session_factory) -> None:
    aggregator = TickCandleAggregator(test_session_factory)
    await aggregator.ingest_batch([
        {"symbol": "TCS", "ltp": 3000.0, "ts": _ist(9, 20, 30), "data": {"volume": 500}},
    ])
    await aggregator.ingest_batch([
        {"symbol": "TCS", "ltp": 3010.0, "ts": _ist(9, 21, 5), "data": {"volume": 700}},
    ])

    async with test_session_factory() as session:
        result = await session.execute(
            select(OhlcvCandle.ts, OhlcvCandle.open, OhlcvCandle.close)
            .where(OhlcvCandle.symbol == "TCS", OhlcvCandle.timeframe == "1m")
            .order_by(OhlcvCandle.ts.asc())
        )
        rows = result.all()

    assert len(rows) == 2
    assert rows[0].open == 3000.0 and rows[0].close == 3000.0
    assert rows[1].open == 3010.0 and rows[1].close == 3010.0


@pytest.mark.db
async def test_rollups_fold_the_underlying_1m_bars(test_session_factory) -> None:
    """Three 1m bars inside one 3m window must fold into open=first's open,
    high/low across all three, close=last's close, volume=sum."""
    aggregator = TickCandleAggregator(test_session_factory)
    base = _ist(9, 15, 0)
    for i, (price, vol) in enumerate([(100.0, 100), (105.0, 250), (98.0, 400)]):
        await aggregator.ingest_batch([
            {"symbol": "INFY", "ltp": price, "ts": base + timedelta(minutes=i), "data": {"volume": vol}},
        ])

    async with test_session_factory() as session:
        result = await session.execute(
            select(OhlcvCandle)
            .where(OhlcvCandle.symbol == "INFY", OhlcvCandle.timeframe == "3m")
        )
        rows = result.scalars().all()

    assert len(rows) == 1
    bar = rows[0]
    assert bar.ts == base
    assert bar.open == 100.0
    assert bar.high == 105.0
    assert bar.low == 98.0
    assert bar.close == 98.0
    # Each 1m bar's own delta-from-baseline volume, summed: the very first
    # tick INFY ever sees has no prior baseline to diff against (0), then
    # 250-100=150, then 400-250=150.
    assert bar.volume == 300


@pytest.mark.db
async def test_ingest_batch_ignores_ticks_missing_required_fields(test_session_factory) -> None:
    aggregator = TickCandleAggregator(test_session_factory)
    await aggregator.ingest_batch([
        {"symbol": "HDFCBANK", "ltp": None, "ts": _ist(9, 20), "data": {"volume": 100}},
        {"symbol": None, "ltp": 100.0, "ts": _ist(9, 20), "data": {"volume": 100}},
    ])

    async with test_session_factory() as session:
        result = await session.execute(select(OhlcvCandle))
        rows = result.scalars().all()

    assert rows == []


async def test_ingest_batch_is_a_no_op_without_a_session_factory() -> None:
    aggregator = TickCandleAggregator(None)
    # Must not raise -- matches every other lazily-DB-backed collector's
    # "no session factory configured" convention.
    await aggregator.ingest_batch([
        {"symbol": "HDFCBANK", "ltp": 100.0, "ts": datetime.now(UTC), "data": {"volume": 100}},
    ])
