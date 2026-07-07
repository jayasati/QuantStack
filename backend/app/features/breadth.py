"""Breadth Feature Engine (Volume 3, Prompt 3.6).

Transforms the market_breadth collector's observations (market_events,
event_type breadth.observation, instrument MARKET) into versioned Feature
Store features on collector-run snapshot time, under the synthetic timeframe
"breadth" and the market-wide symbol "MARKET".

Feature conventions:
- Breadth Strength is (advances - declines)/(advances + declines), -1..1.
- Participation % is the share of the universe advancing.
- Trend Breadth is the mean % of the universe above the 20/50/100/200 EMAs.
- Breadth Divergence passes through the collector's equal-weight minus
  cap-weight return spread, in % points — negative means a few large caps
  are masking broad weakness.
- The momentum features are least-squares slopes over the window, per
  snapshot: Breadth Momentum on strength, Advance/Decline Momentum on the
  cumulative AD line (net advancers per snapshot), and New High/Low Momentum
  on the 52-week high/low counts.
- Breadth Health Score passes through the collector's 0-100 composite.

Every feature ships a look-ahead-safe rolling z-score companion (_z).
"""

from collections.abc import Sequence
from statistics import fmean

from app.core.logging import get_logger
from app.features.base import BaseFeatureEngine
from app.features.normalize import (
    add_normalized_series,
    normalized_definition,
    rolling_slope,
)
from app.features.schema import Candle, FeatureDefinition, Series
from app.features.snapshots import Snapshot, bucket_observations

logger = get_logger(__name__)

ENGINE_NAME = "breadth_feature_engine"
ENGINE_VERSION = "v1"
CATEGORY = "breadth"

MARKET_SYMBOL = "MARKET"
BREADTH_TIMEFRAME = "breadth"

EMA_PERIODS = (20, 50, 100, 200)


# --- Feature definitions -------------------------------------------------------

def breadth_feature_definitions(
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
        define("breadth_strength",
               "(advances - declines)/(advances + declines), -1..1.",
               "ratio", (-1.0, 1.0)),
        define("breadth_participation_pct",
               "Share of the universe advancing, in %.",
               "%", (0.0, 100.0)),
        define("breadth_trend_pct",
               "Mean % of the universe above the 20/50/100/200 EMAs.",
               "%", (0.0, 100.0)),
        define("breadth_divergence",
               "Equal-weight minus cap-weight universe return, in % points "
               "(negative = narrow large-cap leadership).",
               "%", (-10.0, 10.0)),
        define("breadth_health_score",
               "Composite 0-100 breadth score from the breadth collector.",
               "index", (0.0, 100.0)),
    ]
    for w in windows:
        definitions.extend([
            define(f"breadth_momentum_{w}",
                   f"Slope of breadth strength over {w} snapshots.",
                   "ratio", (-1.0, 1.0), ("breadth_strength",), w),
            define(f"breadth_ad_momentum_{w}",
                   f"Slope of the cumulative advance-decline line over {w} "
                   "snapshots (net advancers per snapshot).",
                   "count", (None, None), (), w),
            define(f"breadth_new_high_momentum_{w}",
                   f"Slope of the 52-week new-high count over {w} snapshots.",
                   "count", (None, None), (), w),
            define(f"breadth_new_low_momentum_{w}",
                   f"Slope of the 52-week new-low count over {w} snapshots.",
                   "count", (None, None), (), w),
        ])
    definitions.extend(
        normalized_definition(d, normalization_window) for d in list(definitions)
    )
    return definitions


# --- Pure calculations -----------------------------------------------------------

def compute_breadth_features(
    snapshots: Sequence[Snapshot],
    windows: Sequence[int] = (5, 10, 20, 50, 100, 200),
    normalization_window: int = 100,
) -> dict[str, Series]:
    """Compute every breadth feature (raw + _z normalized) aligned to `snapshots`."""
    n = len(snapshots)
    strength: Series = [None] * n
    participation: Series = [None] * n
    trend: Series = [None] * n
    divergence: Series = [None] * n
    health: Series = [None] * n
    ad_line: Series = [None] * n
    new_highs: Series = [None] * n
    new_lows: Series = [None] * n

    for i, snap in enumerate(snapshots):
        advances = snap.values.get("advances")
        declines = snap.values.get("declines")
        unchanged = snap.values.get("unchanged")
        if advances is not None and declines is not None:
            moving = advances + declines
            if moving > 0:
                strength[i] = (advances - declines) / moving
            if unchanged is not None and moving + unchanged > 0:
                participation[i] = advances / (moving + unchanged) * 100

        ema_values = [
            v for p in EMA_PERIODS
            if (v := snap.values.get(f"pct_above_ema{p}")) is not None
        ]
        if ema_values:
            trend[i] = fmean(ema_values)

        divergence[i] = snap.values.get("breadth_divergence")
        health[i] = snap.values.get("breadth_score")
        new_highs[i] = snap.values.get("new_highs_52w")
        new_lows[i] = snap.values.get("new_lows_52w")

        ad_meta = snap.metadata.get("ad_line_delta") or {}
        ad_value = ad_meta.get("ad_line")
        if ad_value is not None:
            ad_line[i] = float(ad_value)

    out: dict[str, Series] = {
        "breadth_strength": strength,
        "breadth_participation_pct": participation,
        "breadth_trend_pct": trend,
        "breadth_divergence": divergence,
        "breadth_health_score": health,
    }
    for w in windows:
        out[f"breadth_momentum_{w}"] = rolling_slope(strength, w)
        out[f"breadth_ad_momentum_{w}"] = rolling_slope(ad_line, w)
        out[f"breadth_new_high_momentum_{w}"] = rolling_slope(new_highs, w)
        out[f"breadth_new_low_momentum_{w}"] = rolling_slope(new_lows, w)

    return add_normalized_series(out, normalization_window)


# --- Engine -------------------------------------------------------------------------

class BreadthFeatureEngine(BaseFeatureEngine):
    name = ENGINE_NAME
    category = CATEGORY

    def _definitions(self) -> list[FeatureDefinition]:
        return breadth_feature_definitions(
            self.windows,
            self._settings.feature_normalization_window,
            calculation_frequency=f"{self._settings.feature_engine_interval}s",
        )

    def _compute(
        self, candles: Sequence[Candle], benchmark: Sequence[Candle] | None = None
    ) -> dict[str, Series]:
        return {}  # breadth features live on collector-run time, not bars

    async def run(
        self, symbol: str = MARKET_SYMBOL, timeframe: str = "D", full: bool = False
    ) -> dict:
        """Breadth is market-wide: the symbol/timeframe arguments are ignored
        in favor of MARKET/"breadth"."""
        observations = await self._load_labeled_observations(
            "breadth.observation", MARKET_SYMBOL, "metric",
            self._settings.feature_breadth_lookback,
        )
        snapshots = bucket_observations(observations)
        if len(snapshots) < 2:
            return {
                "symbol": MARKET_SYMBOL,
                "timeframe": BREADTH_TIMEFRAME,
                "stored": 0,
                "skipped": True,
            }
        series = compute_breadth_features(
            snapshots, self.windows, self._settings.feature_normalization_window
        )
        return await self._process_series(
            MARKET_SYMBOL, BREADTH_TIMEFRAME, [s.ts for s in snapshots], series, full=full
        )

    async def run_all(self) -> list[dict]:
        """One market-wide run — the watchlist does not apply here."""
        try:
            return [await self.run()]
        except Exception as exc:
            logger.error("breadth feature run failed", extra={"error": str(exc)})
            return [{"symbol": MARKET_SYMBOL, "error": str(exc)}]
