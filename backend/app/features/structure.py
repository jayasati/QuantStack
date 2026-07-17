"""Market Structure Feature Engine (Volume 3, Prompt 3.9).

Encodes price-action structure as machine-readable features on two passes:

Daily pass (aligned to daily bars):
- Swing High/Low: fractal pivots (feature_structure_fractal bars each side),
  confirmed with the fractal lag so no value uses future bars.
- Trend Direction: +1 when the last swings make HH+HL, -1 for LH+LL, 0 mixed.
  Higher Highs / Lower Lows count consecutive swings in sequence.
- Break of Structure fires +/-1 on the bar that closes beyond the last
  confirmed swing; Change of Character is a BOS against the prevailing trend.
- Liquidity Zone Distance: % distance from close to the nearest recent swing
  level (resting liquidity). Fair Value Gaps: net unfilled 3-bar gaps
  (bullish minus bearish); a gap counts as filled once price trades back
  through its midpoint. Order Block Distance: % distance to the most recent
  opposite-colored candle that preceded a >1 ATR impulse.
- Structural Bias blends trend, decayed BOS direction, and the HH/LL balance
  into -1..1. Breakout and Liquidity-Sweep Probabilities are documented v1
  heuristics on bias and level proximity (calibratable, replaceable as v2).

Session pass (from intraday candles, one value per session, stored on the
daily timeframe at midnight IST):
- VWAP Bands: +/-2-sigma band width in % and the close's position inside the
  band; when the instrument reports no volume (indices) the time-weighted
  average price stands in for VWAP.
- Opening Range (first 15 minutes) and Initial Balance (first hour): range in
  % plus the close's position / the session's extension beyond the IB.
- Market/Volume Profile: closes binned across the session range, weighted by
  volume (bar count when volume is absent): POC distance and the 70% value
  area width. Auction Imbalance: where the close settled within the session
  range, -1..1.

Every feature ships a look-ahead-safe rolling z-score companion (_z).
"""

import math
from collections.abc import Sequence
from datetime import datetime
from statistics import fmean
from zoneinfo import ZoneInfo

from app.core.logging import get_logger
from app.features.base import BaseFeatureEngine
from app.features.normalize import add_normalized_series, normalized_definition
from app.features.schema import Candle, FeatureDefinition, Series

logger = get_logger(__name__)

ENGINE_NAME = "market_structure_engine"
ENGINE_VERSION = "v1"
CATEGORY = "structure"

IST = ZoneInfo("Asia/Kolkata")

OPENING_RANGE_BARS = 3   # 3 x 5m = first 15 minutes
INITIAL_BALANCE_BARS = 12  # 12 x 5m = first hour
MIN_SESSION_BARS = 15
PROFILE_BINS = 30
VALUE_AREA = 0.70
FVG_LOOKBACK = 100
LIQUIDITY_LEVELS = 5
ATR_WINDOW = 14
BOS_DECAY_BARS = 10


# --- Feature definitions -------------------------------------------------------

