"""Volatility Feature Engine (Volume 3, Prompt 3.3).

Transforms OHLCV candles into the 10 volatility features of Chapter 11 and
ships every feature with a standardized z-score companion (_z suffix,
look-ahead safe), as the prompt requires.

Feature conventions:
- Historical Volatility is the demeaned std of log returns; Realized
  Volatility is the root mean of squared log returns; both annualized with
  sqrt(252) and expressed in %. Rolling Volatility is the un-annualized
  window std of simple returns in %.
- Volatility of Volatility is the window std of the Rolling Volatility
  series, so it needs 2w bars of history.
- ATR here is expressed as % of close (the price engine stores the absolute
  ATR), keeping the two engines complementary rather than duplicated.
- Volatility Regime ranks the current Rolling Volatility inside the trailing
  normalization window: 0 = low (bottom tercile), 1 = normal, 2 = high.
  Volatility Compression is 1 minus that percentile (1 = tightest squeeze),
  and Expansion Probability is a calibratable v1 heuristic mapped linearly
  from compression (0.1 .. 0.9) — a learned model can ship as v2 without
  breaking consumers, which is exactly what feature versioning is for.
- VIX Distance is realized-minus-implied: Historical Volatility minus the
  India VIX close at the same timestamp, in vol points. When no VIX candles
  exist in ohlcv_candles the feature simply stays empty.
- Expected Move is close x HV x sqrt(w/252): the +/- price move implied by
  current volatility over the next w bars.
"""

import math
from collections.abc import Sequence
from statistics import fmean, pstdev

from app.features.base import BaseFeatureEngine
from app.features.normalize import (
    add_normalized_series,
    normalized_definition,
    trailing_percentile,
)
from app.features.schema import Candle, FeatureDefinition, Series

ENGINE_NAME = "volatility_feature_engine"
ENGINE_VERSION = "v1"
CATEGORY = "volatility"

TRADING_DAYS = 252


# --- Feature definitions -------------------------------------------------------

