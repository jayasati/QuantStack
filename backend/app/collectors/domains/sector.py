"""Sector rotation collector (Volume 2, Chapter 11, Prompt 2.6).

Tracks every NSE sector against the benchmark index and computes relative
strength, relative momentum, relative volume, rolling performance and a
composite Sector Rotation Score. Emits one record per sector plus a summary
record carrying the sector heatmap, the leading sector and the weakening
sector.

Real market data must be injected through a :class:`SectorSource`; the default
:class:`UnconfiguredSectorSource` refuses to run rather than fabricate data.
"""

import math
from abc import ABC, abstractmethod
from typing import Any

from app.collectors.base import BaseCollector, CollectionError
from app.collectors.schema import CollectorCategory, CollectorOutput, Direction

# The twelve tracked sectors, each backed by a real NSE sectoral index
# available through the broker (see sources/broker_sectors.py). NSE publishes
# no Capital Goods or Defence index in the broker universe, so PSU Bank and
# Private Bank complete the twelve instead.
NSE_SECTORS: tuple[str, ...] = (
    "Banking",
    "IT",
    "Auto",
    "Energy",
    "Pharma",
    "FMCG",
    "PSU",
    "PSU Bank",
    "Private Bank",
    "Realty",
    "Metal",
    "Infrastructure",
)

_REQUIRED_FIELDS: tuple[str, ...] = ("return_1d", "return_5d", "return_20d", "volume_ratio")

# Multi-window blend weights for relative strength (short bias, decaying).
_RS_WEIGHTS: dict[str, float] = {"return_1d": 0.5, "return_5d": 0.3, "return_20d": 0.2}

# Momentum threshold (percentage points) below which direction is neutral.
_MOMENTUM_EPS = 0.05


class SectorSource(ABC):
    """Async provider of sector and benchmark window returns.

    ``fetch_sectors`` must return::

        {
            "benchmark": {return_1d, return_5d, return_20d, volume_ratio},
            "sectors": {name: {return_1d, return_5d, return_20d, volume_ratio}},
        }

    with returns expressed in percentage points over each window.
    """

    @abstractmethod
    async def fetch_sectors(self) -> dict:
        """Return benchmark and per-sector window metrics."""


class UnconfiguredSectorSource(SectorSource):
    """Fail-safe default: refuses to run instead of fabricating market data."""

    async def fetch_sectors(self) -> dict:
        raise CollectionError("sector source not configured")


def _require_metrics(entry: Any, label: str) -> dict[str, float]:
    """Extract the required numeric fields or fail — never fabricate values."""
    if not isinstance(entry, dict):
        raise CollectionError(f"sector payload for {label!r} is not a mapping")
    metrics: dict[str, float] = {}
    for field in _REQUIRED_FIELDS:
        value = entry.get(field)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise CollectionError(f"sector payload for {label!r} missing numeric {field!r}")
        metrics[field] = float(value)
    return metrics


class SectorRotationCollector(BaseCollector):
    """Compute relative strength / momentum / volume and a rotation score per sector."""

    name = "sector_rotation"
    category = CollectorCategory.SECTOR
    source = "sector_source"
    interval_seconds = 60
    priority = 15

    def __init__(self, sector_source: SectorSource | None = None) -> None:
        super().__init__()
        if sector_source is None:
            from app.collectors.sources.broker_sectors import BrokerSectorSource

            sector_source = BrokerSectorSource()
        self._source: SectorSource = sector_source

    async def collect(self) -> list[CollectorOutput]:
        payload = await self._source.fetch_sectors()
        if not isinstance(payload, dict):
            raise CollectionError("sector source returned a non-mapping payload")

        benchmark = _require_metrics(payload.get("benchmark"), "benchmark")
        if benchmark["volume_ratio"] <= 0:
            raise CollectionError("benchmark volume_ratio must be positive")

        raw_sectors = payload.get("sectors")
        if not isinstance(raw_sectors, dict):
            raise CollectionError("sector payload missing 'sectors' mapping")
        missing = [name for name in NSE_SECTORS if name not in raw_sectors]
        if missing:
            raise CollectionError(f"sector payload missing sectors: {', '.join(missing)}")

        analytics = {
            name: self._analyze(_require_metrics(raw_sectors[name], name), benchmark)
            for name in NSE_SECTORS
        }

        heatmap = {name: stats["rotation_score"] for name, stats in analytics.items()}
        leader = max(heatmap, key=lambda name: heatmap[name])
        laggard = min(heatmap, key=lambda name: heatmap[name])

        records = [
            CollectorOutput(
                collector_name=self.name,
                collector_category=self.category,
                source=self.source,
                instrument=name,
                raw_value=raw_sectors[name],
                normalized_value=stats["rotation_score"],
                direction=self._direction(stats["relative_momentum"]),
                confidence=0.9,
                metadata={
                    "relative_strength": stats["relative_strength"],
                    "relative_momentum": stats["relative_momentum"],
                    "relative_volume": stats["relative_volume"],
                    "rolling_performance": stats["rolling_performance"],
                    "capital_rotation": stats["capital_rotation"],
                    "is_leader": name == leader,
                    "is_laggard": name == laggard,
                },
            )
            for name, stats in analytics.items()
        ]

        rotation_intensity = sum(
            abs(stats["relative_momentum"]) for stats in analytics.values()
        ) / len(analytics)
        records.append(
            CollectorOutput(
                collector_name=self.name,
                collector_category=self.category,
                source=self.source,
                instrument="SECTORS",
                raw_value=payload,
                normalized_value=heatmap[leader],
                direction=self._direction(analytics[leader]["relative_momentum"]),
                confidence=0.9,
                metadata={
                    "heatmap": heatmap,
                    "leader": leader,
                    "laggard": laggard,
                    "rotation_intensity": rotation_intensity,
                    "benchmark": benchmark,
                },
            )
        )
        return records

    @staticmethod
    def _analyze(sector: dict[str, float], benchmark: dict[str, float]) -> dict[str, float]:
        """Derive relative metrics and the composite rotation score for one sector."""
        rs = {window: sector[window] - benchmark[window] for window in _RS_WEIGHTS}
        relative_strength = sum(_RS_WEIGHTS[window] * rs[window] for window in _RS_WEIGHTS)
        relative_momentum = rs["return_1d"] - rs["return_20d"]
        relative_volume = sector["volume_ratio"] / benchmark["volume_ratio"]
        rolling_performance = (
            sector["return_1d"] + sector["return_5d"] + sector["return_20d"]
        ) / 3.0
        capital_rotation = relative_momentum * relative_volume
        raw = 0.45 * relative_strength + 0.35 * relative_momentum + (relative_volume - 1.0)
        rotation_score = 50.0 * (1.0 + math.tanh(raw / 5.0))
        return {
            "relative_strength": relative_strength,
            "relative_momentum": relative_momentum,
            "relative_volume": relative_volume,
            "rolling_performance": rolling_performance,
            "capital_rotation": capital_rotation,
            "rotation_score": rotation_score,
        }

    @staticmethod
    def _direction(relative_momentum: float) -> Direction:
        if relative_momentum > _MOMENTUM_EPS:
            return Direction.BULLISH
        if relative_momentum < -_MOMENTUM_EPS:
            return Direction.BEARISH
        return Direction.NEUTRAL
