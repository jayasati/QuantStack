"""Feature layer data contracts (Volume 3, Chapters 4-6)."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Candle:
    """Minimal OHLCV bar the feature engines compute from."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


@dataclass(frozen=True)
class FeatureDefinition:
    """Chapter 5 registry metadata. Every feature registers this — never hardcode."""

    feature_name: str
    category: str
    description: str
    version: str = "v1"
    dependencies: tuple[str, ...] = ()
    calculation_frequency: str = "on_schedule"
    owner: str = "feature_engine"
    quality_threshold: float = 80.0
    unit: str = "value"
    # (min, max) sanity bounds; None means unbounded on that side.
    expected_range: tuple[float | None, float | None] = (None, None)
    window: int | None = None


@dataclass(frozen=True)
class FeatureValue:
    """One observation of one feature — every feature is stored independently."""

    feature_name: str
    feature_version: str
    symbol: str
    timeframe: str
    ts: datetime
    value: float
    window: int | None = None
