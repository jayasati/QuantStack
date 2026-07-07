"""Liquidity Feature Engine (Volume 3, Prompt 3.4).

Two time bases feed this engine:

- Bar features come from OHLCV candles and are stored under the run's
  timeframe: Turnover (close x volume) and the Delivery % placeholder.
- Microstructure features come from the live quote snapshots the live_market
  collector persists into market_events (bid/ask/depth), and are stored under
  the synthetic timeframe "quote": spreads, order book depth/imbalance,
  market impact, liquidity score and trend.

Conventions:
- Order Book Imbalance is (bid_depth - ask_depth)/(bid_depth + ask_depth) in
  -1..1. Depth prefers 5-level order book totals, falls back to top-of-book
  quantities, then to the session's total buy/sell quantities (websocket
  ticks carry only the totals).
- Market Impact Estimate is a v1 heuristic in % of mid: half the spread
  scaled up by the participation of a configurable reference order size in
  the visible depth. A calibrated impact model can ship as v2.
- Liquidity Score is a 0-100 composite: 40% spread tightness (1% spread
  scores zero), 30% depth (trailing percentile), 30% impact (0.5% impact
  scores zero). Liquidity Trend is the least-squares slope of the score over
  the window, in points per snapshot.
- Delivery % joins the nse_delivery collector's observations by session date
  (one value per trading day, midnight-IST timestamps), stored under the
  daily timeframe.
- Index symbols quote without bid/ask, depth, or volume, so liquidity
  features stay empty for them by design; they populate for tradeable
  instruments (stocks, futures).

Every feature ships a look-ahead-safe rolling z-score companion (_z).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import fmean

from sqlalchemy import desc, select

from app.features.base import BaseFeatureEngine
from app.features.normalize import (
    add_normalized_series,
    normalized_definition,
    trailing_percentile,
)
from app.features.schema import Candle, FeatureDefinition, Series

ENGINE_NAME = "liquidity_feature_engine"
ENGINE_VERSION = "v1"
CATEGORY = "liquidity"

QUOTE_TIMEFRAME = "quote"

# Score calibration: spreads at or above this % of mid score zero tightness,
# impacts at or above this % score zero. v1 constants, documented above.
SPREAD_SCORE_CEILING_PCT = 1.0
IMPACT_SCORE_CEILING_PCT = 0.5


@dataclass(frozen=True)
class QuoteSnapshot:
    """One live quote observation, depth pre-resolved with fallbacks."""

    ts: datetime
    bid: float | None = None
    ask: float | None = None
    bid_depth: float | None = None
    ask_depth: float | None = None


def parse_quote_event(data: dict) -> QuoteSnapshot | None:
    """Build a QuoteSnapshot from a persisted live_market observation."""
    ts_raw = data.get("timestamp")
    if not ts_raw:
        return None
    try:
        ts = datetime.fromisoformat(ts_raw)
    except (TypeError, ValueError):
        return None
    meta = data.get("metadata") or {}

    def depth_total(side: str, top_of_book: str, session_total: str) -> float | None:
        levels = (meta.get("depth") or {}).get(side) or []
        total = sum(level.get("quantity") or 0 for level in levels if isinstance(level, dict))
        if total > 0:
            return float(total)
        for key in (top_of_book, session_total):
            value = meta.get(key)
            if value:
                return float(value)
        return None

    return QuoteSnapshot(
        ts=ts,
        bid=meta.get("bid"),
        ask=meta.get("ask"),
        bid_depth=depth_total("buy", "bid_qty", "total_buy_qty"),
        ask_depth=depth_total("sell", "ask_qty", "total_sell_qty"),
    )


# --- Feature definitions -------------------------------------------------------

def liquidity_feature_definitions(
    windows: Sequence[int],
    normalization_window: int,
    calculation_frequency: str = "on_schedule",
) -> list[FeatureDefinition]:
    def define(name: str, description: str, unit: str,
               expected: tuple[float | None, float | None],
               dependencies: tuple[str, ...] = (), window: int | None = None,
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
            window=window,
        )

    definitions = [
        # Quote time base ("quote" timeframe).
        define("liquidity_current_spread", "Ask minus bid of the snapshot.",
               "price", (0.0, None)),
        define("liquidity_spread_pct", "Spread as % of the mid price.",
               "%", (0.0, 10.0), ("liquidity_current_spread",)),
        define("liquidity_order_book_imbalance",
               "(bid_depth - ask_depth)/(bid_depth + ask_depth), -1..1.",
               "ratio", (-1.0, 1.0)),
        define("liquidity_bid_depth", "Visible buy-side quantity.", "qty", (0.0, None)),
        define("liquidity_ask_depth", "Visible sell-side quantity.", "qty", (0.0, None)),
        define("liquidity_market_impact_pct",
               "Estimated cost of a reference order in % of mid: half-spread "
               "scaled by depth participation.",
               "%", (0.0, 10.0), ("liquidity_spread_pct",)),
        define("liquidity_score",
               "0-100 composite: 40% spread tightness, 30% depth percentile, "
               "30% impact.",
               "index", (0.0, 100.0),
               ("liquidity_spread_pct", "liquidity_market_impact_pct")),
        # Bar time base (run timeframe).
        define("liquidity_turnover", "Close x volume of the bar.",
               "currency", (0.0, None)),
        define("liquidity_delivery_pct",
               "Delivered vs traded quantity, in % (nse_delivery collector, "
               "one observation per session).",
               "%", (0.0, 100.0)),
    ]
    for w in windows:
        definitions.extend([
            define(f"liquidity_avg_spread_{w}",
                   f"Mean spread over {w} quote snapshots.",
                   "price", (0.0, None), ("liquidity_current_spread",), w),
            define(f"liquidity_trend_{w}",
                   f"Least-squares slope of the liquidity score over {w} snapshots, "
                   "points per snapshot.",
                   "points", (-100.0, 100.0), ("liquidity_score",), w),
        ])
    definitions.extend(
        normalized_definition(d, normalization_window) for d in list(definitions)
    )
    return definitions


# --- Pure calculations -----------------------------------------------------------

def compute_liquidity_quote_features(
    quotes: Sequence[QuoteSnapshot],
    windows: Sequence[int] = (5, 10, 20, 50, 100, 200),
    normalization_window: int = 100,
    reference_order_qty: int = 1000,
) -> dict[str, Series]:
    """Compute quote-based liquidity features aligned to `quotes`."""
    n = len(quotes)
    spread: Series = [None] * n
    spread_pct: Series = [None] * n
    imbalance: Series = [None] * n
    bid_depth: Series = [None] * n
    ask_depth: Series = [None] * n
    impact: Series = [None] * n
    total_depth: Series = [None] * n

    for i, quote in enumerate(quotes):
        depth_sum: float | None = None
        if quote.bid_depth is not None:
            bid_depth[i] = quote.bid_depth
        if quote.ask_depth is not None:
            ask_depth[i] = quote.ask_depth
        if quote.bid_depth is not None and quote.ask_depth is not None:
            candidate = quote.bid_depth + quote.ask_depth
            if candidate > 0:
                imbalance[i] = (quote.bid_depth - quote.ask_depth) / candidate
                depth_sum = candidate
                total_depth[i] = candidate
        if quote.bid is not None and quote.ask is not None and 0 < quote.bid <= quote.ask:
            spread_value = quote.ask - quote.bid
            spread[i] = spread_value
            mid = (quote.ask + quote.bid) / 2
            pct = spread_value / mid * 100
            spread_pct[i] = pct
            if depth_sum is not None:
                impact[i] = pct / 2 * (1 + reference_order_qty / depth_sum)

    min_obs = max(10, normalization_window // 10)
    score: Series = [None] * n
    for i in range(n):
        pct_value, impact_value = spread_pct[i], impact[i]
        if pct_value is None or impact_value is None:
            continue
        depth_pctl = trailing_percentile(total_depth, i, normalization_window, min_obs)
        if depth_pctl is None:
            continue
        spread_component = max(0.0, 1 - pct_value / SPREAD_SCORE_CEILING_PCT) * 100
        impact_component = max(0.0, 1 - impact_value / IMPACT_SCORE_CEILING_PCT) * 100
        score[i] = 0.4 * spread_component + 0.3 * depth_pctl * 100 + 0.3 * impact_component

    out: dict[str, Series] = {
        "liquidity_current_spread": spread,
        "liquidity_spread_pct": spread_pct,
        "liquidity_order_book_imbalance": imbalance,
        "liquidity_bid_depth": bid_depth,
        "liquidity_ask_depth": ask_depth,
        "liquidity_market_impact_pct": impact,
        "liquidity_score": score,
    }

    for w in windows:
        avg_spread: Series = [None] * n
        trend: Series = [None] * n
        mean_t = (w - 1) / 2
        var_t = fmean([(t - mean_t) ** 2 for t in range(w)]) if w > 1 else 0.0
        for i in range(w - 1, n):
            spreads = [s for s in spread[i - w + 1 : i + 1] if s is not None]
            if len(spreads) == w:
                avg_spread[i] = fmean(spreads)
            scores = [s for s in score[i - w + 1 : i + 1] if s is not None]
            if len(scores) == w and var_t > 0:
                mean_s = fmean(scores)
                trend[i] = fmean(
                    [(t - mean_t) * (s - mean_s) for t, s in enumerate(scores)]
                ) / var_t
        out[f"liquidity_avg_spread_{w}"] = avg_spread
        out[f"liquidity_trend_{w}"] = trend

    return add_normalized_series(out, normalization_window)


def compute_liquidity_candle_features(
    candles: Sequence[Candle],
    normalization_window: int = 100,
) -> dict[str, Series]:
    """Compute bar-based liquidity features aligned to `candles`."""
    n = len(candles)
    turnover: Series = [None] * n
    for i, candle in enumerate(candles):
        if candle.volume > 0 and candle.close > 0:
            turnover[i] = candle.close * candle.volume
    return add_normalized_series({"liquidity_turnover": turnover}, normalization_window)


def delivery_series(
    observations: Sequence[tuple[datetime, float]],
    normalization_window: int = 100,
) -> tuple[list[datetime], dict[str, Series]]:
    """Deduped, time-ordered delivery observations -> feature series (+ _z).

    Multiple observations for the same session (intraday provisional then EOD
    final) collapse to the last one seen.
    """
    latest: dict[datetime, float] = {}
    for ts, pct in observations:
        latest[ts] = pct
    timestamps = sorted(latest)
    values: Series = [latest[ts] for ts in timestamps]
    series = add_normalized_series(
        {"liquidity_delivery_pct": values}, normalization_window
    )
    return timestamps, series


# --- Engine -------------------------------------------------------------------------

class LiquidityFeatureEngine(BaseFeatureEngine):
    name = ENGINE_NAME
    category = CATEGORY

    def _definitions(self) -> list[FeatureDefinition]:
        return liquidity_feature_definitions(
            self.windows,
            self._settings.feature_normalization_window,
            calculation_frequency=f"{self._settings.feature_engine_interval}s",
        )

    def _compute(
        self, candles: Sequence[Candle], benchmark: Sequence[Candle] | None = None
    ) -> dict[str, Series]:
        return compute_liquidity_candle_features(
            candles, self._settings.feature_normalization_window
        )

    async def run(self, symbol: str, timeframe: str = "D", full: bool = False) -> dict:
        summary = await super().run(symbol, timeframe, full=full)
        summary["quote_pass"] = await self._run_quote_features(symbol, full=full)
        summary["delivery_pass"] = await self._run_delivery_features(symbol, full=full)
        return summary

    async def _run_delivery_features(self, symbol: str, full: bool = False) -> dict:
        observations = await self._load_delivery(symbol)
        if not observations:
            return {"timeframe": "D", "stored": 0, "skipped": True}
        timestamps, series = delivery_series(
            observations, self._settings.feature_normalization_window
        )
        return await self._process_series(symbol, "D", timestamps, series, full=full)

    async def _load_delivery(self, symbol: str) -> list[tuple[datetime, float]]:
        """Delivery observations from the nse_delivery collector, session-dated."""
        if self._sessions is None:
            return []
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            result = await session.execute(
                select(MarketEvent.data)
                .where(
                    MarketEvent.event_type == "market_data.observation",
                    MarketEvent.source == "nse_delivery",
                    MarketEvent.data["instrument"].astext == symbol,
                )
                .order_by(MarketEvent.id)
                .limit(2000)
            )
            rows = result.scalars().all()
        observations: list[tuple[datetime, float]] = []
        for data in rows:
            meta = (data or {}).get("metadata") or {}
            pct = meta.get("delivery_pct")
            if pct is None:
                continue
            ts_raw = meta.get("position_date") or data.get("timestamp")
            try:
                ts = datetime.fromisoformat(ts_raw)
            except (TypeError, ValueError):
                continue
            observations.append((ts, float(pct)))
        return observations

    async def _run_quote_features(self, symbol: str, full: bool = False) -> dict:
        quotes = await self._load_quotes(symbol)
        if len(quotes) < 2:
            return {"timeframe": QUOTE_TIMEFRAME, "stored": 0, "skipped": True}
        series = compute_liquidity_quote_features(
            quotes,
            self.windows,
            self._settings.feature_normalization_window,
            self._settings.feature_reference_order_qty,
        )
        return await self._process_series(
            symbol, QUOTE_TIMEFRAME, [q.ts for q in quotes], series, full=full
        )

    async def _load_quotes(self, symbol: str) -> list[QuoteSnapshot]:
        if self._sessions is None:
            return []
        from app.database.tables import MarketEvent

        lookback = self._settings.feature_quote_lookback
        async with self._sessions() as session:
            result = await session.execute(
                select(MarketEvent.data)
                .where(
                    MarketEvent.event_type == "market_data.observation",
                    MarketEvent.source == "live_market",
                    MarketEvent.data["instrument"].astext == symbol,
                )
                .order_by(desc(MarketEvent.id))
                .limit(lookback)
            )
            rows = result.scalars().all()
        quotes = [parse_quote_event(data) for data in reversed(rows) if data]
        return [q for q in quotes if q is not None]
