"""Tests for date-range-scoped historical feature regeneration
(data foundation audit 2026-07-17, historical regeneration item).

BaseFeatureEngine.run(start=..., end=...) replaces the default trailing-
lookback candle window with an explicit date range -- these tests seed real
OHLCV history spanning a wider window than the requested range and confirm
only the in-range candles are actually computed/stored, against a real
Postgres (matching this project's I-3 discipline: date-range queries must be
verified at real scale, not just unit-tested against a tiny fixture).
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert

from app.database.tables import OhlcvCandle
from app.features.base import BaseFeatureEngine
from app.features.schema import FeatureDefinition, Series

pytestmark = pytest.mark.db

SYMBOL = "TESTSYM"
BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


class _RangeTestEngine(BaseFeatureEngine):
    name = "range_test_engine"
    category = "test"

    def _definitions(self) -> list[FeatureDefinition]:
        return [FeatureDefinition(feature_name="range_test_feature", category="test",
                                   description="test")]

    def _compute(self, candles, benchmark=None) -> dict[str, Series]:
        return {"range_test_feature": [c.close for c in candles]}


async def _seed_candles(session_factory, n_days: int = 30) -> None:
    rows = [
        {
            "symbol": SYMBOL, "timeframe": "D", "ts": BASE_TS + timedelta(days=i),
            "open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i,
            "close": 100.5 + i, "volume": 1000,
        }
        for i in range(n_days)
    ]
    async with session_factory() as session:
        await session.execute(insert(OhlcvCandle), rows)
        await session.commit()


async def test_run_with_a_date_range_only_stores_in_range_values(
    test_session_factory,
) -> None:
    await _seed_candles(test_session_factory, n_days=30)
    engine = _RangeTestEngine(session_factory=test_session_factory)

    start = BASE_TS + timedelta(days=10)
    end = BASE_TS + timedelta(days=19)
    result = await engine.run(SYMBOL, "D", start=start, end=end)

    assert result["stored"] == 10  # days 10..19 inclusive, not the full 30

    history = await engine.store.history(
        "range_test_feature", symbol=SYMBOL, timeframe="D", limit=100,
    )
    timestamps = sorted(datetime.fromisoformat(row["ts"]) for row in history)
    assert timestamps[0] >= start
    assert timestamps[-1] <= end
    assert len(timestamps) == 10


async def test_run_with_only_a_start_bound_is_open_ended(test_session_factory) -> None:
    await _seed_candles(test_session_factory, n_days=15)
    engine = _RangeTestEngine(session_factory=test_session_factory)

    start = BASE_TS + timedelta(days=10)
    result = await engine.run(SYMBOL, "D", start=start)

    assert result["stored"] == 5  # days 10..14 inclusive


async def test_date_ranged_run_forces_full_semantics_even_without_the_flag(
    test_session_factory,
) -> None:
    """Without this, the incremental watermark (latest_ts_map) would only
    look at the MOST RECENT stored ts -- almost always newer than a past
    date range being targeted -- and silently write nothing at all."""
    await _seed_candles(test_session_factory, n_days=30)
    engine = _RangeTestEngine(session_factory=test_session_factory)

    # First, a normal run stores everything and advances the watermark
    # to day 29 (the most recent candle).
    await engine.run(SYMBOL, "D")

    # Now regenerate an EARLIER window -- full=False (the default), but a
    # date range is given, so it must still produce rows despite every
    # requested ts being older than the current watermark.
    start = BASE_TS + timedelta(days=2)
    end = BASE_TS + timedelta(days=5)
    result = await engine.run(SYMBOL, "D", start=start, end=end)

    assert result["stored"] == 4  # days 2..5 inclusive, not silently 0