def structure_feature_definitions(
    normalization_window: int,
    calculation_frequency: str = "on_schedule",
) -> list[FeatureDefinition]:
    def define(name: str, description: str, unit: str,
               expected: tuple[float | None, float | None],
               dependencies: tuple[str, ...] = (),
               ) -> FeatureDefinition:
        return FeatureDefinition(
            feature_name=name,
            category=CATEGORY,
            description=description,
            version=ENGINE_VERSION,
            dependencies=dependencies,
            calculation_frequency=calculation_frequency,
            owner=ENGINE_NAME,
            unit=unit,
            expected_range=expected,
        )

    definitions = [
        # Daily pass.
        define("ms_swing_high", "Last confirmed fractal swing high level.",
               "price", (0.0, None)),
        define("ms_swing_low", "Last confirmed fractal swing low level.",
               "price", (0.0, None)),
        define("ms_trend_direction", "+1 HH+HL, -1 LH+LL, 0 mixed.",
               "direction", (-1.0, 1.0), ("ms_swing_high", "ms_swing_low")),
        define("ms_higher_highs", "Consecutive higher swing highs.",
               "count", (0.0, None), ("ms_swing_high",)),
        define("ms_lower_lows", "Consecutive lower swing lows.",
               "count", (0.0, None), ("ms_swing_low",)),
        define("ms_break_of_structure",
               "+1/-1 on the bar closing beyond the last swing, else 0.",
               "signal", (-1.0, 1.0), ("ms_swing_high", "ms_swing_low")),
        define("ms_change_of_character",
               "BOS against the prevailing trend: +1/-1 on the bar, else 0.",
               "signal", (-1.0, 1.0), ("ms_break_of_structure", "ms_trend_direction")),
        define("ms_liquidity_zone_distance_pct",
               "Distance to the nearest recent swing level, % of close.",
               "%", (0.0, 50.0), ("ms_swing_high", "ms_swing_low")),
        define("ms_fvg_net_unfilled",
               "Unfilled bullish minus bearish fair value gaps in the lookback.",
               "count", (None, None)),
        define("ms_order_block_distance_pct",
               "Distance to the most recent order block level, % of close.",
               "%", (-50.0, 50.0)),
        define("ms_structural_bias",
               "Trend + decayed BOS + HH/LL balance, -1..1.",
               "ratio", (-1.0, 1.0),
               ("ms_trend_direction", "ms_break_of_structure")),
        define("ms_breakout_probability",
               "v1 heuristic: bias plus swing-high proximity, 0.05..0.95.",
               "probability", (0.0, 1.0), ("ms_structural_bias",)),
        define("ms_sweep_probability",
               "v1 heuristic: liquidity-level proximity, 0.05..0.95.",
               "probability", (0.0, 1.0), ("ms_liquidity_zone_distance_pct",)),
        # Session pass.
        define("ms_vwap_band_width_pct",
               "+/-2-sigma VWAP band width, % of VWAP (TWAP when no volume).",
               "%", (0.0, 20.0)),
        define("ms_vwap_band_position",
               "Close position inside the VWAP band, in sigmas (clipped +/-4).",
               "zscore", (-4.0, 4.0)),
        define("ms_opening_range_pct", "First-15m range, % of open.",
               "%", (0.0, 10.0)),
        define("ms_opening_range_position",
               "Close vs the opening-range mid, in half-ranges (clipped +/-5).",
               "ratio", (-5.0, 5.0), ("ms_opening_range_pct",)),
        define("ms_initial_balance_pct", "First-hour range, % of open.",
               "%", (0.0, 10.0)),
        define("ms_ib_extension",
               "Session range as a multiple of the initial-balance range.",
               "ratio", (1.0, 20.0), ("ms_initial_balance_pct",)),
        define("ms_value_area_width_pct",
               "70% value-area width of the session profile, % of close.",
               "%", (0.0, 20.0)),
        define("ms_poc_distance_pct",
               "Close distance from the session point of control, % of POC.",
               "%", (-10.0, 10.0)),
        define("ms_auction_imbalance",
               "Where the close settled within the session range, -1..1.",
               "ratio", (-1.0, 1.0)),
    ]
    definitions.extend(
        normalized_definition(d, normalization_window) for d in list(definitions)
    )
    return definitions


# --- Daily pass -------------------------------------------------------------------

