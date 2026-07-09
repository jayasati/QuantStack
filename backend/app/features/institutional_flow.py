"""Institutional Flow Feature Engine (Volume 3 gap fill).

Chapter 2 of Volume 3 lists "Institutional Flow Features" as a feature
category, but no Volume 3 prompt (3.1-3.18) was ever assigned to build it —
only Price, Volume, Volatility, Liquidity, Options, Breadth, Sector,
Relative Strength, Market Structure, News, Event Risk, and Time got engines.
Volume 4's Institutional Flow Intelligence (Prompt 4.5) needs a Feature
Store input like every other Volume 4 component (base.py's contract: never
touch collectors directly), so this fills that gap using the exact recipe
Prompt 3.6 (Breadth) and 3.7 (Sector) used: bucket the collector's own
market-wide observations (market_events, event_type
institutional_flow.observation, instrument MARKET) into per-run snapshots.

Feature conventions:
- FII/DII/ETF/Promoter/Insider/SAST scores pass through the collector's own
  [-1, 1] (SAST: [0, 1]) normalized scores — see collectors/domains/flows.py
  for how each is computed (today vs 20-day average, net-over-gross, or a
  saturating scale, per metric).
- Deal Activity Score (block + bulk deals combined — the collector doesn't
  publish it as its own standalone metric) is pulled from the participation-
  index record's `components` metadata sidecar, the same "extra field lives
  in metadata" pattern the Sector Feature Engine uses for its AD-line delta.
- Participation Index passes through the composite 0-100 score (50 =
  neutral), reconstructed from the record's own [-1, 1] normalized value.
- Momentum features are least-squares slopes over the window, per snapshot,
  on the FII, DII, and participation scores — same convention as Breadth
  Momentum.

Every feature ships a look-ahead-safe rolling z-score companion (_z).
"""

from collections.abc import Sequence

from app.core.logging import get_logger
from app.features.base import BaseFeatureEngine
from app.features.normalize import add_normalized_series, normalized_definition, rolling_slope
from app.features.schema import Candle, FeatureDefinition, Series
from app.features.snapshots import Snapshot, bucket_observations

logger = get_logger(__name__)

ENGINE_NAME = "institutional_flow_feature_engine"
ENGINE_VERSION = "v1"
CATEGORY = "institutional_flow"

MARKET_SYMBOL = "MARKET"
FLOW_TIMEFRAME = "flow"
FLOW_EVENT_TYPE = "institutional_flow.observation"

# Metric labels as published in InstitutionalFlowCollector's
# CollectorOutput.metadata["metric"] -> the feature name they become here.
PASSTHROUGH_METRICS: dict[str, str] = {
    "fii_flow": "flow_fii_score",
    "dii_flow": "flow_dii_score",
    "etf_flow": "flow_etf_score",
    "promoter_net": "flow_promoter_score",
    "insider_net": "flow_insider_score",
    "sast_filings": "flow_sast_score",
    "participation_index": "flow_participation_score",
}
MOMENTUM_BASE_FEATURES: tuple[str, ...] = (
    "flow_fii_score", "flow_dii_score", "flow_participation_score",
)


# --- Feature definitions -------------------------------------------------------

def flow_feature_definitions(
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
        define("flow_fii_score",
               "FII net cash flow today vs its 20-day average magnitude, -1..1.",
               "ratio", (-1.0, 1.0)),
        define("flow_dii_score",
               "DII net cash flow today vs its 20-day average magnitude, -1..1.",
               "ratio", (-1.0, 1.0)),
        define("flow_etf_score",
               "ETF net flow today vs the FII 20-day average magnitude, -1..1.",
               "ratio", (-1.0, 1.0)),
        define("flow_deal_activity_score",
               "Net over gross value of today's block + bulk deals, -1..1.",
               "ratio", (-1.0, 1.0)),
        define("flow_promoter_score",
               "Net over gross promoter buy/sell value, -1..1.",
               "ratio", (-1.0, 1.0)),
        define("flow_insider_score",
               "Net insider transactions scaled to saturate at +/-100cr, -1..1.",
               "ratio", (-1.0, 1.0)),
        define("flow_sast_score",
               "SAST filing count scaled to saturate at 10 filings, 0..1.",
               "ratio", (0.0, 1.0)),
        define("flow_participation_score",
               "Composite institutional participation, -1..1 (rescaled from "
               "the 0-100 index).",
               "ratio", (-1.0, 1.0)),
        define("flow_participation_index",
               "Composite Institutional Participation Index, 0-100 (50 = neutral).",
               "index", (0.0, 100.0), ("flow_participation_score",)),
    ]
    for w in windows:
        for base_name in MOMENTUM_BASE_FEATURES:
            definitions.append(
                define(f"{base_name}_momentum_{w}",
                       f"Slope of {base_name} over {w} snapshots.",
                       "ratio", (None, None), (base_name,), w)
            )
    definitions.extend(
        normalized_definition(d, normalization_window) for d in list(definitions)
    )
    return definitions


