"""Volume Feature Engine (Volume 3, Prompt 3.2).

Transforms OHLCV candles into the 13 volume features of Chapter 10 and, per
the prompt, ships every feature with a normalized companion (rolling z-score,
suffix _z, look-ahead safe).

Feature conventions:
- Buying/Selling Pressure attribute the bar's volume by where the close sits
  in the bar's range; Volume Delta is their difference and Volume Imbalance is
  the windowed delta as a fraction of windowed volume (-1..1).
- RVOL divides the bar's volume by the average of the *prior* window, so a
  spike does not dilute its own baseline. Volume Spike is the z-score of the
  bar's volume within the window.
- Volume Trend is the linear-regression slope of volume over the window, as %
  of average volume per bar. Volume Oscillator compares the w-bar volume SMA
  to the 2w-bar SMA, in %.
- OBV and Accumulation/Distribution are cumulative from the start of the
  loaded history; CMF and MFI use their classic definitions per window.
- Index symbols report zero volume, so every feature is None there by design.
"""

from collections.abc import Sequence
from statistics import fmean, pstdev

from app.features.base import BaseFeatureEngine
from app.features.normalize import add_normalized_series, normalized_definition
from app.features.schema import Candle, FeatureDefinition, Series

ENGINE_NAME = "volume_feature_engine"
ENGINE_VERSION = "v1"
CATEGORY = "volume"


# --- Feature definitions -------------------------------------------------------

def volume_feature_definitions(
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
        define("volume_buying_pressure",
               "Bar volume attributed to buyers: (close-low)/(high-low) x volume.",
               "volume", (0.0, None)),
        define("volume_selling_pressure",
               "Bar volume attributed to sellers: (high-close)/(high-low) x volume.",
               "volume", (0.0, None)),
        define("volume_delta", "Buying pressure minus selling pressure.",
               "volume", (None, None),
               ("volume_buying_pressure", "volume_selling_pressure")),
        define("volume_obv", "On-Balance Volume, cumulative over loaded history.",
               "volume", (None, None)),
        define("volume_accum_dist",
               "Accumulation/Distribution line, cumulative over loaded history.",
               "volume", (None, None)),
    ]
    for w in windows:
        definitions.extend([
            define(f"volume_rolling_avg_{w}", f"Simple average volume over {w} bars.",
                   "volume", (0.0, None), (), w),
            define(f"volume_rvol_{w}",
                   f"Relative volume: bar volume vs the prior {w}-bar average.",
                   "ratio", (0.0, 50.0), (f"volume_rolling_avg_{w}",), w),
            define(f"volume_spike_{w}",
                   f"Volume z-score within the {w}-bar window.",
                   "zscore", (-10.0, 20.0), (f"volume_rolling_avg_{w}",), w),
            define(f"volume_trend_{w}",
                   f"Volume slope over {w} bars, as % of average volume per bar.",
                   "%", (-100.0, 100.0), (), w),
            define(f"volume_imbalance_{w}",
                   "Windowed volume delta as a fraction of windowed volume (-1..1).",
                   "ratio", (-1.0, 1.0), ("volume_delta",), w),
            define(f"volume_oscillator_{w}",
                   f"{w}-bar volume SMA vs {2 * w}-bar SMA, in %.",
                   "%", (-100.0, 1000.0), (f"volume_rolling_avg_{w}",), w),
            define(f"volume_cmf_{w}", f"Chaikin Money Flow over {w} bars (-1..1).",
                   "ratio", (-1.0, 1.0), (), w),
            define(f"volume_mfi_{w}", f"Money Flow Index over {w} bars (0..100).",
                   "index", (0.0, 100.0), (), w),
        ])
    # Prompt 3.2: normalize every feature — each raw feature gets a _z companion.
    definitions.extend(
        normalized_definition(d, normalization_window) for d in list(definitions)
    )
    return definitions


# --- Pure calculations -----------------------------------------------------------

