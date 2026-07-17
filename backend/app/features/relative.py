"""Relative Strength Feature Engine (Volume 3, Prompt 3.8).

Compares every watchlist stock against five references — NIFTY, SENSEX, its
sector index, its industry index, and its peer basket — on daily candles.

Reference conventions:
- Sector and industry indices come from feature_stock_sectors /
  feature_stock_industries config; industry falls back to the sector until a
  finer-grained industry index source exists. Reference candles are kept
  fresh by the reference_indices collector.
- Peers are the other watchlist equities; the peer reference is their
  equal-weight log-price basket, aligned bar by bar.

Feature conventions (per reference, per window, in % where noted):
- Relative Strength: change of ln(stock/reference) over the window x100 —
  the stock's cumulative out/under-performance in % points.
- Relative Momentum: least-squares slope of ln(stock/reference) over the
  window x100 — out-performance drift per bar.
- Relative Volatility: ratio of return standard deviations over the window
  (>1 = stock swings harder than the reference).
- Relative Volume (vs peers only): stock's average volume over the window
  vs the mean of peer averages.
- Percentile Rank (vs peers + self): percentile of the stock's window
  return within the group, 0-100.
- Outperformance Score: 0-100 composite: mean of tanh(strength/5) across
  available references, mapped onto 0-100 (50 = in line with references).

Every feature ships a look-ahead-safe rolling z-score companion (_z).
"""

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from statistics import fmean, pstdev

from app.core.config import get_settings
from app.core.logging import get_logger
from app.features.base import BaseFeatureEngine
from app.features.normalize import (
    add_normalized_series,
    normalized_definition,
    rolling_slope,
)
from app.features.schema import Candle, FeatureDefinition, Series
from app.market.instruments import INDEX_TOKENS

logger = get_logger(__name__)

ENGINE_NAME = "relative_strength_engine"
ENGINE_VERSION = "v1"
CATEGORY = "relative"

REFERENCES = ("nifty", "sensex", "sector", "industry", "peers")


# --- Feature definitions -------------------------------------------------------