def volatility_feature_definitions(
    windows: Sequence[int],
    normalization_window: int,
    vix_symbol: str,
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
        definitions.extend([
            define(f"volatility_hist_{w}",
                   f"Annualized std of log returns over {w} bars, in %.",
                   "%", (0.0, 200.0), (), w),
            define(f"volatility_realized_{w}",
                   f"Annualized root mean squared log return over {w} bars, in %.",
                   "%", (0.0, 200.0), (), w),
            define(f"volatility_rolling_{w}",
                   f"Window std of simple returns over {w} bars, in % (not annualized).",
                   "%", (0.0, 50.0), (), w),
            define(f"volatility_of_volatility_{w}",
                   f"Window std of the {w}-bar rolling volatility series.",
                   "%", (0.0, 50.0), (f"volatility_rolling_{w}",), w),
            define(f"volatility_atr_pct_{w}",
                   f"Average True Range over {w} bars as % of close.",
                   "%", (0.0, 50.0), (f"price_atr_{w}",), w),
            define(f"volatility_regime_{w}",
                   f"Tercile regime of {w}-bar rolling volatility within the trailing "
                   f"{normalization_window} bars: 0 low, 1 normal, 2 high.",
                   "regime", (0.0, 2.0), (f"volatility_rolling_{w}",), w),
            define(f"volatility_vix_distance_{w}",
                   f"{w}-bar historical volatility minus the {vix_symbol} close, "
                   "in vol points (realized minus implied).",
                   "volpts", (-100.0, 100.0), (f"volatility_hist_{w}",), w),
            define(f"volatility_expected_move_{w}",
                   f"Price move implied by {w}-bar historical volatility over the "
                   f"next {w} bars: close x HV x sqrt(w/252).",
                   "price", (0.0, None), (f"volatility_hist_{w}",), w),
            define(f"volatility_compression_{w}",
                   f"1 minus the percentile of {w}-bar rolling volatility within the "
                   f"trailing {normalization_window} bars (1 = tightest squeeze).",
                   "ratio", (0.0, 1.0), (f"volatility_rolling_{w}",), w),
            define(f"volatility_expansion_prob_{w}",
                   "Heuristic probability of volatility expansion, mapped linearly "
                   "from compression (0.1 .. 0.9).",
                   "probability", (0.0, 1.0), (f"volatility_compression_{w}",), w),
        ])
    # Prompt 3.3: generate standardized z-scores for every feature.
    definitions.extend(
        normalized_definition(d, normalization_window) for d in list(definitions)
    )
    return definitions


# --- Pure calculations -----------------------------------------------------------

def compute_volatility_features(
    candles: Sequence[Candle],
    vix: Sequence[Candle] | None = None,
    windows: Sequence[int] = (5, 10, 20, 50, 100, 200),
    normalization_window: int = 100,
) -> dict[str, Series]:
    """Compute every volatility feature (raw + _z normalized) aligned to `candles`."""
    n = len(candles)
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]

    log_return: Series = [None] * n
    simple_return: Series = [None] * n
    true_range: Series = [None] * n
    for i in range(1, n):
        prev_close = closes[i - 1]
        if prev_close > 0:
            simple_return[i] = closes[i] / prev_close - 1
            if closes[i] > 0:
                log_return[i] = math.log(closes[i] / prev_close)
            true_range[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - prev_close),
                abs(lows[i] - prev_close),
            )

    vix_close: Series | None = None
    if vix:
        by_ts = {c.ts: c.close for c in vix}
        vix_close = [by_ts.get(c.ts) for c in candles]

    min_obs = max(10, normalization_window // 10)
    out: dict[str, Series] = {}
    for w in windows:
        hist: Series = [None] * n
        realized: Series = [None] * n
        rolling: Series = [None] * n
        vol_of_vol: Series = [None] * n
        atr_pct: Series = [None] * n
        regime: Series = [None] * n
        vix_distance: Series = [None] * n
        expected_move: Series = [None] * n
        compression: Series = [None] * n
        expansion_prob: Series = [None] * n

        annualize = math.sqrt(TRADING_DAYS)
        for i in range(w, n):
            log_rets = [r for r in log_return[i - w + 1 : i + 1] if r is not None]
            if len(log_rets) == w:
                hist[i] = pstdev(log_rets) * annualize * 100
                realized[i] = math.sqrt(fmean([r * r for r in log_rets]) * TRADING_DAYS) * 100
            simple_rets = [r for r in simple_return[i - w + 1 : i + 1] if r is not None]
            if len(simple_rets) == w:
                rolling[i] = pstdev(simple_rets) * 100
            trs = [t for t in true_range[i - w + 1 : i + 1] if t is not None]
            if len(trs) == w and closes[i] > 0:
                atr_pct[i] = sum(trs) / w / closes[i] * 100

            hist_value = hist[i]
            if hist_value is not None:
                expected_move[i] = closes[i] * hist_value / 100 * math.sqrt(w / TRADING_DAYS)
                vix_value = vix_close[i] if vix_close is not None else None
                if vix_value is not None:
                    vix_distance[i] = hist_value - vix_value

        for i in range(w, n):
            vols = [v for v in rolling[i - w + 1 : i + 1] if v is not None]
            if len(vols) == w:
                vol_of_vol[i] = pstdev(vols)
            percentile = trailing_percentile(rolling, i, normalization_window, min_obs)
            if percentile is not None:
                regime[i] = 0.0 if percentile < 1 / 3 else (1.0 if percentile < 2 / 3 else 2.0)
                squeeze = 1 - percentile
                compression[i] = squeeze
                expansion_prob[i] = 0.1 + 0.8 * squeeze

        out[f"volatility_hist_{w}"] = hist
        out[f"volatility_realized_{w}"] = realized
        out[f"volatility_rolling_{w}"] = rolling
        out[f"volatility_of_volatility_{w}"] = vol_of_vol
        out[f"volatility_atr_pct_{w}"] = atr_pct
        out[f"volatility_regime_{w}"] = regime
        out[f"volatility_vix_distance_{w}"] = vix_distance
        out[f"volatility_expected_move_{w}"] = expected_move
        out[f"volatility_compression_{w}"] = compression
        out[f"volatility_expansion_prob_{w}"] = expansion_prob

    return add_normalized_series(out, normalization_window)


# --- Engine -------------------------------------------------------------------------

class VolatilityFeatureEngine(BaseFeatureEngine):
    name = ENGINE_NAME
    category = CATEGORY

    def _definitions(self) -> list[FeatureDefinition]:
        return volatility_feature_definitions(
            self.windows,
            self._settings.feature_normalization_window,
            self._settings.feature_vix_symbol,
            calculation_frequency=f"{self._settings.feature_engine_interval}s",
        )

    def _reference_symbol(self, symbol: str) -> str | None:
        vix = self._settings.feature_vix_symbol
        return vix if symbol != vix else None

    def _compute(
        self, candles: Sequence[Candle], benchmark: Sequence[Candle] | None = None
    ) -> dict[str, Series]:
        return compute_volatility_features(
            candles,
            vix=benchmark,
            windows=self.windows,
            normalization_window=self._settings.feature_normalization_window,
        )
