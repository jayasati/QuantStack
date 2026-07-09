"""Options Feature Engine (Volume 3, Prompt 3.5).

Transforms the option-chain observations published by the options_intelligence
collector (market_events, event_type options.observation) into versioned
Feature Store features. Observations from one collector run are bucketed into
a chain snapshot (the collector runs on a fixed interval); features live on
snapshot time under the synthetic timeframe "chain".

Feature conventions:
- OI Change % is the change of total chain OI (calls + puts, from the PCR
  observation's metadata) versus the previous snapshot, in %.
- Max Pain Distance is (max pain strike - spot)/spot in % — positive when max
  pain sits above spot (upward pull into expiry).
- IV Rank is (current ATM IV - min)/(max - min) over the trailing
  normalization window of snapshots; IV Percentile is the trailing percentile
  of ATM IV over the same window. Both 0-100.
- Call/Put Writing Scores are the collector's writing intensities: fresh OI
  added on that side relative to the side's total OI.
- Gamma/Delta Exposure pass through the collector's chain-wide proxies and
  stay empty on snapshots where the chain carried no Greeks.
- ATM Theta %, ATM Gamma, ATM Vega (same-day F&O gap fill, 2026-07-09) pass
  through the collector's ATM-strike Greeks — genuinely different from
  Gamma/Delta Exposure above: those are chain-wide OI-weighted sums, these
  are the single ATM strike's raw Greeks, which is where gamma/theta peak
  and where a same-day trader's actual risk concentrates. Instrument-level,
  not position-level: this codebase has no open-position tracking yet.
- Option Volume Ratio is the volume PCR (put volume / call volume).
- Expected Move is the one-day move implied by ATM IV:
  spot x IV/100 / sqrt(252).
- Dealer Positioning Score is a v1 heuristic in -1..1: the balance of put vs
  call writing intensity — positive when put writing dominates (professional
  money selling downside, structurally supportive). A Greeks-aware model can
  ship as v2.

Every feature ships a look-ahead-safe rolling z-score companion (_z).
"""

import math
from collections.abc import Sequence

from app.features.base import BaseFeatureEngine
from app.features.normalize import (
    add_normalized_series,
    normalized_definition,
    trailing_percentile,
)
from app.features.schema import Candle, FeatureDefinition, Series
from app.features.snapshots import Snapshot as ChainSnapshot
from app.features.snapshots import bucket_observations as bucket_snapshots

ENGINE_NAME = "options_feature_engine"
ENGINE_VERSION = "v1"
CATEGORY = "options"

OPTIONS_TIMEFRAME = "chain"
TRADING_DAYS = 252


# --- Feature definitions -------------------------------------------------------

def options_feature_definitions(
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
        define("options_pcr", "Put/call ratio of total chain OI.", "ratio", (0.0, 5.0)),
        define("options_oi_change_pct",
               "Total chain OI change vs the previous snapshot, in %.",
               "%", (-50.0, 50.0), ("options_pcr",)),
        define("options_max_pain_distance_pct",
               "Max pain strike distance from spot, in % of spot.",
               "%", (-20.0, 20.0)),
        define("options_atm_iv", "At-the-money implied volatility, in %.",
               "%", (0.0, 150.0)),
        define("options_iv_rank",
               f"ATM IV position between its min and max over the trailing "
               f"{normalization_window} snapshots (0-100).",
               "index", (0.0, 100.0), ("options_atm_iv",)),
        define("options_iv_percentile",
               f"Trailing percentile of ATM IV over {normalization_window} "
               "snapshots (0-100).",
               "index", (0.0, 100.0), ("options_atm_iv",)),
        define("options_call_writing_score",
               "Fresh call OI added relative to total call OI.",
               "ratio", (0.0, 1.0)),
        define("options_put_writing_score",
               "Fresh put OI added relative to total put OI.",
               "ratio", (0.0, 1.0)),
        define("options_gamma_exposure",
               "Net gamma exposure proxy (call gamma x OI - put gamma x OI); "
               "empty when the chain carries no Greeks.",
               "exposure", (None, None)),
        define("options_delta_exposure",
               "Net delta exposure proxy (call delta x OI + put delta x OI); "
               "empty when the chain carries no Greeks.",
               "exposure", (None, None)),
        define("options_atm_theta_pct",
               "ATM strike's Theta as % of ATM premium — same-day time-decay "
               "burn rate; empty when the chain carries no Greeks.",
               "%", (0.0, 50.0)),
        define("options_atm_gamma",
               "ATM strike's raw Gamma (call + put) — where gamma risk "
               "peaks; empty when the chain carries no Greeks.",
               "exposure", (None, None)),
        define("options_atm_vega",
               "ATM strike's raw Vega (call + put) — IV-crush/spike "
               "sensitivity; empty when the chain carries no Greeks.",
               "exposure", (None, None)),
        define("options_volume_ratio", "Put volume / call volume (volume PCR).",
               "ratio", (0.0, 5.0)),
        define("options_expected_move",
               "One-day expected move implied by ATM IV: "
               "spot x IV/100 / sqrt(252).",
               "price", (0.0, None), ("options_atm_iv",)),
        define("options_dealer_positioning",
               "Put vs call writing balance in -1..1 (v1 heuristic; positive "
               "= put-writing dominance, structurally supportive).",
               "ratio", (-1.0, 1.0),
               ("options_call_writing_score", "options_put_writing_score")),
    ]
    definitions.extend(
        normalized_definition(d, normalization_window) for d in list(definitions)
    )
    return definitions