# --- Pure calculations -----------------------------------------------------------

def compute_flow_features(
    snapshots: Sequence[Snapshot],
    windows: Sequence[int] = (5, 10, 20, 50, 100, 200),
    normalization_window: int = 100,
) -> dict[str, Series]:
    """Compute every flow feature (raw + _z normalized) aligned to `snapshots`."""
    n = len(snapshots)
    scores: dict[str, Series] = {name: [None] * n for name in PASSTHROUGH_METRICS.values()}
    deal_activity: Series = [None] * n
    participation_index: Series = [None] * n

    for i, snap in enumerate(snapshots):
        for metric, feature_name in PASSTHROUGH_METRICS.items():
            value = snap.values.get(metric)
            if value is not None:
                scores[feature_name][i] = value

        participation_meta = snap.metadata.get("participation_index") or {}
        components = participation_meta.get("components") or {}
        deal_score = components.get("deal_activity")
        if deal_score is not None:
            deal_activity[i] = float(deal_score)

        participation_score = scores["flow_participation_score"][i]
        if participation_score is not None:
            participation_index[i] = 50 + 50 * participation_score

    out: dict[str, Series] = {
        **scores,
        "flow_deal_activity_score": deal_activity,
        "flow_participation_index": participation_index,
    }
    for w in windows:
        for base_name in MOMENTUM_BASE_FEATURES:
            out[f"{base_name}_momentum_{w}"] = rolling_slope(out[base_name], w)

    return add_normalized_series(out, normalization_window)


# --- Engine -------------------------------------------------------------------------

class InstitutionalFlowFeatureEngine(BaseFeatureEngine):
    name = ENGINE_NAME
    category = CATEGORY

    def _definitions(self) -> list[FeatureDefinition]:
        return flow_feature_definitions(
            self.windows,
            self._settings.feature_normalization_window,
            calculation_frequency=f"{self._settings.feature_engine_interval}s",
        )

    def _compute(
        self, candles: Sequence[Candle], benchmark: Sequence[Candle] | None = None
    ) -> dict[str, Series]:
        return {}  # flow features live on collector-run time, not bars

    async def run(
        self, symbol: str = MARKET_SYMBOL, timeframe: str = "D", full: bool = False
    ) -> dict:
        """Flow is market-wide: the symbol/timeframe arguments are ignored
        in favor of MARKET/"flow"."""
        observations = await self._load_labeled_observations(
            FLOW_EVENT_TYPE, MARKET_SYMBOL, "metric", self._settings.feature_flow_lookback
        )
        snapshots = bucket_observations(observations)
        if len(snapshots) < 2:
            return {
                "symbol": MARKET_SYMBOL,
                "timeframe": FLOW_TIMEFRAME,
                "stored": 0,
                "skipped": True,
            }
        series = compute_flow_features(
            snapshots, self.windows, self._settings.feature_normalization_window
        )
        return await self._process_series(
            MARKET_SYMBOL, FLOW_TIMEFRAME, [s.ts for s in snapshots], series, full=full
        )

    async def run_all(self) -> list[dict]:
        """One market-wide run — the watchlist does not apply here."""
        try:
            return [await self.run()]
        except Exception as exc:
            logger.error("institutional flow feature run failed", extra={"error": str(exc)})
            return [{"symbol": MARKET_SYMBOL, "error": str(exc)}]
