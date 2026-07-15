"""Tests for CandleRetentionCollector (2026-07-16) -- nothing pruned
ohlcv_candles before this; each interval keeps only what
HistoricalCandleCollector.default_lookback says it needs."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.collectors.domains.retention import CandleRetentionCollector
from app.collectors.market_data import HistoricalCandleCollector
from app.database.tables import OhlcvCandle


def test_collector_is_after_hours_only() -> None:
    assert CandleRetentionCollector.after_hours_only is True


@pytest.mark.db
async def test_collect_deletes_only_rows_past_their_intervals_lookback(
    test_session_factory,
) -> None:
    now = datetime.now(UTC)
    lookback_1m = HistoricalCandleCollector.default_lookback["1m"]
    lookback_d = HistoricalCandleCollector.default_lookback["D"]

    async with test_session_factory() as session:
        session.add_all([
            # 1m: one row just past its 2-day lookback, one well within it.
            OhlcvCandle(
                symbol="HDFCBANK", timeframe="1m", ts=now - lookback_1m - timedelta(hours=1),
                open=1, high=1, low=1, close=1, volume=0,
            ),
            OhlcvCandle(
                symbol="HDFCBANK", timeframe="1m", ts=now - timedelta(hours=1),
                open=1, high=1, low=1, close=1, volume=0,
            ),
            # D: well within its 2-year lookback -- must survive even
            # though it's much older than 1m's rows.
            OhlcvCandle(
                symbol="HDFCBANK", timeframe="D", ts=now - timedelta(days=30),
                open=1, high=1, low=1, close=1, volume=0,
            ),
            # D: past its 2-year lookback -- must be deleted.
            OhlcvCandle(
                symbol="HDFCBANK", timeframe="D", ts=now - lookback_d - timedelta(days=1),
                open=1, high=1, low=1, close=1, volume=0,
            ),
        ])
        await session.commit()

    collector = CandleRetentionCollector(session_factory=test_session_factory)
    records = await collector.collect()

    async with test_session_factory() as session:
        result = await session.execute(select(OhlcvCandle.timeframe, OhlcvCandle.ts))
        remaining = result.all()

    assert len(remaining) == 2
    timeframes_remaining = {r.timeframe for r in remaining}
    assert timeframes_remaining == {"1m", "D"}
    assert all(r.ts >= now - lookback_1m for r in remaining if r.timeframe == "1m")
    assert all(r.ts >= now - lookback_d for r in remaining if r.timeframe == "D")

    # One record per configured interval, even when nothing needed
    # deleting for most of them -- consistent observability, not
    # conditionally silent.
    assert len(records) == len(HistoricalCandleCollector.default_lookback)
    by_interval = {r.instrument: r.metadata["rows_deleted"] for r in records}
    assert by_interval["1m"] == 1
    assert by_interval["D"] == 1
    assert by_interval["5m"] == 0


async def test_collect_raises_without_a_session_factory() -> None:
    from app.collectors.base import CollectionError

    collector = CandleRetentionCollector()
    collector._sessions = lambda: None  # type: ignore[method-assign]  # keep the test off the real DB
    with pytest.raises(CollectionError):
        await collector.collect()