# --- Pure calculations -----------------------------------------------------------

def compute_options_features(
    snapshots: Sequence[ChainSnapshot],
    normalization_window: int = 100,
) -> dict[str, Series]:
    """Compute every options feature (raw + _z normalized) aligned to `snapshots`."""
    n = len(snapshots)

    def passthrough(feature: str) -> Series:
        return [snap.values.get(feature) for snap in snapshots]

    pcr = passthrough("pcr")
    atm_iv = passthrough("atm_iv")
    call_writing = passthrough("call_writing")
    put_writing = passthrough("put_writing")

    total_oi: Series = [None] * n
    max_pain_distance: Series = [None] * n
    volume_ratio: Series = [None] * n
    expected_move: Series = [None] * n
    dealer_positioning: Series = [None] * n

    for i, snap in enumerate(snapshots):
        pcr_meta = snap.metadata.get("pcr") or {}
        call_oi, put_oi = pcr_meta.get("total_call_oi"), pcr_meta.get("total_put_oi")
        if call_oi is not None and put_oi is not None and call_oi + put_oi > 0:
            total_oi[i] = float(call_oi) + float(put_oi)

        pain_meta = snap.metadata.get("max_pain") or {}
        spot = pain_meta.get("spot") or (snap.metadata.get("oi_distribution") or {}).get("spot")
        distance = pain_meta.get("distance_from_spot")
        if spot and distance is not None:
            max_pain_distance[i] = float(distance) / float(spot) * 100

        volume_meta = snap.metadata.get("volume_distribution") or {}
        if volume_meta.get("volume_pcr") is not None:
            volume_ratio[i] = float(volume_meta["volume_pcr"])

        iv_value = atm_iv[i]
        if spot and iv_value is not None:
            expected_move[i] = float(spot) * iv_value / 100 / math.sqrt(TRADING_DAYS)

        call_value, put_value = call_writing[i], put_writing[i]
        if call_value is not None and put_value is not None and call_value + put_value > 0:
            dealer_positioning[i] = (put_value - call_value) / (put_value + call_value)

    oi_change_pct: Series = [None] * n
    for i in range(1, n):
        current, previous = total_oi[i], total_oi[i - 1]
        if current is not None and previous is not None and previous > 0:
            oi_change_pct[i] = (current / previous - 1) * 100

    min_obs = max(10, normalization_window // 10)
    iv_rank: Series = [None] * n
    iv_percentile: Series = [None] * n
    for i in range(n):
        current_iv = atm_iv[i]
        if current_iv is None:
            continue
        trailing = [
            v for v in atm_iv[max(0, i - normalization_window + 1) : i + 1]
            if v is not None
        ]
        if len(trailing) < min_obs:
            continue
        low, high = min(trailing), max(trailing)
        if high > low:
            iv_rank[i] = (current_iv - low) / (high - low) * 100
        percentile = trailing_percentile(atm_iv, i, normalization_window, min_obs)
        if percentile is not None:
            iv_percentile[i] = percentile * 100

    out: dict[str, Series] = {
        "options_pcr": pcr,
        "options_oi_change_pct": oi_change_pct,
        "options_max_pain_distance_pct": max_pain_distance,
        "options_atm_iv": atm_iv,
        "options_iv_rank": iv_rank,
        "options_iv_percentile": iv_percentile,
        "options_call_writing_score": call_writing,
        "options_put_writing_score": put_writing,
        "options_gamma_exposure": passthrough("gamma_exposure"),
        "options_delta_exposure": passthrough("delta_exposure"),
        "options_atm_theta_pct": passthrough("atm_theta_pct"),
        "options_atm_gamma": passthrough("atm_gamma"),
        "options_atm_vega": passthrough("atm_vega"),
        "options_volume_ratio": volume_ratio,
        "options_expected_move": expected_move,
        "options_dealer_positioning": dealer_positioning,
    }
    return add_normalized_series(out, normalization_window)


# --- Engine -------------------------------------------------------------------------

class OptionsFeatureEngine(BaseFeatureEngine):
    name = ENGINE_NAME
    category = CATEGORY

    def _definitions(self) -> list[FeatureDefinition]:
        return options_feature_definitions(
            self._settings.feature_normalization_window,
            calculation_frequency=f"{self._settings.feature_engine_interval}s",
        )

    def _compute(
        self, candles: Sequence[Candle], benchmark: Sequence[Candle] | None = None
    ) -> dict[str, Series]:
        return {}  # options features live on chain-snapshot time, not bars

    async def run(self, symbol: str, timeframe: str = "D", full: bool = False) -> dict:
        """Options features ignore the bar timeframe — they live on chain
        snapshots under the synthetic timeframe "chain"."""
        observations = await self._load_labeled_observations(
            "options.observation", symbol, "feature",
            self._settings.feature_options_lookback,
        )
        snapshots = bucket_snapshots(observations)
        if len(snapshots) < 2:
            return {
                "symbol": symbol,
                "timeframe": OPTIONS_TIMEFRAME,
                "stored": 0,
                "skipped": True,
            }
        series = compute_options_features(
            snapshots, self._settings.feature_normalization_window
        )
        return await self._process_series(
            symbol, OPTIONS_TIMEFRAME, [s.ts for s in snapshots], series, full=full
        )
