"""Sector Feature Engine (Volume 3, Prompt 3.7).

Transforms the sector_rotation collector's observations (market_events,
event_type sector.observation) into versioned Feature Store features on
collector-run snapshot time. Per-sector features store with the sector name
as the symbol (Banking, IT, ...); market-wide features store under the
summary symbol SECTORS. Everything lives under the synthetic timeframe
"sector".

Feature conventions:
- Sector Relative Strength, Sector Momentum, and Capital Rotation Score pass
  through the collector's benchmark-relative metrics (multi-window blended
  RS, 1d-vs-20d momentum spread, and momentum x relative volume).
- Heat Score is the collector's composite 0-100 rotation score.
- Sector Leadership is the cross-sectional z-score of a sector's heat within
  that snapshot — continuous leadership, not just a leader flag.
- Winning Sector Rank ranks sectors by heat per snapshot (1 = strongest).
- Sector Correlation is the rolling correlation of a sector's relative
  strength against the cross-sector mean over the window — low correlation
  marks genuine rotation candidates.
- Sector Rotation Index (market-wide) passes through the collector's
  rotation intensity: the mean absolute relative momentum across sectors.
- Sector Participation (market-wide) is the share of sectors outperforming
  the benchmark (positive relative strength), in %.

Every feature ships a look-ahead-safe rolling z-score companion (_z).
"""

from collections.abc import Sequence
from datetime import datetime
from statistics import fmean, pstdev

from sqlalchemy import desc, select

from app.core.logging import get_logger
from app.features.base import BaseFeatureEngine
from app.features.normalize import (
    add_normalized_series,
    normalized_definition,
    rolling_correlation,
)
from app.features.schema import Candle, FeatureDefinition, Series
from app.features.snapshots import Snapshot, bucket_observations

logger = get_logger(__name__)

ENGINE_NAME = "sector_feature_engine"
ENGINE_VERSION = "v1"
CATEGORY = "sector"

SECTORS_SYMBOL = "SECTORS"
SECTOR_TIMEFRAME = "sector"


# --- Feature definitions -------------------------------------------------------

def sector_feature_definitions(
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
        define("sector_relative_strength",
               "Benchmark-relative strength, multi-window blend in % points.",
               "%", (-20.0, 20.0)),
        define("sector_momentum",
               "Relative momentum: 1d minus 20d benchmark-relative return.",
               "%", (-20.0, 20.0)),
        define("sector_capital_rotation",
               "Relative momentum x relative volume — capital moving in/out.",
               "score", (-100.0, 100.0)),
        define("sector_heat_score",
               "Composite 0-100 rotation score from the sector collector.",
               "index", (0.0, 100.0)),
        define("sector_leadership",
               "Cross-sectional z-score of the sector's heat within the "
               "snapshot (positive = leading).",
               "zscore", (-4.0, 4.0), ("sector_heat_score",)),
        define("sector_rank",
               "Rank of the sector by heat within the snapshot (1 = strongest).",
               "rank", (1.0, None), ("sector_heat_score",)),
        define("sector_rotation_index",
               "Mean absolute relative momentum across sectors (market-wide, "
               "symbol SECTORS).",
               "%", (0.0, 20.0)),
        define("sector_participation_pct",
               "Share of sectors outperforming the benchmark, in % "
               "(market-wide, symbol SECTORS).",
               "%", (0.0, 100.0), ("sector_relative_strength",)),
    ]
    for w in windows:
        definitions.append(
            define(f"sector_correlation_{w}",
                   f"Correlation of the sector's relative strength vs the "
                   f"cross-sector mean over {w} snapshots.",
                   "ratio", (-1.0, 1.0), ("sector_relative_strength",), w)
        )
    definitions.extend(
        normalized_definition(d, normalization_window) for d in list(definitions)
    )
    return definitions


# --- Pure calculations -----------------------------------------------------------