def _confirmed_swings(
    candles: Sequence[Candle], k: int
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """(confirm_index, level) for fractal swing highs and lows.

    A pivot at bar j confirms at bar j+k — features may only use it from the
    confirmation bar onward (look-ahead safety).
    """
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []
    for j in range(k, len(candles) - k):
        neighborhood = range(j - k, j + k + 1)
        if all(highs[j] >= highs[m] for m in neighborhood):
            swing_highs.append((j + k, highs[j]))
        if all(lows[j] <= lows[m] for m in neighborhood):
            swing_lows.append((j + k, lows[j]))
    return swing_highs, swing_lows


def compute_structure_daily(
    candles: Sequence[Candle],
    fractal: int = 2,
    normalization_window: int = 100,
) -> dict[str, Series]:
    """Daily-structure features aligned to `candles`."""
    n = len(candles)
    closes = [c.close for c in candles]
    swing_high_events, swing_low_events = _confirmed_swings(candles, fractal)

    atr: Series = [None] * n
    true_ranges: list[float] = [0.0] * n
    for i in range(1, n):
        prev_close = closes[i - 1]
        true_ranges[i] = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - prev_close),
            abs(candles[i].low - prev_close),
        )
        if i >= ATR_WINDOW:
            atr[i] = fmean(true_ranges[i - ATR_WINDOW + 1 : i + 1])

    swing_high: Series = [None] * n
    swing_low: Series = [None] * n
    trend: Series = [None] * n
    higher_highs: Series = [None] * n
    lower_lows: Series = [None] * n
    bos: Series = [0.0] * n
    choch: Series = [0.0] * n
    liquidity_distance: Series = [None] * n
    fvg_net: Series = [None] * n
    order_block_distance: Series = [None] * n
    bias: Series = [None] * n
    breakout_prob: Series = [None] * n
    sweep_prob: Series = [None] * n

    high_idx = 0
    low_idx = 0
    highs_seen: list[float] = []
    lows_seen: list[float] = []
    hh_run = 0
    ll_run = 0
    broken_high: float | None = None
    broken_low: float | None = None
    last_bos_bar: int | None = None
    last_bos_direction = 0.0

    # Fair value gaps: (top, bottom, +1/-1) still unfilled.
    open_gaps: list[tuple[float, float, float]] = []
    last_order_block: float | None = None

    for i in range(n):
        while high_idx < len(swing_high_events) and swing_high_events[high_idx][0] <= i:
            level = swing_high_events[high_idx][1]
            high_idx += 1
            # Equal highs are one liquidity level, not a new swing.
            if highs_seen and math.isclose(level, highs_seen[-1]):
                continue
            hh_run = hh_run + 1 if highs_seen and level > highs_seen[-1] else 0
            highs_seen.append(level)
            broken_high = None  # a fresh swing re-arms the BOS trigger
        while low_idx < len(swing_low_events) and swing_low_events[low_idx][0] <= i:
            level = swing_low_events[low_idx][1]
            low_idx += 1
            if lows_seen and math.isclose(level, lows_seen[-1]):
                continue
            ll_run = ll_run + 1 if lows_seen and level < lows_seen[-1] else 0
            lows_seen.append(level)
            broken_low = None

        if highs_seen:
            swing_high[i] = highs_seen[-1]
            higher_highs[i] = float(hh_run)
        if lows_seen:
            swing_low[i] = lows_seen[-1]
            lower_lows[i] = float(ll_run)

        if len(highs_seen) >= 2 and len(lows_seen) >= 2:
            up = highs_seen[-1] > highs_seen[-2] and lows_seen[-1] > lows_seen[-2]
            down = highs_seen[-1] < highs_seen[-2] and lows_seen[-1] < lows_seen[-2]
            trend[i] = 1.0 if up else (-1.0 if down else 0.0)

        close = closes[i]
        last_high, last_low = swing_high[i], swing_low[i]
        prevailing = trend[i] or 0.0
        if last_high is not None and close > last_high and broken_high != last_high:
            bos[i] = 1.0
            broken_high = last_high
            last_bos_bar, last_bos_direction = i, 1.0
            if prevailing < 0:
                choch[i] = 1.0
        elif last_low is not None and close < last_low and broken_low != last_low:
            bos[i] = -1.0
            broken_low = last_low
            last_bos_bar, last_bos_direction = i, -1.0
            if prevailing > 0:
                choch[i] = -1.0

        levels = [
            *highs_seen[-LIQUIDITY_LEVELS:],
            *lows_seen[-LIQUIDITY_LEVELS:],
        ]
        if levels and close > 0:
            liquidity_distance[i] = min(abs(level - close) for level in levels) / close * 100

        # Fair value gaps form on three-bar displacement and fill at midpoint.
        if i >= 2:
            if candles[i].low > candles[i - 2].high:
                open_gaps.append((candles[i].low, candles[i - 2].high, 1.0))
            elif candles[i].high < candles[i - 2].low:
                open_gaps.append((candles[i - 2].low, candles[i].high, -1.0))
        still_open = []
        for top, bottom, direction in open_gaps[-FVG_LOOKBACK:]:
            midpoint = (top + bottom) / 2
            filled = candles[i].low <= midpoint <= candles[i].high
            if not filled:
                still_open.append((top, bottom, direction))
        open_gaps = still_open
        fvg_net[i] = float(sum(direction for _, _, direction in open_gaps))

        # Order block: opposite-colored bar preceding a >1 ATR impulse.
        atr_value = atr[i]
        if i >= 1 and atr_value is not None:
            impulse = closes[i] - closes[i - 1]
            prev = candles[i - 1]
            if impulse > atr_value and prev.close < prev.open:
                last_order_block = (prev.high + prev.low) / 2
            elif impulse < -atr_value and prev.close > prev.open:
                last_order_block = (prev.high + prev.low) / 2
        if last_order_block is not None and close > 0:
            order_block_distance[i] = (close - last_order_block) / last_order_block * 100

        trend_value = trend[i]
        if trend_value is not None:
            decayed_bos = 0.0
            if last_bos_bar is not None and i - last_bos_bar <= BOS_DECAY_BARS:
                decayed_bos = last_bos_direction * (1 - (i - last_bos_bar) / BOS_DECAY_BARS)
            hh = higher_highs[i] or 0.0
            ll = lower_lows[i] or 0.0
            bias_value = 0.5 * trend_value + 0.3 * decayed_bos + 0.2 * math.tanh((hh - ll) / 3)
            bias_value = max(-1.0, min(1.0, bias_value))
            bias[i] = bias_value

            if last_high is not None and close > 0:
                proximity = 1 - min(abs(last_high - close) / close * 100, 2.0) / 2.0
                breakout_prob[i] = max(
                    0.05, min(0.95, 0.5 + 0.3 * bias_value + 0.2 * proximity)
                )
        liq = liquidity_distance[i]
        if liq is not None:
            sweep_prob[i] = max(
                0.05, min(0.95, 0.15 + 0.7 * (1 - min(liq, 2.0) / 2.0))
            )

    out: dict[str, Series] = {
        "ms_swing_high": swing_high,
        "ms_swing_low": swing_low,
        "ms_trend_direction": trend,
        "ms_higher_highs": higher_highs,
        "ms_lower_lows": lower_lows,
        "ms_break_of_structure": bos,
        "ms_change_of_character": choch,
        "ms_liquidity_zone_distance_pct": liquidity_distance,
        "ms_fvg_net_unfilled": fvg_net,
        "ms_order_block_distance_pct": order_block_distance,
        "ms_structural_bias": bias,
        "ms_breakout_probability": breakout_prob,
        "ms_sweep_probability": sweep_prob,
    }
    return add_normalized_series(out, normalization_window)