def compute_volume_features(
    candles: Sequence[Candle],
    windows: Sequence[int] = (5, 10, 20, 50, 100, 200),
    normalization_window: int = 100,
) -> dict[str, Series]:
    """Compute every volume feature (raw + _z normalized) aligned to `candles`.

    Returns an empty map when the history carries no volume at all (index
    symbols) — there is nothing meaningful to store.
    """
    n = len(candles)
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    volumes = [float(c.volume) for c in candles]
    if sum(volumes) <= 0:
        return {}
    typical = [(h + low + c) / 3 for h, low, c in zip(highs, lows, closes, strict=True)]

    buying: Series = [None] * n
    selling: Series = [None] * n
    delta: Series = [None] * n
    # Money-flow multiplier per bar: where the close sits in the range (-1..1).
    mf_multiplier: list[float] = [0.0] * n
    for i in range(n):
        bar_range = highs[i] - lows[i]
        if bar_range > 0:
            mf_multiplier[i] = ((closes[i] - lows[i]) - (highs[i] - closes[i])) / bar_range
            if volumes[i] > 0:
                buy_volume = (closes[i] - lows[i]) / bar_range * volumes[i]
                sell_volume = (highs[i] - closes[i]) / bar_range * volumes[i]
                buying[i] = buy_volume
                selling[i] = sell_volume
                delta[i] = buy_volume - sell_volume

    obv: Series = [None] * n
    running_obv = 0.0
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            running_obv += volumes[i]
        elif closes[i] < closes[i - 1]:
            running_obv -= volumes[i]
        obv[i] = running_obv

    accum_dist: Series = [None] * n
    running_ad = 0.0
    for i in range(n):
        running_ad += mf_multiplier[i] * volumes[i]
        accum_dist[i] = running_ad

    out: dict[str, Series] = {
        "volume_buying_pressure": buying,
        "volume_selling_pressure": selling,
        "volume_delta": delta,
        "volume_obv": obv,
        "volume_accum_dist": accum_dist,
    }

    for w in windows:
        rolling_avg: Series = [None] * n
        rvol: Series = [None] * n
        spike: Series = [None] * n
        trend: Series = [None] * n
        imbalance: Series = [None] * n
        oscillator: Series = [None] * n
        cmf: Series = [None] * n
        mfi: Series = [None] * n

        for i in range(n):
            if i >= w - 1:
                window_volumes = volumes[i - w + 1 : i + 1]
                avg = fmean(window_volumes)
                rolling_avg[i] = avg
                std = pstdev(window_volumes)
                if std > 0:
                    spike[i] = (volumes[i] - avg) / std
                if avg > 0:
                    # Least-squares slope of volume vs bar index, as % of avg.
                    mean_t = (w - 1) / 2
                    var_t = fmean([(t - mean_t) ** 2 for t in range(w)])
                    slope = fmean(
                        [(t - mean_t) * (v - avg) for t, v in enumerate(window_volumes)]
                    ) / var_t
                    trend[i] = slope / avg * 100

                volume_sum = sum(window_volumes)
                if volume_sum > 0:
                    deltas = [d for d in delta[i - w + 1 : i + 1] if d is not None]
                    if deltas:
                        imbalance[i] = sum(deltas) / volume_sum
                    cmf[i] = (
                        sum(mf_multiplier[j] * volumes[j] for j in range(i - w + 1, i + 1))
                        / volume_sum
                    )

            if i >= w:
                prior_avg = fmean(volumes[i - w : i])
                if prior_avg > 0:
                    rvol[i] = volumes[i] / prior_avg

                positive_flow = 0.0
                negative_flow = 0.0
                for j in range(i - w + 1, i + 1):
                    raw_flow = typical[j] * volumes[j]
                    if typical[j] > typical[j - 1]:
                        positive_flow += raw_flow
                    elif typical[j] < typical[j - 1]:
                        negative_flow += raw_flow
                total_flow = positive_flow + negative_flow
                if total_flow > 0:
                    mfi[i] = positive_flow / total_flow * 100

            if i >= 2 * w - 1:
                slow = fmean(volumes[i - 2 * w + 1 : i + 1])
                fast = rolling_avg[i]
                if slow > 0 and fast is not None:
                    oscillator[i] = (fast - slow) / slow * 100

        out[f"volume_rolling_avg_{w}"] = rolling_avg
        out[f"volume_rvol_{w}"] = rvol
        out[f"volume_spike_{w}"] = spike
        out[f"volume_trend_{w}"] = trend
        out[f"volume_imbalance_{w}"] = imbalance
        out[f"volume_oscillator_{w}"] = oscillator
        out[f"volume_cmf_{w}"] = cmf
        out[f"volume_mfi_{w}"] = mfi

    return add_normalized_series(out, normalization_window)


# --- Engine -------------------------------------------------------------------------

class VolumeFeatureEngine(BaseFeatureEngine):
    name = ENGINE_NAME
    category = CATEGORY

    def _definitions(self) -> list[FeatureDefinition]:
        return volume_feature_definitions(
            self.windows,
            self._settings.feature_normalization_window,
            calculation_frequency=f"{self._settings.feature_engine_interval}s",
        )

    def _compute(
        self, candles: Sequence[Candle], benchmark: Sequence[Candle] | None = None
    ) -> dict[str, Series]:
        return compute_volume_features(
            candles, self.windows, self._settings.feature_normalization_window
        )