def relative_feature_definitions(
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

    definitions: list[FeatureDefinition] = []
    for w in windows:
        for ref in REFERENCES:
            definitions.extend([
                define(f"rs_{ref}_strength_{w}",
                       f"Cumulative out-performance vs {ref} over {w} bars, "
                       "in % points.",
                       "%", (-50.0, 50.0), (), w),
                define(f"rs_{ref}_momentum_{w}",
                       f"Out-performance drift vs {ref}: slope of the relative "
                       f"log price over {w} bars, in % per bar.",
                       "%", (-5.0, 5.0), (f"rs_{ref}_strength_{w}",), w),
                define(f"rs_{ref}_volatility_{w}",
                       f"Return volatility ratio vs {ref} over {w} bars.",
                       "ratio", (0.0, 10.0), (), w),
            ])
        definitions.extend([
            define(f"rs_relative_volume_{w}",
                   f"Average volume over {w} bars vs the mean of peer averages.",
                   "ratio", (0.0, 20.0), (), w),
            define(f"rs_percentile_rank_{w}",
                   f"Percentile of the {w}-bar return within peers + self, 0-100.",
                   "index", (0.0, 100.0), (), w),
            define(f"rs_outperformance_{w}",
                   f"0-100 composite of strength across references over {w} bars "
                   "(50 = in line).",
                   "index", (0.0, 100.0),
                   tuple(f"rs_{ref}_strength_{w}" for ref in REFERENCES), w),
        ])
    definitions.extend(
        normalized_definition(d, normalization_window) for d in list(definitions)
    )
    return definitions


# --- Pure calculations -----------------------------------------------------------

def _log_close_map(candles: Sequence[Candle]) -> dict[datetime, float]:
    return {c.ts: math.log(c.close) for c in candles if c.close > 0}


def _aligned(series_map: dict[datetime, float], timestamps: Sequence[datetime]) -> Series:
    return [series_map.get(ts) for ts in timestamps]


def _returns(log_prices: Series) -> Series:
    out: Series = [None] * len(log_prices)
    for i in range(1, len(log_prices)):
        current, previous = log_prices[i], log_prices[i - 1]
        if current is not None and previous is not None:
            out[i] = current - previous
    return out


def _window_std(returns: Series, i: int, w: int) -> float | None:
    values = [r for r in returns[i - w + 1 : i + 1] if r is not None]
    if len(values) < w:
        return None
    std = pstdev(values)
    return std if std > 0 else None


def compute_relative_features(
    candles: Sequence[Candle],
    references: Mapping[str, Sequence[Candle]],
    peer_candles: Mapping[str, Sequence[Candle]],
    windows: Sequence[int] = (5, 10, 20, 50, 100, 200),
    normalization_window: int = 100,
) -> dict[str, Series]:
    """Compute every relative-strength feature aligned to the stock's candles.

    `references` maps reference name (nifty/sensex/sector/industry) to that
    index's candles; the peer basket is built from `peer_candles`. References
    without data simply produce empty series — never fabricated.
    """
    n = len(candles)
    timestamps = [c.ts for c in candles]
    stock_ln: Series = [math.log(c.close) if c.close > 0 else None for c in candles]
    stock_returns = _returns(stock_ln)
    stock_volumes = [float(c.volume) for c in candles]

    peer_ln_maps = {name: _log_close_map(pc) for name, pc in peer_candles.items()}
    peer_volume_maps = {
        name: {c.ts: float(c.volume) for c in pc} for name, pc in peer_candles.items()
    }

    # Reference log-price series aligned to the stock's bars. The peer basket
    # is the equal-weight mean of peer log prices per bar.
    ref_ln: dict[str, Series] = {}
    for ref in ("nifty", "sensex", "sector", "industry"):
        ref_series = references.get(ref)
        ref_ln[ref] = (
            _aligned(_log_close_map(ref_series), timestamps) if ref_series else [None] * n
        )
    basket: Series = [None] * n
    for i, ts in enumerate(timestamps):
        values = [m[ts] for m in peer_ln_maps.values() if ts in m]
        if values:
            basket[i] = fmean(values)
    ref_ln["peers"] = basket

    relative_ln: dict[str, Series] = {}
    ref_returns: dict[str, Series] = {}
    for ref, series in ref_ln.items():
        relative: Series = [None] * n
        for i in range(n):
            stock_value, ref_value = stock_ln[i], series[i]
            if stock_value is not None and ref_value is not None:
                relative[i] = stock_value - ref_value
        relative_ln[ref] = relative
        ref_returns[ref] = _returns(series)

    out: dict[str, Series] = {}
    for w in windows:
        strengths: dict[str, Series] = {}
        for ref in REFERENCES:
            relative = relative_ln[ref]
            strength: Series = [None] * n
            volatility: Series = [None] * n
            for i in range(w, n):
                current, base = relative[i], relative[i - w]
                if current is not None and base is not None:
                    strength[i] = (current - base) * 100
                stock_std = _window_std(stock_returns, i, w)
                ref_std = _window_std(ref_returns[ref], i, w)
                if stock_std is not None and ref_std is not None:
                    volatility[i] = stock_std / ref_std
            momentum = [
                v * 100 if v is not None else None
                for v in rolling_slope(relative, w)
            ]
            strengths[ref] = strength
            out[f"rs_{ref}_strength_{w}"] = strength
            out[f"rs_{ref}_momentum_{w}"] = momentum
            out[f"rs_{ref}_volatility_{w}"] = volatility

        relative_volume: Series = [None] * n
        percentile: Series = [None] * n
        outperformance: Series = [None] * n
        for i in range(w, n):
            window_volume = [v for v in stock_volumes[i - w + 1 : i + 1] if v > 0]
            if len(window_volume) == w:
                peer_averages = []
                for volume_map in peer_volume_maps.values():
                    peer_window = [
                        volume_map[ts]
                        for ts in timestamps[i - w + 1 : i + 1]
                        if volume_map.get(ts, 0) > 0
                    ]
                    if len(peer_window) == w:
                        peer_averages.append(fmean(peer_window))
                if peer_averages:
                    relative_volume[i] = fmean(window_volume) / fmean(peer_averages)

            stock_now, stock_base = stock_ln[i], stock_ln[i - w]
            if stock_now is not None and stock_base is not None:
                stock_return = stock_now - stock_base
                group = [stock_return]
                for ln_map in peer_ln_maps.values():
                    now, base = ln_map.get(timestamps[i]), ln_map.get(timestamps[i - w])
                    if now is not None and base is not None:
                        group.append(now - base)
                if len(group) >= 2:
                    percentile[i] = (
                        sum(1 for r in group if r <= stock_return) / len(group) * 100
                    )

            ref_strengths = [
                s for ref in REFERENCES if (s := strengths[ref][i]) is not None
            ]
            if ref_strengths:
                outperformance[i] = 50 * (
                    1 + fmean([math.tanh(s / 5) for s in ref_strengths])
                )

        out[f"rs_relative_volume_{w}"] = relative_volume
        out[f"rs_percentile_rank_{w}"] = percentile
        out[f"rs_outperformance_{w}"] = outperformance

    return add_normalized_series(out, normalization_window)


# --- Engine -------------------------------------------------------------------------

class RelativeStrengthEngine(BaseFeatureEngine):
    name = ENGINE_NAME
    category = CATEGORY

    def _definitions(self) -> list[FeatureDefinition]:
        return relative_feature_definitions(
            self.windows,
            self._settings.feature_normalization_window,
            calculation_frequency=f"{self._settings.feature_engine_interval}s",
        )

    def _compute(
        self, candles: Sequence[Candle], benchmark: Sequence[Candle] | None = None
    ) -> dict[str, Series]:
        return {}  # run() assembles multi-reference inputs itself

    @staticmethod
    def equity_watchlist() -> list[str]:
        return [
            s for s in get_settings().watchlist if s.upper() not in INDEX_TOKENS
        ]

    async def run(
        self,
        symbol: str,
        timeframe: str = "D",
        full: bool = False,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict:
        """`start`/`end` (data foundation audit 2026-07-17, historical
        regeneration item): date-ranges the target symbol's own candles AND
        every reference/peer series it's compared against, so a
        regenerated window stays internally consistent (not, e.g., a
        ranged target symbol compared against a peer's full unranged
        history)."""
        candles = await self._load_candles(symbol, timeframe, start=start, end=end)
        if start is not None or end is not None:
            full = True
        if len(candles) < 2:
            return {"symbol": symbol, "timeframe": timeframe, "stored": 0, "skipped": True}

        settings = self._settings
        sector = settings.feature_stock_sectors.get(symbol)
        industry = settings.feature_stock_industries.get(symbol, sector)
        references: dict[str, Sequence[Candle]] = {}
        for ref, ref_symbol in (
            ("nifty", settings.feature_benchmark_symbol),
            ("sensex", settings.feature_sensex_symbol),
            ("sector", sector),
            ("industry", industry),
        ):
            if ref_symbol:
                ref_candles = await self._load_candles(ref_symbol, timeframe, start=start, end=end)
                if ref_candles:
                    references[ref] = ref_candles

        peer_candles: dict[str, Sequence[Candle]] = {}
        for peer in self.equity_watchlist():
            if peer == symbol:
                continue
            candles_for_peer = await self._load_candles(peer, timeframe, start=start, end=end)
            if candles_for_peer:
                peer_candles[peer] = candles_for_peer

        series = compute_relative_features(
            candles,
            references,
            peer_candles,
            self.windows,
            settings.feature_normalization_window,
        )
        return await self._process_series(
            symbol, timeframe, [c.ts for c in candles], series, full=full
        )

    async def run_all(
        self,
        full: bool = False,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict]:
        """Relative strength applies to equities only — indices are references."""
        results: list[dict] = []
        for timeframe in self._settings.feature_timeframes:
            for symbol in self.equity_watchlist():
                try:
                    results.append(
                        await self.run(symbol, timeframe, full=full, start=start, end=end)
                    )
                except Exception as exc:
                    logger.error(
                        "relative strength run failed",
                        extra={"symbol": symbol, "error": str(exc)},
                    )
                    results.append({"symbol": symbol, "error": str(exc)})
        return results
