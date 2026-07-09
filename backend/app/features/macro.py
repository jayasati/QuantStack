"""Macro Feature Engine (Volume 3 gap fill).

Chapter 2 of Volume 3 lists "Macro Features" as a category, but none of the
12 actual Volume 3 prompts (3.1-3.18) was ever assigned to build it — the
same gap Institutional Flow Intelligence (Prompt 4.5) hit. Prompt 4.8's
Cross-Asset Correlation Engine needs daily return series for USDINR, crude,
gold, and global equity indices, so this fills the gap.

Unlike Breadth/Sector/Institutional Flow (one market-wide "MARKET" instrument
labeled by metric), each macro factor is its own instrument — USDINR, CRUDE,
GOLD, SPX, ... — the same per-instrument shape Sector Features (Prompt 3.7)
uses, not the single-instrument-many-metrics shape. Transforms the
macro_intelligence collector's observations (market_events, event_type
macro.observation, one record per factor plus the MACRO_PRESSURE composite)
into versioned Feature Store features, one symbol per factor, under the
synthetic timeframe "macro".

Feature conventions:
- macro_score passes through the collector's signed [-1, 1] factor score
  (already sign-adjusted for India-equity impact — see collectors/domains/
  macro.py's FACTOR_SIGNS).
- macro_value, macro_return_1d_pct, and macro_zscore_20d are pulled from the
  record's metadata sidecar (value / change_1d_pct / zscore_20d) — the same
  "extra field lives in metadata" pattern Breadth/Sector/Institutional Flow
  Features use for their own sidecar fields.

Every feature ships a look-ahead-safe rolling z-score companion (_z).
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select

from app.core.logging import get_logger
from app.features.base import BaseFeatureEngine
from app.features.normalize import add_normalized_series, normalized_definition
from app.features.schema import Candle, FeatureDefinition, Series
from app.features.snapshots import Snapshot, bucket_observations

logger = get_logger(__name__)

ENGINE_NAME = "macro_feature_engine"
ENGINE_VERSION = "v1"
CATEGORY = "macro"

MACRO_TIMEFRAME = "macro"
MACRO_EVENT_TYPE = "macro.observation"


# --- Feature definitions -------------------------------------------------------

def macro_feature_definitions(
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
        define("macro_score",
               "Signed, India-equity-impact-adjusted factor score, -1..1.",
               "ratio", (-1.0, 1.0)),
        define("macro_value", "Raw price/level of the factor.",
               "price", (None, None)),
        define("macro_return_1d_pct", "1-day % change of the factor.",
               "%", (None, None)),
        define("macro_zscore_20d", "20-day z-score of the factor's level.",
               "zscore", (-4.0, 4.0)),
    ]
    definitions.extend(
        normalized_definition(d, normalization_window) for d in list(definitions)
    )
    return definitions


# --- Pure calculations -----------------------------------------------------------

def compute_macro_factor_features(
    snapshots: Sequence[Snapshot],
    factor: str,
    normalization_window: int = 100,
) -> dict[str, Series]:
    """Compute one factor's feature series (raw + _z) aligned to `snapshots`."""
    n = len(snapshots)
    score: Series = [None] * n
    value: Series = [None] * n
    return_1d: Series = [None] * n
    zscore_20d: Series = [None] * n

    for i, snap in enumerate(snapshots):
        score[i] = snap.values.get(factor)
        meta = snap.metadata.get(factor) or {}
        if meta.get("value") is not None:
            value[i] = float(meta["value"])
        if meta.get("change_1d_pct") is not None:
            return_1d[i] = float(meta["change_1d_pct"])
        if meta.get("zscore_20d") is not None:
            zscore_20d[i] = float(meta["zscore_20d"])

    out: dict[str, Series] = {
        "macro_score": score,
        "macro_value": value,
        "macro_return_1d_pct": return_1d,
        "macro_zscore_20d": zscore_20d,
    }
    return add_normalized_series(out, normalization_window)


# --- Engine -------------------------------------------------------------------------

class MacroFeatureEngine(BaseFeatureEngine):
    name = ENGINE_NAME
    category = CATEGORY

    def _definitions(self) -> list[FeatureDefinition]:
        return macro_feature_definitions(
            self._settings.feature_normalization_window,
            calculation_frequency=f"{self._settings.feature_engine_interval}s",
        )

    def _compute(
        self, candles: Sequence[Candle], benchmark: Sequence[Candle] | None = None
    ) -> dict[str, Series]:
        return {}  # macro features live on collector-run time, not bars

    async def run(
        self, symbol: str = "MACRO_PRESSURE", timeframe: str = "D", full: bool = False
    ) -> dict:
        """Macro covers every factor at once — the symbol/timeframe arguments
        are ignored in favor of the factor universe found in the data."""
        rows = await self._load_macro_observations()
        snapshots = bucket_observations(rows)
        if len(snapshots) < 2:
            return {
                "symbol": symbol,
                "timeframe": MACRO_TIMEFRAME,
                "stored": 0,
                "skipped": True,
            }
        factors = sorted({name for snap in snapshots for name in snap.values})
        stored = 0
        online = 0
        for factor in factors:
            series = compute_macro_factor_features(
                snapshots, factor, self._settings.feature_normalization_window
            )
            result = await self._process_series(
                factor, MACRO_TIMEFRAME, [s.ts for s in snapshots], series, full=full
            )
            stored += result["stored"]
            online += result["online_entries"]
        return {
            "symbol": "MACRO",
            "timeframe": MACRO_TIMEFRAME,
            "factors": len(factors),
            "stored": stored,
            "online_entries": online,
        }

    async def run_all(self) -> list[dict]:
        """One run covering every factor — the watchlist does not apply here."""
        try:
            return [await self.run()]
        except Exception as exc:
            logger.error("macro feature run failed", extra={"error": str(exc)})
            return [{"symbol": "MACRO", "error": str(exc)}]

    async def _load_macro_observations(
        self,
    ) -> list[tuple[datetime, str, float | None, dict[str, Any]]]:
        """Macro observations labeled by their instrument (the factor name)."""
        if self._sessions is None:
            return []
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            result = await session.execute(
                select(MarketEvent.data)
                .where(MarketEvent.event_type == MACRO_EVENT_TYPE)
                .order_by(desc(MarketEvent.id))
                .limit(self._settings.feature_macro_lookback)
            )
            rows = result.scalars().all()
        observations: list[tuple[datetime, str, float | None, dict[str, Any]]] = []
        for data in reversed(rows):
            if not data:
                continue
            label = data.get("instrument")
            ts_raw = data.get("timestamp")
            if not label or not ts_raw:
                continue
            try:
                ts = datetime.fromisoformat(ts_raw)
            except (TypeError, ValueError):
                continue
            value = data.get("normalized_value")
            observations.append(
                (
                    ts,
                    label,
                    float(value) if value is not None else None,
                    data.get("metadata") or {},
                )
            )
        return observations
