"""Price Feature Engine (Volume 3, Prompt 3.1).

Transforms OHLCV candles into the 17 price features of Chapter 9, across the
rolling windows 5/10/20/50/100/200. Every feature is registered with Chapter 5
metadata, stored independently in the Feature Store, and stamped with a
version so calculations stay reproducible (Chapter 6).

Feature conventions:
- Returns are ratios; everything suffixed _pct / distance / momentum is in %.
- Daily Range % measures bar extension vs the prior close; Intraday Range %
  measures open-to-close movement within the bar.
- ATR is the simple mean of True Range over the window.
- Beta/Alpha/Correlation regress the symbol's simple returns on the benchmark's
  (alpha is the per-bar abnormal return in %); the benchmark symbol itself
  skips these three features.
"""

import math
from collections.abc import Sequence
from datetime import datetime
from statistics import fmean

from app.features.base import BaseFeatureEngine
from app.features.normalize import add_normalized_series, normalized_definition
from app.features.schema import Candle, FeatureDefinition, Series

ENGINE_NAME = "price_feature_engine"
ENGINE_VERSION = "v1"
CATEGORY = "price"


# --- Feature definitions -------------------------------------------------------

def price_feature_definitions(
    windows: Sequence[int],
    benchmark_symbol: str,
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
        define("price_log_return", "Log return of close vs prior close.",
               "ratio", (-1.0, 1.0)),
        define("price_simple_return", "Simple return of close vs prior close.",
               "ratio", (-1.0, 1.0)),
        define("price_gap_pct", "Open gap vs prior close, in %.", "%", (-25.0, 25.0)),
        define("price_true_range", "True range: max of bar range and gaps vs prior close.",
               "price", (0.0, None)),
        define("price_daily_range_pct", "High-low bar range as % of prior close.",
               "%", (0.0, 50.0)),
        define("price_intraday_range_pct", "Open-to-close move within the bar, in %.",
               "%", (-50.0, 50.0)),
    ]
    for w in windows:
        definitions.extend([
            define(f"price_atr_{w}", f"Average True Range over {w} bars.",
                   "price", (0.0, None), ("price_true_range",), w),
            define(f"price_rolling_high_{w}", f"Highest high over {w} bars.",
                   "price", (0.0, None), (), w),
            define(f"price_rolling_low_{w}", f"Lowest low over {w} bars.",
                   "price", (0.0, None), (), w),
            define(f"price_dist_from_high_{w}",
                   f"Close distance from the {w}-bar rolling high, in % (<= 0).",
                   "%", (-100.0, 0.001), (f"price_rolling_high_{w}",), w),
            define(f"price_dist_from_low_{w}",
                   f"Close distance from the {w}-bar rolling low, in % (>= 0).",
                   "%", (-0.001, 500.0), (f"price_rolling_low_{w}",), w),
            define(f"price_vwap_distance_{w}",
                   f"Close distance from the {w}-bar rolling VWAP, in %.",
                   "%", (-50.0, 50.0), (), w),
            define(f"price_momentum_{w}", f"Close change over {w} bars, in %.",
                   "%", (-95.0, 500.0), (), w),
            define(f"price_acceleration_{w}",
                   f"Bar-over-bar change of {w}-bar momentum, in % points.",
                   "%", (-200.0, 200.0), (f"price_momentum_{w}",), w),
            define(f"price_beta_{w}",
                   f"Rolling beta of simple returns vs {benchmark_symbol} over {w} bars.",
                   "ratio", (-5.0, 5.0), ("price_simple_return",), w),
            define(f"price_alpha_{w}",
                   f"Rolling per-bar alpha vs {benchmark_symbol} over {w} bars, in %.",
                   "%", (-10.0, 10.0), (f"price_beta_{w}",), w),
            define(f"price_correlation_{w}",
                   f"Rolling correlation of simple returns vs {benchmark_symbol} "
                   f"over {w} bars.",
                   "ratio", (-1.0, 1.0), ("price_simple_return",), w),
        ])
    # Prompt 3.13: normalize every feature — each raw feature gets a _z
    # companion (the same contract volume.py/breadth.py/etc. already follow;
    # this engine previously stored only raw values, feeding the ML ensemble
    # unnormalized price_* features).
    definitions.extend(
        normalized_definition(d, normalization_window) for d in list(definitions)
    )
    return definitions


# --- Pure calculations -----------------------------------------------------------