# --- Session pass -----------------------------------------------------------------

def _session_features(bars: Sequence[Candle]) -> dict[str, float | None]:
    closes = [b.close for b in bars]
    volumes = [float(b.volume) for b in bars]
    typical = [(b.high + b.low + b.close) / 3 for b in bars]
    session_high = max(b.high for b in bars)
    session_low = min(b.low for b in bars)
    session_range = session_high - session_low
    close = closes[-1]
    open_ = bars[0].open

    weights = volumes if sum(volumes) > 0 else [1.0] * len(bars)  # TWAP fallback
    total_weight = sum(weights)
    vwap = sum(t * w for t, w in zip(typical, weights, strict=True)) / total_weight
    sigma = math.sqrt(
        sum(w * (t - vwap) ** 2 for t, w in zip(typical, weights, strict=True))
        / total_weight
    )
    band_width = 4 * sigma / vwap * 100 if vwap > 0 else None
    band_position = (
        max(-4.0, min(4.0, (close - vwap) / sigma)) if sigma > 0 else None
    )

    or_bars = bars[:OPENING_RANGE_BARS]
    or_high, or_low = max(b.high for b in or_bars), min(b.low for b in or_bars)
    or_range = or_high - or_low
    or_pct = or_range / open_ * 100 if open_ > 0 else None
    or_position = (
        max(-5.0, min(5.0, (close - (or_high + or_low) / 2) / (or_range / 2)))
        if or_range > 0
        else None
    )

    ib_bars = bars[:INITIAL_BALANCE_BARS]
    ib_high, ib_low = max(b.high for b in ib_bars), min(b.low for b in ib_bars)
    ib_range = ib_high - ib_low
    ib_pct = ib_range / open_ * 100 if open_ > 0 else None
    ib_extension = session_range / ib_range if ib_range > 0 else None

    poc_distance = None
    value_area_width = None
    if session_range > 0:
        bin_width = session_range / PROFILE_BINS
        bins = [0.0] * PROFILE_BINS
        for c, w in zip(closes, weights, strict=True):
            index = min(int((c - session_low) / bin_width), PROFILE_BINS - 1)
            bins[index] += w
        poc_index = max(range(PROFILE_BINS), key=lambda b: bins[b])
        poc = session_low + (poc_index + 0.5) * bin_width
        target = VALUE_AREA * sum(bins)
        covered = bins[poc_index]
        low_index = high_index = poc_index
        while covered < target and (low_index > 0 or high_index < PROFILE_BINS - 1):
            below = bins[low_index - 1] if low_index > 0 else -1.0
            above = bins[high_index + 1] if high_index < PROFILE_BINS - 1 else -1.0
            if above >= below:
                high_index += 1
                covered += bins[high_index]
            else:
                low_index -= 1
                covered += bins[low_index]
        value_area_width = (high_index - low_index + 1) * bin_width / close * 100
        poc_distance = (close - poc) / poc * 100 if poc > 0 else None

    auction = (
        (close - (session_high + session_low) / 2) / (session_range / 2)
        if session_range > 0
        else None
    )

    return {
        "ms_vwap_band_width_pct": band_width,
        "ms_vwap_band_position": band_position,
        "ms_opening_range_pct": or_pct,
        "ms_opening_range_position": or_position,
        "ms_initial_balance_pct": ib_pct,
        "ms_ib_extension": ib_extension,
        "ms_value_area_width_pct": value_area_width,
        "ms_poc_distance_pct": poc_distance,
        "ms_auction_imbalance": auction,
    }


