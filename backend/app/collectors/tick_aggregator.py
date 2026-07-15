"""Real-time candle aggregation from live ticks (2026-07-16).

LiveMarketCollector already polls/streams a tick every 15s during market
hours; this builds and live-updates the current 1-minute OHLCV bar directly
from those ticks, instead of waiting on HistoricalCandleCollector's 300s
external-source sweep to notice new data exists. Coarser bars
(3m/5m/15m/30m/1H) are re-derived by folding the underlying 1m bars on every
cycle, not tracked as separate running state -- always fresh, self-healing
if a tick was missed.

Layer boundary: this only ever touches the CURRENT (most recent) bucket for
each interval -- it never rewrites history, so it can't fight with
HistoricalCandleCollector's own writes for anything but "right now".
HistoricalCandleCollector remains the deep-backfill/gap-filler layer
underneath (D always comes from there; anything older than what this layer
has seen live in the last couple of days does too) -- it uses
INSERT ... ON CONFLICT DO NOTHING, so it can never clobber this layer's
more complete, more current data. This layer uses DO UPDATE, since
overwriting its OWN previous partial state with a more complete one as
more ticks arrive is exactly the intent (2026-07-16 decision: the forming
candle should visibly live-update, not just appear once its minute closes).

The NSE/BSE "today-only" fallback in HistoricalCandleCollector predates
this layer and was the original workaround for DEBT-2 (broker's own candle
pipeline silently lagging). It stays wired as a last-resort path -- if this
layer has a gap (WebSocket drop, container restart) and a direct broker
fetch also fails -- but is no longer the primary source for "today's" data.

Batching: everything below is grouped into a small, FIXED number of SQL
round trips per ingest_batch() call (one multi-row upsert for 1m, then one
SELECT + one multi-row upsert per rollup interval), not one round trip per
symbol -- an earlier per-symbol version measured 111ms/symbol against a
real Postgres in test_load_and_performance.py, well past Volume 1's <100ms
target; this version's round-trip count doesn't grow with symbol count at
all (still ~11 statements total whether it's 3 symbols or 25).
"""

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.database.tables import OhlcvCandle

logger = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Session-anchored, not clock-anchored: NSE's 09:15 open, matching the
# broker's own historical-candle convention (verified live 2026-07-15 --
# stored 1H/30m/15m bars land on 09:15/10:15/..., not 09:00/10:00/...).
SESSION_OPEN_HOUR = 9
SESSION_OPEN_MINUTE = 15

# interval -> width in minutes. "1m" is the base unit this layer builds
# directly from ticks; the rest are folded from stored 1m bars.
ROLLUP_INTERVALS: dict[str, int] = {"3m": 3, "5m": 5, "15m": 15, "30m": 30, "1H": 60}


