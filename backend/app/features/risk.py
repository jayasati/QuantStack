"""Risk Feature Engine (Volume 3, Chapter 2 gap fill).

The doc's Chapter 2 category table lists "Risk Features" alongside every
other domain, but — unlike Institutional Flow and Macro, which got their own
numbered prompts and chapters when built later as gap fills — no chapter or
prompt ever specified what belongs in it. This engine fills that gap with the
standard quant risk-feature set that isn't already covered elsewhere in the
Feature Store: tail risk (VaR/CVaR/tail ratio) and risk-adjusted return
(Sharpe/Sortino/Calmar) metrics, computed purely from OHLCV candles like
Price and Volatility. It deliberately does NOT duplicate:
- Volatility Feature Engine: dispersion (historical/realized/rolling vol,
  ATR%, vol regime/compression, VIX distance).
- Price Feature Engine: systematic risk (beta/alpha/correlation vs benchmark).
- Event Risk Feature Engine (app/features/events.py, Chapter 19): event-driven
  risk windows/freezes, a different unit of analysis (calendar events, not
  the return distribution).

Feature conventions:
- All ratio/return-based metrics use SIMPLE returns (not log), matching how
  Sharpe/Sortino/Calmar are conventionally defined.
- VaR/CVaR/downside-deviation/max-drawdown/current-drawdown/Ulcer-Index are
  expressed as positive-magnitude percentages (a "10% VaR" means a possible
  10% loss, not -10%).
- Sharpe/Sortino assume a 0% risk-free rate: this codebase has no daily
  risk-free-rate data source (India10Y was investigated and left
  deliberately omitted — see project notes), and fabricating one would
  violate the "never fabricate market data" rule every other collector and
  engine in this codebase follows. Document this rather than hide it.
- VaR/CVaR/tail-ratio use empirical (historical) quantiles of the window's
  return distribution, not a parametric (Gaussian) assumption — the same
  look-ahead-safe trailing-window approach as every other rolling feature
  here, and no distributional assumption that a fat-tailed emerging-market
  return series would violate.
"""

import math
from collections.abc import Sequence
from statistics import fmean, pstdev

from app.features.base import BaseFeatureEngine
from app.features.normalize import add_normalized_series, normalized_definition
from app.features.schema import Candle, FeatureDefinition, Series

ENGINE_NAME = "risk_feature_engine"
ENGINE_VERSION = "v1"
CATEGORY = "risk"

TRADING_DAYS = 252


# --- Feature definitions -------------------------------------------------------