def compute_sector_features(
    snapshots: Sequence[Snapshot],
    windows: Sequence[int] = (5, 10, 20, 50, 100, 200),
    normalization_window: int = 100,
) -> tuple[dict[str, dict[str, Series]], dict[str, Series]]:
    """Compute per-sector and market-wide series aligned to `snapshots`.

    Returns ({sector: {feature: series}}, {market_feature: series}).
    """
    n = len(snapshots)
    sectors = sorted(
        {name for snap in snapshots for name in snap.values if name != SECTORS_SYMBOL}
    )

    strength: dict[str, Series] = {s: [None] * n for s in sectors}
    momentum: dict[str, Series] = {s: [None] * n for s in sectors}
    capital: dict[str, Series] = {s: [None] * n for s in sectors}
    heat: dict[str, Series] = {s: [None] * n for s in sectors}
    leadership: dict[str, Series] = {s: [None] * n for s in sectors}
    rank: dict[str, Series] = {s: [None] * n for s in sectors}

    rotation_index: Series = [None] * n
    participation: Series = [None] * n
    mean_strength: Series = [None] * n

    for i, snap in enumerate(snapshots):
        for s in sectors:
            meta = snap.metadata.get(s) or {}
            for target, key in (
                (strength, "relative_strength"),
                (momentum, "relative_momentum"),
                (capital, "capital_rotation"),
            ):
                value = meta.get(key)
                if value is not None:
                    target[s][i] = float(value)
            heat_value = snap.values.get(s)
            if heat_value is not None:
                heat[s][i] = heat_value

        # Cross-sectional leadership and rank from this snapshot's heats.
        heats = {s: h for s in sectors if (h := heat[s][i]) is not None}
        if len(heats) >= 2:
            mean_heat = fmean(heats.values())
            std_heat = pstdev(heats.values())
            if std_heat > 0:
                for s, h in heats.items():
                    leadership[s][i] = (h - mean_heat) / std_heat
            ordered = sorted(heats, key=lambda s: (-heats[s], s))
            for position, s in enumerate(ordered, start=1):
                rank[s][i] = float(position)

        strengths = [v for s in sectors if (v := strength[s][i]) is not None]
        if strengths:
            mean_strength[i] = fmean(strengths)
            participation[i] = sum(1 for v in strengths if v > 0) / len(strengths) * 100

        summary_meta = snap.metadata.get(SECTORS_SYMBOL) or {}
        intensity = summary_meta.get("rotation_intensity")
        if intensity is not None:
            rotation_index[i] = float(intensity)

    per_sector: dict[str, dict[str, Series]] = {}
    for s in sectors:
        series: dict[str, Series] = {
            "sector_relative_strength": strength[s],
            "sector_momentum": momentum[s],
            "sector_capital_rotation": capital[s],
            "sector_heat_score": heat[s],
            "sector_leadership": leadership[s],
            "sector_rank": rank[s],
        }
        for w in windows:
            series[f"sector_correlation_{w}"] = rolling_correlation(
                strength[s], mean_strength, w
            )
        per_sector[s] = add_normalized_series(series, normalization_window)

    market = add_normalized_series(
        {
            "sector_rotation_index": rotation_index,
            "sector_participation_pct": participation,
        },
        normalization_window,
    )
    return per_sector, market


# --- Engine -------------------------------------------------------------------------

class SectorFeatureEngine(BaseFeatureEngine):
    name = ENGINE_NAME
    category = CATEGORY

    def _definitions(self) -> list[FeatureDefinition]:
        return sector_feature_definitions(
            self.windows,
            self._settings.feature_normalization_window,
            calculation_frequency=f"{self._settings.feature_engine_interval}s",
        )

    def _compute(
        self, candles: Sequence[Candle], benchmark: Sequence[Candle] | None = None
    ) -> dict[str, Series]:
        return {}  # sector features live on collector-run time, not bars

    async def run(
        self, symbol: str = SECTORS_SYMBOL, timeframe: str = "D", full: bool = False
    ) -> dict:
        """Sector features cover every sector at once — the symbol/timeframe
        arguments are ignored in favor of the sector universe."""
        observations = await self._load_sector_observations()
        snapshots = bucket_observations(observations)
        if len(snapshots) < 2:
            return {
                "symbol": SECTORS_SYMBOL,
                "timeframe": SECTOR_TIMEFRAME,
                "stored": 0,
                "skipped": True,
            }
        per_sector, market = compute_sector_features(
            snapshots, self.windows, self._settings.feature_normalization_window
        )
        timestamps = [s.ts for s in snapshots]
        stored = 0
        online = 0
        for sector, series in per_sector.items():
            result = await self._process_series(
                sector, SECTOR_TIMEFRAME, timestamps, series, full=full
            )
            stored += result["stored"]
            online += result["online_entries"]
        market_result = await self._process_series(
            SECTORS_SYMBOL, SECTOR_TIMEFRAME, timestamps, market, full=full
        )
        stored += market_result["stored"]
        online += market_result["online_entries"]
        return {
            "symbol": SECTORS_SYMBOL,
            "timeframe": SECTOR_TIMEFRAME,
            "sectors": len(per_sector),
            "stored": stored,
            "online_entries": online,
        }

    async def run_all(self) -> list[dict]:
        """One universe-wide run — the watchlist does not apply here."""
        try:
            return [await self.run()]
        except Exception as exc:
            logger.error("sector feature run failed", extra={"error": str(exc)})
            return [{"symbol": SECTORS_SYMBOL, "error": str(exc)}]

    async def _load_sector_observations(
        self,
    ) -> list[tuple[datetime, str, float | None, dict]]:
        """Sector observations labeled by their instrument (sector name)."""
        if self._sessions is None:
            return []
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            result = await session.execute(
                select(MarketEvent.data)
                .where(MarketEvent.event_type == "sector.observation")
                .order_by(desc(MarketEvent.id))
                .limit(self._settings.feature_sector_lookback)
            )
            rows = result.scalars().all()
        observations: list[tuple[datetime, str, float | None, dict]] = []
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