def floor_to_session_bucket(ts: datetime, minutes: int) -> datetime:
    """Bucket start for `ts`, anchored to the 09:15 IST session open rather
    than midnight -- so e.g. 1H buckets land on 09:15/10:15/... like the
    broker's own bars, not 09:00/10:00/.... Returns an IST-aware datetime
    (storing it as-is is fine: Postgres TIMESTAMPTZ normalizes by instant
    regardless of the attached tzinfo, same convention already used by
    candle_aggregate.py's NSE/BSE bucketing)."""
    ist_ts = ts.astimezone(IST)
    session_open = ist_ts.replace(
        hour=SESSION_OPEN_HOUR, minute=SESSION_OPEN_MINUTE, second=0, microsecond=0
    )
    elapsed_minutes = (ist_ts - session_open).total_seconds() // 60
    bucket_index = int(elapsed_minutes // minutes)
    return session_open + timedelta(minutes=bucket_index * minutes)


class TickCandleAggregator:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None) -> None:
        self._sessions = session_factory
        # symbol -> in-progress 1m bar state (pure in-memory cache; the DB
        # row is the actual source of truth, this just avoids a read before
        # every update). Lost on restart -- acceptable, see module docstring.
        self._bars: dict[str, dict] = {}
        # symbol -> last cumulative day-volume seen. Angel One's quote/tick
        # volume field is cumulative for the day, not per-tick, so each 1m
        # bar's own volume is measured as a delta from this baseline.
        self._last_cum_volume: dict[str, int] = {}

    async def ingest_batch(self, ticks: list[dict]) -> None:
        if self._sessions is None or not ticks:
            return
        updates: list[tuple[str, dict, int]] = []
        for tick in ticks:
            symbol = tick.get("symbol")
            ltp = tick.get("ltp")
            ts = tick.get("ts")
            volume = (tick.get("data") or {}).get("volume")
            if symbol is None or ltp is None or ts is None:
                continue
            bar, bar_volume = self._update_bar(symbol, ltp, volume, ts)
            updates.append((symbol, bar, bar_volume))
        if not updates:
            return

        try:
            async with self._sessions() as session:
                await self._upsert_many(session, [
                    (symbol, "1m", bar["bucket"], bar["open"], bar["high"], bar["low"],
                     bar["close"], bar_volume)
                    for symbol, bar, bar_volume in updates
                ])
                # Almost always one shared bucket (every symbol's tick this
                # cycle floors to the same current minute) -- grouping
                # handles the rare case of a cycle straddling a minute
                # boundary without a per-symbol query.
                symbols_by_bucket: dict[datetime, list[str]] = defaultdict(list)
                for symbol, bar, _ in updates:
                    symbols_by_bucket[bar["bucket"]].append(symbol)
                for bucket_1m, symbols in symbols_by_bucket.items():
                    await self._upsert_rollups(session, symbols, bucket_1m)
                await session.commit()
        except Exception as exc:
            logger.warning("live candle aggregation failed", extra={"error": str(exc)})

    def _update_bar(
        self, symbol: str, ltp: float, cumulative_volume: int | None, ts: datetime
    ) -> tuple[dict, int]:
        bucket = floor_to_session_bucket(ts, 1)
        baseline = self._last_cum_volume.get(symbol, cumulative_volume or 0)
        bar = self._bars.get(symbol)
        if bar is None or bar["bucket"] != bucket:
            bar = {"bucket": bucket, "open": ltp, "high": ltp, "low": ltp, "close": ltp}
            self._bars[symbol] = bar
            # A fresh bucket's volume baseline is whatever cumulative
            # volume had already been seen going into it, not this tick's
            # own value -- otherwise the bar's very first tick always
            # measures a volume of 0.
            bar["baseline_volume"] = baseline
        else:
            bar["high"] = max(bar["high"], ltp)
            bar["low"] = min(bar["low"], ltp)
            bar["close"] = ltp
        if cumulative_volume is not None:
            self._last_cum_volume[symbol] = cumulative_volume
        bar_volume = max(0, (cumulative_volume or 0) - bar["baseline_volume"])
        return bar, bar_volume

    async def _upsert_rollups(self, session, symbols: list[str], bucket_1m: datetime) -> None:
        for interval, minutes in ROLLUP_INTERVALS.items():
            bucket = floor_to_session_bucket(bucket_1m, minutes)
            window_end = bucket + timedelta(minutes=minutes)
            result = await session.execute(
                select(
                    OhlcvCandle.symbol, OhlcvCandle.ts, OhlcvCandle.open,
                    OhlcvCandle.high, OhlcvCandle.low, OhlcvCandle.close, OhlcvCandle.volume,
                )
                .where(
                    OhlcvCandle.symbol.in_(symbols),
                    OhlcvCandle.timeframe == "1m",
                    OhlcvCandle.ts >= bucket,
                    OhlcvCandle.ts < window_end,
                )
                .order_by(OhlcvCandle.symbol, OhlcvCandle.ts.asc())
            )
            by_symbol: dict[str, list] = defaultdict(list)
            for row in result.all():
                by_symbol[row.symbol].append(row)
            if not by_symbol:
                continue
            await self._upsert_many(session, [
                (
                    symbol, interval, bucket, rows[0].open,
                    max(r.high for r in rows), min(r.low for r in rows),
                    rows[-1].close, sum(r.volume or 0 for r in rows),
                )
                for symbol, rows in by_symbol.items()
            ])

    @staticmethod
    async def _upsert_many(
        session, rows: list[tuple[str, str, datetime, float, float, float, float, int]]
    ) -> None:
        if not rows:
            return
        stmt = pg_insert(OhlcvCandle).values([
            {
                "symbol": symbol, "timeframe": timeframe, "ts": ts,
                "open": open_, "high": high, "low": low, "close": close, "volume": volume,
            }
            for symbol, timeframe, ts, open_, high, low, close, volume in rows
        ])
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "timeframe", "ts"],
            set_={
                "open": stmt.excluded.open, "high": stmt.excluded.high,
                "low": stmt.excluded.low, "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
            },
        )
        await session.execute(stmt)