def risk_feature_definitions(
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
        definitions.extend([
            define(f"risk_var_95_{w}",
                   f"Historical 95% Value at Risk over {w} bars, in % (positive = loss "
                   "magnitude).",
                   "%", (0.0, 100.0), (), w),
            define(f"risk_var_99_{w}",
                   f"Historical 99% Value at Risk over {w} bars, in % (positive = loss "
                   "magnitude).",
                   "%", (0.0, 100.0), (), w),
            define(f"risk_cvar_95_{w}",
                   f"Expected Shortfall (mean loss beyond the 95% VaR threshold) over "
                   f"{w} bars, in %.",
                   "%", (0.0, 100.0), (f"risk_var_95_{w}",), w),
            define(f"risk_max_drawdown_{w}",
                   f"Largest peak-to-trough decline within the trailing {w} bars, in %.",
                   "%", (0.0, 100.0), (), w),
            define(f"risk_current_drawdown_{w}",
                   f"Decline from the {w}-bar rolling high as of the current bar, in %.",
                   "%", (0.0, 100.0), (), w),
            define(f"risk_downside_deviation_{w}",
                   f"Annualized std of below-target (0%) returns over {w} bars, in %.",
                   "%", (0.0, 200.0), (), w),
            define(f"risk_sharpe_{w}",
                   f"Annualized Sharpe ratio over {w} bars (0% risk-free rate assumed; "
                   "no daily risk-free data source exists in this codebase).",
                   "ratio", (-10.0, 10.0), (), w),
            define(f"risk_sortino_{w}",
                   f"Annualized Sortino ratio over {w} bars (0% risk-free/target rate).",
                   "ratio", (-10.0, 10.0), (f"risk_downside_deviation_{w}",), w),
            define(f"risk_calmar_{w}",
                   f"Annualized return over max drawdown, over {w} bars.",
                   "ratio", (-10.0, 10.0), (f"risk_max_drawdown_{w}",), w),
            define(f"risk_skew_{w}",
                   f"Skewness of simple returns over {w} bars (negative = crash-prone "
                   "left tail).",
                   "moment", (-5.0, 5.0), (), w),
            define(f"risk_kurtosis_{w}",
                   f"Excess kurtosis of simple returns over {w} bars (> 0 = fatter "
                   "tails than a normal distribution).",
                   "moment", (-5.0, 50.0), (), w),
            define(f"risk_ulcer_index_{w}",
                   f"Root-mean-square of drawdowns within the trailing {w} bars, in % "
                   "(smoother than max drawdown alone).",
                   "%", (0.0, 100.0), (), w),
            define(f"risk_tail_ratio_{w}",
                   f"Magnitude of the 95th-percentile return over the 5th-percentile "
                   f"return, over {w} bars (< 1 = fatter loss tail than gain tail).",
                   "ratio", (0.0, 20.0), (), w),
        ])
    # Standardized z-score companion for every feature, matching every other engine.
    definitions.extend(
        normalized_definition(d, normalization_window) for d in list(definitions)
    )
    return definitions


# --- Pure calculations -----------------------------------------------------------

def _quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated order statistic (same formula as normalize.py's
    _quartiles, generalized to an arbitrary quantile)."""
    ordered = sorted(values)
    n = len(ordered)
    position = q * (n - 1)
    lower = int(position)
    upper = min(lower + 1, n - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def compute_risk_features(
    candles: Sequence[Candle],
    windows: Sequence[int] = (5, 10, 20, 50, 100, 200),
    normalization_window: int = 100,
) -> dict[str, Series]:
    """Compute every risk feature (raw + _z normalized) aligned to `candles`."""
    n = len(candles)
    closes = [c.close for c in candles]

    simple_return: Series = [None] * n
    for i in range(1, n):
        prev_close = closes[i - 1]
        if prev_close > 0:
            simple_return[i] = closes[i] / prev_close - 1

    annualize = math.sqrt(TRADING_DAYS)
    out: dict[str, Series] = {}
    for w in windows:
        var95: Series = [None] * n
        var99: Series = [None] * n
        cvar95: Series = [None] * n
        max_dd: Series = [None] * n
        cur_dd: Series = [None] * n
        downside_dev: Series = [None] * n
        sharpe: Series = [None] * n
        sortino: Series = [None] * n
        calmar: Series = [None] * n
        skew: Series = [None] * n
        kurtosis: Series = [None] * n
        ulcer: Series = [None] * n
        tail_ratio: Series = [None] * n

        for i in range(w, n):
            window_rets = [r for r in simple_return[i - w + 1 : i + 1] if r is not None]
            if len(window_rets) == w:
                mean_r = fmean(window_rets)
                std_r = pstdev(window_rets)

                q05 = _quantile(window_rets, 0.05)
                q95 = _quantile(window_rets, 0.95)
                q01 = _quantile(window_rets, 0.01)
                var95[i] = max(0.0, -q05 * 100)
                var99[i] = max(0.0, -q01 * 100)
                tail_losses = [r for r in window_rets if r <= q05]
                if tail_losses:
                    cvar95[i] = max(0.0, -fmean(tail_losses) * 100)
                if q05 != 0:
                    tail_ratio[i] = abs(q95) / abs(q05)

                downside_dev_raw = math.sqrt(fmean([min(r, 0.0) ** 2 for r in window_rets]))
                downside_dev[i] = downside_dev_raw * annualize * 100

                if std_r > 0:
                    sharpe[i] = mean_r / std_r * annualize
                if downside_dev_raw > 0:
                    sortino[i] = mean_r / downside_dev_raw * annualize
                if std_r > 0:
                    skew[i] = fmean([((r - mean_r) / std_r) ** 3 for r in window_rets])
                    kurtosis[i] = fmean([((r - mean_r) / std_r) ** 4 for r in window_rets]) - 3.0

            window_closes = closes[i - w + 1 : i + 1]
            peak = window_closes[0]
            drawdowns: list[float] = []
            for close in window_closes:
                peak = max(peak, close)
                drawdowns.append((peak - close) / peak if peak > 0 else 0.0)
            max_dd_pct = max(drawdowns) * 100
            max_dd[i] = max_dd_pct
            cur_dd[i] = drawdowns[-1] * 100
            ulcer[i] = math.sqrt(fmean([d * d for d in drawdowns])) * 100

            if len(window_rets) == w and max_dd_pct > 0:
                annualized_return_pct = mean_r * TRADING_DAYS * 100
                calmar[i] = annualized_return_pct / max_dd_pct

        out[f"risk_var_95_{w}"] = var95
        out[f"risk_var_99_{w}"] = var99
        out[f"risk_cvar_95_{w}"] = cvar95
        out[f"risk_max_drawdown_{w}"] = max_dd
        out[f"risk_current_drawdown_{w}"] = cur_dd
        out[f"risk_downside_deviation_{w}"] = downside_dev
        out[f"risk_sharpe_{w}"] = sharpe
        out[f"risk_sortino_{w}"] = sortino
        out[f"risk_calmar_{w}"] = calmar
        out[f"risk_skew_{w}"] = skew
        out[f"risk_kurtosis_{w}"] = kurtosis
        out[f"risk_ulcer_index_{w}"] = ulcer
        out[f"risk_tail_ratio_{w}"] = tail_ratio

    return add_normalized_series(out, normalization_window)


# --- Engine -------------------------------------------------------------------------

class RiskFeatureEngine(BaseFeatureEngine):
    name = ENGINE_NAME
    category = CATEGORY

    def _definitions(self) -> list[FeatureDefinition]:
        return risk_feature_definitions(
            self.windows,
            self._settings.feature_normalization_window,
            calculation_frequency=f"{self._settings.feature_engine_interval}s",
        )

    def _compute(
        self, candles: Sequence[Candle], benchmark: Sequence[Candle] | None = None
    ) -> dict[str, Series]:
        return compute_risk_features(
            candles,
            windows=self.windows,
            normalization_window=self._settings.feature_normalization_window,
        )
