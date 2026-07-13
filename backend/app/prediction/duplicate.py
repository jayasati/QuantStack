"""Duplicate Signal Engine (Volume 5, Prompt 5.14).

"Avoid spam." Processes an already-ranked batch of signals (Prompt 5.13's
SignalPriorityEngine.rank(), highest priority_score first) and greedily
keeps a signal only if it isn't a duplicate of one already kept -- so
between two redundant signals, the higher-priority one always survives.

Four detections, each honestly scoped to what this codebase actually has:

- Repeated Opportunities: the same (symbol, direction) was already ranked
  within a cooldown window, via SignalPriorityEngine's own persisted
  history (`recent()`). A v1 proxy, not a definitive "was this actually
  sent to Telegram" ledger -- that ledger doesn't exist until Prompt
  5.15's Opportunity Lifecycle Manager is built.
- Correlated Stocks: CorrelationIntelligenceEngine (Volume 4, Prompt 4.8)
  is NOT reusable here -- it only computes an 8-asset macro correlation
  matrix (NIFTY/BANKNIFTY/USDINR/CRUDE/GOLD/US_MARKETS/GLOBAL_INDICES/
  SECTOR_INDICES), with no pairwise API for arbitrary equity symbols. This
  engine computes its own pairwise correlation directly from raw
  `price_simple_return` history, reusing app.features.normalize's own
  `rolling_correlation` (the same Pearson-correlation math
  CorrelationIntelligenceEngine itself uses internally) rather than
  re-implementing it -- called with window == the full aligned lookback,
  so it returns exactly one "current correlation" reading rather than a
  per-step series.
- Repeated Breakouts: a substring match for "breakout" (case-insensitive)
  against each signal's own `reason` string (Prompt 5.2's own
  human-readable trigger summary, threaded through onto RankedSignal
  specifically for this check). A second signal with a breakout-flavored
  reason, once one is already kept, is suppressed.
- Sector Duplication: `Settings.feature_stock_sectors` (Prompt 3.8's own
  stock -> sector-index mapping) is the only stock-to-sector map this
  codebase has -- coverage is whatever's configured, not universal, so a
  symbol missing from it reads as "sector unknown" and is NEVER suppressed
  on this basis (never fabricate a sector to justify a suppression).

Suppressed signals are never silently dropped -- every one carries the
specific reason(s) it was suppressed for, and `kept` preserves signal
diversity by construction (at most MAX_SIGNALS_PER_SECTOR per known
sector, at most one breakout-flavored signal, no two correlated symbols
both surviving).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.cache import CacheService
from app.core.config import Settings, get_settings
from app.events.bus import Event, EventBus
from app.features.normalize import rolling_correlation
from app.features.store import FeatureStore
from app.prediction.priority import TOP_N_DEFAULT, RankedSignal, SignalPriorityEngine

EVENT_TYPE = "duplicate_signal.result"

REPEAT_COOLDOWN_MINUTES = 60  # don't re-flag the same (symbol, direction) within an hour
REPEAT_HISTORY_LIMIT = 50

CORRELATION_TIMEFRAME = "D"
CORRELATION_LOOKBACK = 60  # trading days
CORRELATION_THRESHOLD = 0.8  # |r| at or above this reads as "the same underlying bet"

BREAKOUT_KEYWORD = "breakout"
MAX_SIGNALS_PER_SECTOR = 1  # "maintain signal diversity" taken literally: one best per sector


def is_breakout_signal(reason: str) -> bool:
    return BREAKOUT_KEYWORD in reason.lower()


def _aligned_returns(
    series_a: Sequence[Mapping[str, Any]], series_b: Sequence[Mapping[str, Any]]
) -> tuple[list[float], list[float]]:
    """Two FeatureStore.history() rows lists (ts-descending) -> two
    chronologically-ordered, timestamp-aligned value lists."""
    values_a = {row["ts"]: row["value"] for row in series_a if row["value"] is not None}
    values_b = {row["ts"]: row["value"] for row in series_b if row["value"] is not None}
    common_ts = sorted(set(values_a) & set(values_b))
    return [values_a[ts] for ts in common_ts], [values_b[ts] for ts in common_ts]


def pairwise_correlation(returns_a: Sequence[float], returns_b: Sequence[float]) -> float | None:
    """Current Pearson correlation over the full aligned lookback -- a
    single reading, not a per-step series, by calling rolling_correlation
    with window == len(the aligned series)."""
    n = min(len(returns_a), len(returns_b))
    if n < 2:
        return None
    series = rolling_correlation(list(returns_a[-n:]), list(returns_b[-n:]), window=n)
    return series[-1]


@dataclass(frozen=True)
class SuppressedSignal:
    symbol: str
    direction: str
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "direction": self.direction, "reasons": self.reasons}


@dataclass
class DuplicateFilterResult:
    as_of: datetime
    kept: list[RankedSignal] = field(default_factory=list)
    suppressed: list[SuppressedSignal] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "kept": [signal.to_dict() for signal in self.kept],
            "suppressed": [s.to_dict() for s in self.suppressed],
        }


class DuplicateSignalEngine:
    name = "duplicate_signal_engine"

    def __init__(
        self,
        session_factory: Any = None,
        cache: CacheService | None = None,
        settings: Settings | None = None,
        bus: EventBus | None = None,
        priority_engine: SignalPriorityEngine | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings or get_settings()
        self._bus = bus
        self.store = FeatureStore(session_factory=session_factory, cache=cache)
        self._priority = priority_engine or SignalPriorityEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )

    async def filter_signals(self, signals: Sequence[RankedSignal]) -> DuplicateFilterResult:
        """Greedy pass in the given (already priority-ranked) order: a
        signal is kept only if it isn't a duplicate of one already kept."""
        as_of = signals[0].as_of if signals else datetime.now(UTC)
        kept: list[RankedSignal] = []
        suppressed: list[SuppressedSignal] = []
        sector_counts: dict[str, int] = {}
        breakout_kept = False

        for signal in signals:
            reasons: list[str] = []

            if await self._is_repeated_opportunity(signal):
                reasons.append(
                    f"Repeated Opportunity: {signal.symbol} ({signal.direction}) was already "
                    f"flagged within the last {REPEAT_COOLDOWN_MINUTES} minutes."
                )

            correlated_with = await self._correlated_with_any(signal, kept)
            if correlated_with is not None:
                other_symbol, correlation = correlated_with
                reasons.append(
                    f"Correlated Stocks: {signal.symbol} correlates with already-kept "
                    f"{other_symbol} (r={correlation:+.2f})."
                )

            is_breakout = is_breakout_signal(signal.reason)
            if is_breakout and breakout_kept:
                reasons.append("Repeated Breakouts: another breakout signal is already kept.")

            sector = self._settings.feature_stock_sectors.get(signal.symbol)
            if sector is not None and sector_counts.get(sector, 0) >= MAX_SIGNALS_PER_SECTOR:
                reasons.append(f"Sector Duplication: sector '{sector}' is already represented.")

            if reasons:
                suppressed.append(SuppressedSignal(
                    symbol=signal.symbol, direction=signal.direction, reasons=reasons
                ))
                continue

            kept.append(signal)
            if sector is not None:
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
            if is_breakout:
                breakout_kept = True

        result = DuplicateFilterResult(as_of=as_of, kept=kept, suppressed=suppressed)
        await self._persist(result)
        return result

    async def rank_and_filter(self, top_n: int = TOP_N_DEFAULT) -> DuplicateFilterResult:
        """Convenience: a fresh SignalPriorityEngine.rank() call, then
        de-duplicated over its output."""
        signals = await self._priority.rank(top_n=top_n)
        return await self.filter_signals(signals)

    async def _is_repeated_opportunity(self, signal: RankedSignal) -> bool:
        history = await self._priority.recent(symbol=signal.symbol, limit=REPEAT_HISTORY_LIMIT)
        cutoff = signal.as_of - timedelta(minutes=REPEAT_COOLDOWN_MINUTES)
        for row in history:
            if row.get("direction") != signal.direction:
                continue
            row_as_of = datetime.fromisoformat(row["as_of"])
            if cutoff <= row_as_of < signal.as_of:
                return True
        return False

    async def _correlated_with_any(
        self, signal: RankedSignal, kept: Sequence[RankedSignal]
    ) -> tuple[str, float] | None:
        for other in kept:
            correlation = await self._pairwise_correlation(signal.symbol, other.symbol)
            if correlation is not None and abs(correlation) >= CORRELATION_THRESHOLD:
                return other.symbol, correlation
        return None

    async def _pairwise_correlation(self, symbol_a: str, symbol_b: str) -> float | None:
        series_a = await self.store.history(
            "price_simple_return", symbol=symbol_a, timeframe=CORRELATION_TIMEFRAME,
            limit=CORRELATION_LOOKBACK,
        )
        series_b = await self.store.history(
            "price_simple_return", symbol=symbol_b, timeframe=CORRELATION_TIMEFRAME,
            limit=CORRELATION_LOOKBACK,
        )
        returns_a, returns_b = _aligned_returns(series_a, series_b)
        return pairwise_correlation(returns_a, returns_b)

    async def _persist(self, result: DuplicateFilterResult) -> None:
        if self._bus is not None:
            await self._bus.publish(
                Event(type=EVENT_TYPE, payload=result.to_dict(), source=self.name)
            )
        if self._sessions is None:
            return
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            session.add(MarketEvent(
                event_type=EVENT_TYPE,
                source=self.name,
                data=result.to_dict(),
            ))
            await session.commit()

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        if self._sessions is None:
            return []
        from sqlalchemy import desc, select

        from app.database.tables import MarketEvent

        query = (
            select(MarketEvent.data)
            .where(MarketEvent.event_type == EVENT_TYPE)
            .order_by(desc(MarketEvent.id))
            .limit(limit)
        )
        async with self._sessions() as session:
            result = await session.execute(query)
            return list(result.scalars().all())
