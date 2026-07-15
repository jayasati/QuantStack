"""Tests for the shared NSE/BSE tick-to-candle aggregator."""

from datetime import UTC, datetime

from app.collectors.sources.candle_aggregate import bucket_ticks_into_candles


def _ist(hour: int, minute: int, second: int = 0) -> datetime:
    # 2026-07-15 09:00 UTC = 14:30 IST -- picked so the whole test day stays
    # within IST's UTC+5:30 offset without touching a DST-like edge case
    # (India has none, but keeping the arithmetic simple either way).
    from datetime import timedelta

    return datetime(2026, 7, 15, hour, minute, second, tzinfo=UTC) - timedelta(hours=5, minutes=30)


def test_buckets_ticks_into_correct_ohlc_per_bar() -> None:
    ticks = [
        (_ist(9, 15, 5), 100.0),
        (_ist(9, 15, 30), 102.0),
        (_ist(9, 15, 50), 99.0),
        (_ist(9, 16, 10), 101.0),
        (_ist(9, 16, 40), 103.0),
    ]
    candles = bucket_ticks_into_candles("HDFCBANK", "1m", ticks)
    assert len(candles) == 2
    first, second = candles
    assert first.timestamp.hour == 9 and first.timestamp.minute == 15
    assert first.open == 100.0
    assert first.high == 102.0
    assert first.low == 99.0
    assert first.close == 99.0  # last tick in the 09:15 bucket
    assert first.volume == 0
    assert second.open == 101.0
    assert second.close == 103.0


def test_buckets_align_to_interval_width_not_just_minute() -> None:
    """5m bars should floor to the nearest 5-minute boundary, not the
    nearest 1-minute one -- 09:17 belongs in the 09:15 bucket."""
    ticks = [(_ist(9, 17, 0), 50.0), (_ist(9, 19, 59), 55.0), (_ist(9, 20, 1), 60.0)]
    candles = bucket_ticks_into_candles("HDFCBANK", "5m", ticks)
    assert len(candles) == 2
    assert candles[0].timestamp.minute == 15
    assert candles[0].close == 55.0
    assert candles[1].timestamp.minute == 20
    assert candles[1].close == 60.0


def test_candles_are_chronologically_ordered_regardless_of_input_order() -> None:
    ticks = [(_ist(9, 20, 0), 60.0), (_ist(9, 15, 0), 50.0), (_ist(9, 17, 0), 52.0)]
    candles = bucket_ticks_into_candles("HDFCBANK", "5m", ticks)
    assert [c.timestamp.minute for c in candles] == [15, 20]


def test_drops_ticks_with_none_price() -> None:
    ticks = [(_ist(9, 15, 0), 100.0), (_ist(9, 15, 30), None)]
    candles = bucket_ticks_into_candles("HDFCBANK", "1m", ticks)
    assert len(candles) == 1
    assert candles[0].open == candles[0].close == 100.0


def test_empty_ticks_produce_no_candles() -> None:
    assert bucket_ticks_into_candles("HDFCBANK", "5m", []) == []


def test_unknown_interval_produces_no_candles() -> None:
    ticks = [(_ist(9, 15, 0), 100.0)]
    assert bucket_ticks_into_candles("HDFCBANK", "D", ticks) == []