def compute_structure_sessions(
    intraday: Sequence[Candle],
    normalization_window: int = 100,
    min_session_bars: int = MIN_SESSION_BARS,
) -> tuple[list[datetime], dict[str, Series]]:
    """One value per complete session, timestamped at midnight IST."""
    sessions: dict[datetime, list[Candle]] = {}
    for candle in intraday:
        session_date = candle.ts.astimezone(IST).date()
        key = datetime(
            session_date.year, session_date.month, session_date.day, tzinfo=IST
        )
        sessions.setdefault(key, []).append(candle)

    timestamps: list[datetime] = []
    rows: list[dict[str, float | None]] = []
    for key in sorted(sessions):
        bars = sorted(sessions[key], key=lambda c: c.ts)
        if len(bars) < min_session_bars:
            continue
        timestamps.append(key)
        rows.append(_session_features(bars))

    names = [
        "ms_vwap_band_width_pct", "ms_vwap_band_position",
        "ms_opening_range_pct", "ms_opening_range_position",
        "ms_initial_balance_pct", "ms_ib_extension",
        "ms_value_area_width_pct", "ms_poc_distance_pct", "ms_auction_imbalance",
    ]
    series: dict[str, Series] = {
        name: [row.get(name) for row in rows] for name in names
    }
    return timestamps, add_normalized_series(series, normalization_window)


# --- Engine -------------------------------------------------------------------------

class MarketStructureEngine(BaseFeatureEngine):
    name = ENGINE_NAME
    category = CATEGORY

    def _definitions(self) -> list[FeatureDefinition]:
        return structure_feature_definitions(
            self._settings.feature_normalization_window,
            calculation_frequency=f"{self._settings.feature_engine_interval}s",
        )

    def _compute(
        self, candles: Sequence[Candle], benchmark: Sequence[Candle] | None = None
    ) -> dict[str, Series]:
        return compute_structure_daily(
            candles,
            self._settings.feature_structure_fractal,
            self._settings.feature_normalization_window,
        )

    async def run(
        self,
        symbol: str,
        timeframe: str = "D",
        full: bool = False,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict:
        """`start`/`end` (data foundation audit 2026-07-17, historical
        regeneration item) only date-range the primary OHLCV pass
        (`super().run()`) -- the intraday session pass loads its own fixed
        intraday-timeframe window, a separate scope this chunk doesn't
        extend (documented boundary, not an oversight)."""
        summary = await super().run(symbol, timeframe, full=full, start=start, end=end)
        summary["session_pass"] = await self._run_session_features(symbol, full=full)
        return summary

    async def _run_session_features(self, symbol: str, full: bool = False) -> dict:
        intraday = await self._load_candles(
            symbol, self._settings.feature_intraday_timeframe
        )
        if len(intraday) < MIN_SESSION_BARS:
            return {"timeframe": "D", "stored": 0, "skipped": True}
        timestamps, series = compute_structure_sessions(
            intraday, self._settings.feature_normalization_window
        )
        if not timestamps:
            return {"timeframe": "D", "stored": 0, "skipped": True}
        return await self._process_series(symbol, "D", timestamps, series, full=full)