def compute_price_features(
    candles: Sequence[Candle],
    benchmark: Sequence[Candle] | None = None,
    windows: Sequence[int] = (5, 10, 20, 50, 100, 200),
    normalization_window: int = 100,
) -> dict[str, Series]:
    """Compute every price feature as a series aligned to `candles`.

    Cold-start bars (not enough history for a window) hold None and are never
    emitted or stored.
    """
    n = len(candles)
    opens = [c.open for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]
    typical = [(h + low + c) / 3 for h, low, c in zip(highs, lows, closes, strict=True)]

    log_return: Series = [None] * n
    simple_return: Series = [None] * n
    gap_pct: Series = [None] * n
    true_range: Series = [None] * n
    daily_range_pct: Series = [None] * n
    intraday_range_pct: Series = [None] * n

    for i in range(n):
        if i > 0 and closes[i - 1] > 0:
            prev_close = closes[i - 1]
            simple_return[i] = closes[i] / prev_close - 1
            if closes[i] > 0:
                log_return[i] = math.log(closes[i] / prev_close)
            gap_pct[i] = (opens[i] - prev_close) / prev_close * 100
            true_range[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - prev_close),
                abs(lows[i] - prev_close),
            )
            daily_range_pct[i] = (highs[i] - lows[i]) / prev_close * 100
        if opens[i] > 0:
            intraday_range_pct[i] = (closes[i] - opens[i]) / opens[i] * 100

    out: dict[str, Series] = {
        "price_log_return": log_return,
        "price_simple_return": simple_return,
        "price_gap_pct": gap_pct,
        "price_true_range": true_range,
        "price_daily_range_pct": daily_range_pct,
        "price_intraday_range_pct": intraday_range_pct,
    }

    bench_return: Series | None = None
    if benchmark:
        by_ts: dict[datetime, float] = {}
        prev: float | None = None
        for candle in benchmark:
            if prev is not None and prev > 0:
                by_ts[candle.ts] = candle.close / prev - 1
            prev = candle.close
        bench_return = [by_ts.get(c.ts) for c in candles]

    for w in windows:
        rolling_high: Series = [None] * n
        rolling_low: Series = [None] * n
        dist_high: Series = [None] * n
        dist_low: Series = [None] * n
        vwap_distance: Series = [None] * n
        atr: Series = [None] * n
        momentum: Series = [None] * n
        acceleration: Series = [None] * n

        for i in range(n):
            if i >= w - 1:
                window_high = max(highs[i - w + 1 : i + 1])
                window_low = min(lows[i - w + 1 : i + 1])
                rolling_high[i] = window_high
                rolling_low[i] = window_low
                if window_high > 0:
                    dist_high[i] = (closes[i] - window_high) / window_high * 100
                if window_low > 0:
                    dist_low[i] = (closes[i] - window_low) / window_low * 100
                volume_sum = sum(volumes[i - w + 1 : i + 1])
                if volume_sum > 0:
                    vwap = (
                        sum(typical[j] * volumes[j] for j in range(i - w + 1, i + 1))
                        / volume_sum
                    )
                    if vwap > 0:
                        vwap_distance[i] = (closes[i] - vwap) / vwap * 100
            if i >= w:
                trs = [t for t in true_range[i - w + 1 : i + 1] if t is not None]
                if len(trs) == w:
                    atr[i] = sum(trs) / w
                if closes[i - w] > 0:
                    momentum[i] = (closes[i] / closes[i - w] - 1) * 100
        for i in range(1, n):
            current, previous = momentum[i], momentum[i - 1]
            if current is not None and previous is not None:
                acceleration[i] = current - previous

        out[f"price_atr_{w}"] = atr
        out[f"price_rolling_high_{w}"] = rolling_high
        out[f"price_rolling_low_{w}"] = rolling_low
        out[f"price_dist_from_high_{w}"] = dist_high
        out[f"price_dist_from_low_{w}"] = dist_low
        out[f"price_vwap_distance_{w}"] = vwap_distance
        out[f"price_momentum_{w}"] = momentum
        out[f"price_acceleration_{w}"] = acceleration

        if bench_return is not None:
            beta, alpha, correlation = _rolling_regression(simple_return, bench_return, w)
            out[f"price_beta_{w}"] = beta
            out[f"price_alpha_{w}"] = alpha
            out[f"price_correlation_{w}"] = correlation

    return add_normalized_series(out, normalization_window)


def _rolling_regression(
    returns: Series, bench_returns: Series, w: int
) -> tuple[Series, Series, Series]:
    n = len(returns)
    beta: Series = [None] * n
    alpha: Series = [None] * n
    correlation: Series = [None] * n
    # Real feeds have holes; require most of the window rather than all of it.
    min_obs = max(3, w // 2)
    for i in range(w, n):
        pairs: list[tuple[float, float]] = []
        for j in range(i - w + 1, i + 1):
            stock_return, bench_return = returns[j], bench_returns[j]
            if stock_return is not None and bench_return is not None:
                pairs.append((stock_return, bench_return))
        if len(pairs) < min_obs:
            continue
        ys = [p[0] for p in pairs]
        xs = [p[1] for p in pairs]
        mean_y = fmean(ys)
        mean_x = fmean(xs)
        var_x = fmean([(x - mean_x) ** 2 for x in xs])
        if var_x <= 0:
            continue
        cov = fmean([(x - mean_x) * (y - mean_y) for y, x in pairs])
        beta_value = cov / var_x
        beta[i] = beta_value
        alpha[i] = (mean_y - beta_value * mean_x) * 100
        var_y = fmean([(y - mean_y) ** 2 for y in ys])
        if var_y > 0:
            correlation[i] = cov / math.sqrt(var_x * var_y)
    return beta, alpha, correlation


# --- Engine -------------------------------------------------------------------------

class PriceFeatureEngine(BaseFeatureEngine):
    name = ENGINE_NAME
    category = CATEGORY
    uses_benchmark = True

    def _definitions(self) -> list[FeatureDefinition]:
        return price_feature_definitions(
            self.windows,
            self.benchmark_symbol,
            self._settings.feature_normalization_window,
            calculation_frequency=f"{self._settings.feature_engine_interval}s",
        )

    def _compute(
        self, candles: Sequence[Candle], benchmark: Sequence[Candle] | None = None
    ) -> dict[str, Series]:
        return compute_price_features(
            candles, benchmark, self.windows, self._settings.feature_normalization_window
        )
