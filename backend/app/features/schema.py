"""Feature layer data contracts (Volume 3, Chapters 4-6)."""

from dataclasses import dataclass
from datetime import datetime

# A feature computed over candles: one value per bar, None during cold start.
Series = list[float | None]


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
    """One observation of one feature — every feature is stored independently.

    `collector_version` and `feature_quality_score` (data foundation audit
    2026-07-17, feature-row metadata item) complete the mandate's per-row
    metadata contract alongside the always-present `ts`/`symbol`/
    `feature_version` -- `last_updated` is deliberately NOT a field here,
    since it's a store-write-time concept (when this row was last
    persisted), not a compute-time one; `FeatureStore._write_offline` stamps
    it directly. `collector_version` names the feature ENGINE code version
    that produced this row (`BaseFeatureEngine.engine_version`) -- distinct
    from `feature_version`, which is the calculation's own semantic version
    (Chapter 6). `feature_quality_score` is attached via `dataclasses.replace`
    after `_quality_check()` runs (it's computed per-batch, after values are
    first built) -- both default to values that keep every existing
    construction site (only `base.py`'s `build_values_at`, confirmed by
    grep) working unchanged."""

    feature_name: str
    feature_version: str
    symbol: str
    timeframe: str
    ts: datetime
    value: float
    window: int | None = None
    collector_version: str = "1.0.0"
    feature_quality_score: float | None = None
