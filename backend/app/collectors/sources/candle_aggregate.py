"""Shared OHLC-from-ticks aggregation for the exchange-native candle
fallback sources (NSE, BSE) -- both expose today's intraday price action
only as a raw (timestamp, price) tick series, not pre-built OHLC bars
(unlike the broker's own historical-candle endpoint), so both need the same
bucketing logic. Volume is always 0: neither NSE's nor BSE's public
chart-tick feeds carry per-tick volume, only price."""

from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from app.market.broker import Candle

IST = ZoneInfo("Asia/Kolkata")

# symbol/interval -> minutes, mirrors market_data.py's INTERVAL_MINUTES.
# Duplicated rather than imported to avoid a collectors.sources ->
# collectors.market_data import (sources are meant to be leaf modules).
INTERVAL_MINUTES: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1H": 60,
}


def bucket_ticks_into_candles(
    symbol: str, interval: str, ticks: Sequence[tuple[datetime, float]]
) -> list[Candle]:
    """Bucket a (timestamp, price) tick series -- chronological order not
    required, this sorts -- into `interval`-wide OHLC bars, bucketed on IST
    calendar-minute boundaries (bucket start = tick time floored to the
    interval width). Ticks with a non-finite/missing price are dropped
    before bucketing."""
    minutes = INTERVAL_MINUTES.get(interval)
    if minutes is None:
        return []
    clean = sorted(
        (ts, price) for ts, price in ticks if price is not None
    )
    if not clean:
        return []

    buckets: dict[datetime, list[float]] = {}
    for ts, price in clean:
        ist_ts = ts.astimezone(IST)
        floored_minute = (ist_ts.minute // minutes) * minutes
        bucket_start = ist_ts.replace(
            minute=floored_minute, second=0, microsecond=0
        )
        buckets.setdefault(bucket_start, []).append(price)

    candles: list[Candle] = []
    for bucket_start in sorted(buckets):
        prices = buckets[bucket_start]
        candles.append(
            Candle(
                symbol=symbol,
                interval=interval,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=0,
                timestamp=bucket_start,
            )
        )
    return candles
