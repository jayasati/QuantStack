"""Triple Barrier Labeling (Volume 5, Prompt 5.5).

Classic triple-barrier method (Lopez de Prado, "Advances in Financial
Machine Learning"): a hypothetical trade opened at candles[entry] is walked
forward bar by bar until one of three exits resolves it — profit target,
stop, or a maximum holding time (the "vertical barrier"). This is the
training-data generator Prompt 5.6 (Ensemble Prediction) will eventually
need: labels, not live signals, which is why this module runs over
historical candles on demand rather than being scheduled like Prompts
5.1-5.4.

The doc's "expanded" method adds four barrier types beyond the classic
three, each mapped to a real, already-built source rather than invented:
- Dynamic Profit Target / Dynamic Stop: scaled by trailing realized
  volatility computed directly from the same candle window (not a fixed
  %), so barriers widen in high-vol regimes and narrow in calm ones.
- Gap Events: every bar's OPEN is checked against both barriers before its
  high/low are — a barrier can be breached by an overnight/weekend gap
  without ever being "touched" continuously, and the exact fill price is
  less certain when that happens (label_quality reflects this).
- Trailing Barrier: once price has moved favorably past
  BarrierConfig.trail_activation_pct, the stop ratchets toward the
  favorable extreme (BarrierConfig.trail_pct behind it) and never loosens.
- Event Barrier / Liquidity Barrier: force an early exit when
  event_trading_freeze or liquidity_score (Volume 3's real, already-stored
  features) cross a documented threshold during the holding window —
  fetched once per label_history() call and looked up by timestamp per
  entry, not re-queried per entry.

Label assignment (a documented v1 convention, since the doc doesn't fully
spell out the win/loss/timeout/partial-success boundary):
- WIN: the profit target barrier resolved the trade (gap or intrabar).
- LOSS: the fixed stop barrier resolved it, OR a forced/protective exit
  (trailing stop, event barrier, liquidity barrier) closed at a
  non-positive return.
- PARTIAL SUCCESS: a forced/protective exit closed at a POSITIVE return —
  profitable, but short of the full target. This is the label the doc's
  "expanded" method exists to produce; it cannot happen under the classic
  three-barrier method alone.
- TIMEOUT: the maximum holding time was reached with no barrier touch and
  no forced exit at all — genuinely no resolution, kept distinct from
  Partial Success even when the ending return happens to be positive,
  since nothing here actually fired to lock in that outcome.

Store labels separately from features: a dedicated MarketEvent event_type
("triple_barrier.label"), not the feature_store table.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from statistics import pstdev
from typing import Any
from zoneinfo import ZoneInfo

from app.core.cache import CacheService
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.features.schema import Candle
from app.features.store import FeatureStore

logger = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")
EVENT_FEATURE_SYMBOL = "MARKET"
EVENT_FEATURE_TIMEFRAME = "events"  # matches app/features/events.py's EventRiskEngine
LIQUIDITY_FEATURE_TIMEFRAME = "quote"  # matches app/features/liquidity.py's LiquidityFeatureEngine
FEATURE_HISTORY_LIMIT = 2000

EVENT_TYPE = "triple_barrier.label"

TRAILING_VOL_WINDOW = 20  # bars used to estimate trailing realized volatility
MIN_TRAILING_VOL = 0.002  # floor (0.2%/bar) so a dead-flat series can't zero out barriers
K_PROFIT = 2.0  # documented v1 heuristic: 2:1 reward:risk, matching common practice
K_STOP = 1.0
DEFAULT_MAX_HOLDING_BARS = 10
DEFAULT_TRAIL_ACTIVATION_PCT = 0.5  # activates once halfway to the profit target
DEFAULT_TRAIL_PCT = 0.4  # gives back at most 40% of the favorable move once trailing

EVENT_FREEZE_THRESHOLD = 1.0  # event_trading_freeze == 1.0
LIQUIDITY_SCORE_THRESHOLD = 30.0  # 0-100 scale; below this reads as a liquidity crunch


@dataclass(frozen=True)
class BarrierConfig:
    profit_target_pct: float
    stop_pct: float
    max_holding_bars: int = DEFAULT_MAX_HOLDING_BARS
    trail_activation_pct: float = DEFAULT_TRAIL_ACTIVATION_PCT
    trail_pct: float = DEFAULT_TRAIL_PCT

    def to_dict(self) -> dict[str, Any]:
        return {
            "profit_target_pct": self.profit_target_pct,
            "stop_pct": self.stop_pct,
            "max_holding_bars": self.max_holding_bars,
            "trail_activation_pct": self.trail_activation_pct,
            "trail_pct": self.trail_pct,
        }


@dataclass
class Label:
    symbol: str
    timeframe: str
    direction: str  # "long" | "short"
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime | None
    exit_price: float | None
    exit_reason: str
    exit_return_pct: float | None
    label: str  # "win" | "loss" | "timeout" | "partial_success"
    label_quality: float
    bars_held: int
    barrier_config: BarrierConfig
    gap: bool = False
    ambiguous: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "entry_ts": self.entry_ts.isoformat(),
            "entry_price": self.entry_price,
            "exit_ts": self.exit_ts.isoformat() if self.exit_ts else None,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "exit_return_pct": self.exit_return_pct,
            "label": self.label,
            "label_quality": self.label_quality,
            "bars_held": self.bars_held,
            "barrier_config": self.barrier_config.to_dict(),
            "gap": self.gap,
            "ambiguous": self.ambiguous,
        }


def trailing_volatility(candles: Sequence[Candle], end_index: int) -> float:
    """Per-bar (not annualized) realized volatility of log returns over the
    trailing TRAILING_VOL_WINDOW bars ending at end_index (inclusive)."""
    start = max(0, end_index - TRAILING_VOL_WINDOW + 1)
    window = candles[start : end_index + 1]
    returns = [
        math.log(window[i].close / window[i - 1].close)
        for i in range(1, len(window))
        if window[i - 1].close > 0 and window[i].close > 0
    ]
    if len(returns) < 2:
        return MIN_TRAILING_VOL
    return max(pstdev(returns), MIN_TRAILING_VOL)


def barrier_config_for(
    candles: Sequence[Candle],
    entry_index: int,
    max_holding_bars: int = DEFAULT_MAX_HOLDING_BARS,
) -> BarrierConfig:
    """Dynamic Profit Target / Dynamic Stop: scaled by trailing realized
    volatility as of the entry bar, not a fixed percentage."""
    vol = trailing_volatility(candles, entry_index)
    return BarrierConfig(
        profit_target_pct=round(K_PROFIT * vol, 6),
        stop_pct=round(K_STOP * vol, 6),
        max_holding_bars=max_holding_bars,
    )


def _breach_price(entry_price: float, pct: float, sign: float, side: str) -> float:
    """side='profit' moves in the favorable direction, side='stop' against it."""
    move = pct if side == "profit" else -pct
    return entry_price * (1 + sign * move)


def label_single_entry(
    forward_candles: Sequence[Candle],
    entry_price: float,
    entry_ts: datetime,
    direction: str,
    config: BarrierConfig,
    symbol: str = "",
    timeframe: str = "",
    event_barrier_dates: frozenset[date] = frozenset(),
    liquidity_barrier_dates: frozenset[date] = frozenset(),
) -> Label:
    """Pure walk-forward barrier resolution. forward_candles must start
    with the first bar AFTER entry (entry itself is not in this sequence)."""
    sign = 1.0 if direction == "long" else -1.0
    profit_price = _breach_price(entry_price, config.profit_target_pct, sign, "profit")
    fixed_stop_price = _breach_price(entry_price, config.stop_pct, sign, "stop")

    def resolve(
        exit_price: float, exit_ts: datetime, bar_index: int, exit_reason: str,
        gap: bool = False, ambiguous: bool = False,
    ) -> Label:
        return _resolve(
            symbol, timeframe, direction, entry_ts, entry_price, exit_price, exit_ts,
            exit_reason, bar_index, config, gap=gap, ambiguous=ambiguous,
        )

    if not forward_candles:
        return _resolve(
            symbol, timeframe, direction, entry_ts, entry_price, entry_price, entry_ts,
            "insufficient_data", -1, config,
        )

    trailing_active = False
    trailing_stop_price = fixed_stop_price
    favorable_extreme = entry_price

    usable_bars = forward_candles[: config.max_holding_bars]
    for i, candle in enumerate(usable_bars):
        current_stop = trailing_stop_price if trailing_active else fixed_stop_price

        # 1. Gap check at the bar's open, before any intrabar touch: a
        # barrier can be breached by a gap without ever being "touched"
        # continuously.
        gap_profit = candle.open >= profit_price if sign > 0 else candle.open <= profit_price
        gap_stop = candle.open <= current_stop if sign > 0 else candle.open >= current_stop
        if gap_profit and gap_stop:
            # ambiguous gap through both -- conservative: assume the worse outcome
            return resolve(candle.open, candle.ts, i, "stop", gap=True, ambiguous=True)
        if gap_profit:
            return resolve(profit_price, candle.ts, i, "profit_target", gap=True)
        if gap_stop:
            reason = "trailing_stop" if trailing_active else "stop"
            return resolve(current_stop, candle.ts, i, reason, gap=True)

        # 2. Intrabar touch (high/low range).
        touch_profit = candle.high >= profit_price if sign > 0 else candle.low <= profit_price
        touch_stop = candle.low <= current_stop if sign > 0 else candle.high >= current_stop
        if touch_profit and touch_stop:
            return resolve(current_stop, candle.ts, i, "stop", ambiguous=True)
        if touch_profit:
            return resolve(profit_price, candle.ts, i, "profit_target")
        if touch_stop:
            reason = "trailing_stop" if trailing_active else "stop"
            return resolve(current_stop, candle.ts, i, reason)

        # 3. Event / Liquidity barriers force an exit at this bar's close.
        # Matched by IST calendar date, not exact timestamp: event/liquidity
        # features run on different cadences ("events"/"quote") than the
        # candles being labeled ("D" or intraday), so they rarely share an
        # exact timestamp even when they cover the same trading day.
        bar_date = candle.ts.astimezone(IST).date()
        if bar_date in event_barrier_dates:
            return resolve(candle.close, candle.ts, i, "event_barrier")
        if bar_date in liquidity_barrier_dates:
            return resolve(candle.close, candle.ts, i, "liquidity_barrier")

        # 4. Trailing stop bookkeeping (never loosens once active). Activation
        # and trail distance are both expressed as a fraction of the profit
        # target distance, so they scale with the same dynamic volatility
        # barriers do.
        favorable_extreme = (
            max(favorable_extreme, candle.high) if sign > 0
            else min(favorable_extreme, candle.low)
        )
        favorable_move_pct = sign * (favorable_extreme - entry_price) / entry_price
        activation = config.trail_activation_pct * config.profit_target_pct
        if not trailing_active and favorable_move_pct >= activation:
            trailing_active = True
        if trailing_active:
            trail_distance = config.trail_pct * config.profit_target_pct
            candidate_trail = favorable_extreme * (1 - sign * trail_distance)
            trailing_stop_price = (
                max(trailing_stop_price, candidate_trail) if sign > 0
                else min(trailing_stop_price, candidate_trail)
            )

    last = usable_bars[-1]
    if len(forward_candles) < config.max_holding_bars:
        return resolve(last.close, last.ts, len(usable_bars) - 1, "insufficient_data")
    return resolve(last.close, last.ts, len(usable_bars) - 1, "max_holding_time")


def _resolve(
    symbol: str,
    timeframe: str,
    direction: str,
    entry_ts: datetime,
    entry_price: float,
    exit_price: float,
    exit_ts: datetime,
    exit_reason: str,
    bar_index: int,
    config: BarrierConfig,
    gap: bool = False,
    ambiguous: bool = False,
) -> Label:
    sign = 1.0 if direction == "long" else -1.0
    exit_return_pct = round(sign * (exit_price - entry_price) / entry_price * 100, 4)

    if exit_reason == "profit_target":
        label = "win"
    elif exit_reason == "stop":
        label = "loss"
    elif exit_reason in ("trailing_stop", "event_barrier", "liquidity_barrier"):
        label = "partial_success" if exit_return_pct > 0 else "loss"
    else:  # max_holding_time, insufficient_data
        label = "timeout"

    quality = 1.0
    if gap:
        quality -= 0.3
    if ambiguous:
        quality -= 0.4
    if exit_reason == "max_holding_time":
        quality -= 0.2
    if exit_reason == "insufficient_data":
        quality = 0.1
    quality = max(0.0, min(1.0, quality))

    return Label(
        symbol=symbol, timeframe=timeframe, direction=direction, entry_ts=entry_ts,
        entry_price=entry_price, exit_ts=exit_ts, exit_price=round(exit_price, 4),
        exit_reason=exit_reason, exit_return_pct=exit_return_pct, label=label,
        label_quality=round(quality, 4), bars_held=bar_index + 1,
        barrier_config=config, gap=gap, ambiguous=ambiguous,
    )


class TripleBarrierLabelingEngine:
    """Runs on demand over historical candles — a training-data generator,
    not a scheduled live engine like Prompts 5.1-5.4."""

    name = "triple_barrier_labeling_engine"

    def __init__(
        self,
        session_factory: Any = None,
        cache: CacheService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings or get_settings()
        self.store = FeatureStore(session_factory=session_factory, cache=cache)

    async def label_history(
        self,
        symbol: str,
        timeframe: str = "D",
        direction: str = "long",
        lookback_bars: int = 100,
        max_holding_bars: int = DEFAULT_MAX_HOLDING_BARS,
    ) -> list[Label]:
        """One label per historical entry point over the trailing
        `lookback_bars`, each resolved by walking forward through candles
        that already exist in the database (this is a backtest, not a live
        prediction — the "future" bars are already known history)."""
        candles = await self._load_candles(symbol, timeframe)
        if len(candles) < TRAILING_VOL_WINDOW + 2:
            return []

        # Every entry needs TRAILING_VOL_WINDOW bars of history behind it
        # (for the dynamic barrier calc) and at least 1 forward candle ahead.
        first_entry_index = TRAILING_VOL_WINDOW
        last_entry_index = len(candles) - 2
        if first_entry_index > last_entry_index:
            return []
        start_index = max(first_entry_index, last_entry_index - lookback_bars + 1)

        event_dates, liquidity_dates = await self._breach_dates(symbol)

        labels = [
            label_single_entry(
                candles[entry_index + 1 : entry_index + 1 + max_holding_bars],
                candles[entry_index].close,
                candles[entry_index].ts,
                direction,
                barrier_config_for(candles, entry_index, max_holding_bars),
                symbol=symbol,
                timeframe=timeframe,
                event_barrier_dates=event_dates,
                liquidity_barrier_dates=liquidity_dates,
            )
            for entry_index in range(start_index, last_entry_index + 1)
        ]
        await self._persist(labels)
        return labels

    async def _load_candles(self, symbol: str, timeframe: str) -> list[Candle]:
        if self._sessions is None:
            return []
        from sqlalchemy import select

        from app.database.tables import OhlcvCandle

        lookback = self._settings.feature_candle_lookback
        async with self._sessions() as session:
            result = await session.execute(
                select(OhlcvCandle)
                .where(OhlcvCandle.symbol == symbol, OhlcvCandle.timeframe == timeframe)
                .order_by(OhlcvCandle.ts.desc())
                .limit(lookback)
            )
            rows = result.scalars().all()
        return [
            Candle(
                ts=row.ts, open=row.open, high=row.high, low=row.low,
                close=row.close, volume=row.volume or 0,
            )
            for row in reversed(rows)
        ]

    async def _breach_dates(self, symbol: str) -> tuple[frozenset[date], frozenset[date]]:
        """IST calendar dates where the Event Barrier / Liquidity Barrier
        should force an exit — fetched once per label_history() call, not
        re-queried per entry point."""
        if self._sessions is None:
            return frozenset(), frozenset()

        event_rows = await self.store.history(
            "event_trading_freeze", symbol=EVENT_FEATURE_SYMBOL,
            timeframe=EVENT_FEATURE_TIMEFRAME, limit=FEATURE_HISTORY_LIMIT,
        )
        liquidity_rows = await self.store.history(
            "liquidity_score", symbol=symbol,
            timeframe=LIQUIDITY_FEATURE_TIMEFRAME, limit=FEATURE_HISTORY_LIMIT,
        )
        event_dates = frozenset(
            datetime.fromisoformat(row["ts"]).astimezone(IST).date()
            for row in event_rows
            if row["value"] is not None and row["value"] >= EVENT_FREEZE_THRESHOLD
        )
        liquidity_dates = frozenset(
            datetime.fromisoformat(row["ts"]).astimezone(IST).date()
            for row in liquidity_rows
            if row["value"] is not None and row["value"] < LIQUIDITY_SCORE_THRESHOLD
        )
        return event_dates, liquidity_dates

    async def _persist(self, labels: list[Label]) -> None:
        """Store labels separately from features: a dedicated event_type,
        never the feature_store table. One row per label."""
        if self._sessions is None or not labels:
            return
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            for label in labels:
                session.add(MarketEvent(
                    event_type=EVENT_TYPE,
                    source=self.name,
                    data=label.to_dict(),
                ))
            await session.commit()

    async def recent(
        self, symbol: str | None = None, label: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        if self._sessions is None:
            return []
        from sqlalchemy import desc, select

        from app.database.tables import MarketEvent

        query = select(MarketEvent.data).where(MarketEvent.event_type == EVENT_TYPE)
        if symbol is not None:
            query = query.where(MarketEvent.data["symbol"].astext == symbol)
        if label is not None:
            query = query.where(MarketEvent.data["label"].astext == label)
        query = query.order_by(desc(MarketEvent.id)).limit(limit)
        async with self._sessions() as session:
            result = await session.execute(query)
            return list(result.scalars().all())
