"""Initial database schema — the 20 foundation tables.

Volume 1 creates these tables empty; the business logic that populates them
arrives in later volumes. Every table gets a surrogate id, a created_at
timestamp, and a JSONB payload column so early volumes can persist structured
records before their final schemas are specified. Domain-specific columns are
added by later volumes through Alembic migrations — never by editing history.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, MetaData, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


# --- Collection & health -----------------------------------------------------

class Collector(Base):
    __tablename__ = "collectors"
    name: Mapped[str] = mapped_column(String(100), unique=True)
    version: Mapped[str] = mapped_column(String(20), default="1.0.0")
    enabled: Mapped[bool] = mapped_column(default=True)


class CollectorHealth(Base):
    __tablename__ = "collector_health"
    collector_name: Mapped[str] = mapped_column(String(100), index=True)
    quality_score: Mapped[float | None] = mapped_column(nullable=True)


class MarketEvent(Base):
    __tablename__ = "market_events"
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    source: Mapped[str] = mapped_column(String(100))


# --- Features (Volume 3) -------------------------------------------------------

class FeatureStoreRow(Base):
    """Offline feature store: one row per feature observation (Prompt 3.1)."""

    __tablename__ = "feature_store"
    __table_args__ = (
        UniqueConstraint(
            "feature_name", "feature_version", "symbol", "timeframe", "ts",
            name="uq_feature_store_identity",
        ),
    )
    feature_name: Mapped[str] = mapped_column(String(200), index=True)
    feature_version: Mapped[str] = mapped_column(String(20), default="v1")
    symbol: Mapped[str] = mapped_column(String(50), index=True)
    timeframe: Mapped[str] = mapped_column(String(10))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    value: Mapped[float] = mapped_column()
    window_size: Mapped[int | None] = mapped_column(nullable=True)


class FeatureVersion(Base):
    __tablename__ = "feature_versions"
    __table_args__ = (
        UniqueConstraint("feature_name", "version", name="uq_feature_versions_identity"),
    )
    feature_name: Mapped[str] = mapped_column(String(200), index=True)
    version: Mapped[str] = mapped_column(String(20))
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)


class FeatureRegistryRow(Base):
    """Master list of registered features and their Chapter 5 metadata."""

    __tablename__ = "feature_registry"
    feature_name: Mapped[str] = mapped_column(String(200), unique=True)
    category: Mapped[str] = mapped_column(String(50), index=True)
    description: Mapped[str] = mapped_column(String(500))
    version: Mapped[str] = mapped_column(String(20))
    calculation_frequency: Mapped[str] = mapped_column(String(50))
    owner: Mapped[str] = mapped_column(String(100))
    quality_threshold: Mapped[float] = mapped_column(default=0.0)
    unit: Mapped[str] = mapped_column(String(50))
    expected_min: Mapped[float | None] = mapped_column(nullable=True)
    expected_max: Mapped[float | None] = mapped_column(nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True)


class FeatureDependencyRow(Base):
    """Edges of the feature dependency graph (Chapter 7)."""

    __tablename__ = "feature_dependencies"
    __table_args__ = (
        UniqueConstraint("feature_name", "depends_on", name="uq_feature_dependencies_edge"),
    )
    feature_name: Mapped[str] = mapped_column(String(200), index=True)
    depends_on: Mapped[str] = mapped_column(String(200), index=True)


class FeatureQualityRow(Base):
    __tablename__ = "feature_quality"
    feature_name: Mapped[str] = mapped_column(String(200), index=True)
    symbol: Mapped[str | None] = mapped_column(String(50), nullable=True)
    timeframe: Mapped[str | None] = mapped_column(String(10), nullable=True)
    quality_score: Mapped[float] = mapped_column()
    sample_count: Mapped[int] = mapped_column(default=0)


class FeatureStatisticRow(Base):
    __tablename__ = "feature_statistics"
    feature_name: Mapped[str] = mapped_column(String(200), index=True)
    symbol: Mapped[str | None] = mapped_column(String(50), nullable=True)
    timeframe: Mapped[str | None] = mapped_column(String(10), nullable=True)
    mean: Mapped[float | None] = mapped_column(nullable=True)
    std: Mapped[float | None] = mapped_column(nullable=True)
    min_value: Mapped[float | None] = mapped_column(nullable=True)
    max_value: Mapped[float | None] = mapped_column(nullable=True)
    sample_count: Mapped[int] = mapped_column(default=0)


class FeatureDriftRow(Base):
    __tablename__ = "feature_drift"
    feature_name: Mapped[str] = mapped_column(String(200), index=True)
    metric: Mapped[str] = mapped_column(String(50))
    value: Mapped[float] = mapped_column()
    threshold: Mapped[float] = mapped_column()
    breached: Mapped[bool] = mapped_column(default=False)


class FeatureUsageRow(Base):
    __tablename__ = "feature_usage"
    __table_args__ = (
        UniqueConstraint(
            "feature_name", "consumer", "symbol", "timeframe", name="uq_feature_usage_edge"
        ),
    )
    feature_name: Mapped[str] = mapped_column(String(200), index=True)
    consumer: Mapped[str] = mapped_column(String(100))
    # Real columns, not JSONB-only -- a prior version kept symbol/timeframe
    # only inside `data`, so the unique edge above was (feature_name,
    # consumer) alone and two symbols recommending the same feature name
    # silently overwrote each other's row (migration 0006).
    symbol: Mapped[str] = mapped_column(String(50), index=True)
    timeframe: Mapped[str] = mapped_column(String(10))


# --- Market intelligence -----------------------------------------------------

class MarketRegime(Base):
    __tablename__ = "market_regime"
    regime: Mapped[str] = mapped_column(String(50), index=True)
    probability: Mapped[float | None] = mapped_column(nullable=True)


class RegimeWeights(Base):
    __tablename__ = "regime_weights"
    regime: Mapped[str] = mapped_column(String(50), index=True)


class BreadthMetrics(Base):
    __tablename__ = "breadth_metrics"


class SectorRotation(Base):
    __tablename__ = "sector_rotation"
    sector: Mapped[str | None] = mapped_column(String(50), index=True, nullable=True)


class RelativeStrength(Base):
    __tablename__ = "relative_strength"
    symbol: Mapped[str | None] = mapped_column(String(50), index=True, nullable=True)


class MarketStructure(Base):
    __tablename__ = "market_structure"
    symbol: Mapped[str | None] = mapped_column(String(50), index=True, nullable=True)


class EventRisk(Base):
    __tablename__ = "event_risk"
    event_name: Mapped[str | None] = mapped_column(String(200), nullable=True)


# --- Prediction & signals ----------------------------------------------------

class PredictionResult(Base):
    __tablename__ = "prediction_results"
    symbol: Mapped[str | None] = mapped_column(String(50), index=True, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(50), nullable=True)


class SignalQuality(Base):
    __tablename__ = "signal_quality"
    grade: Mapped[str | None] = mapped_column(String(10), nullable=True)


class TradeSignal(Base):
    __tablename__ = "trade_signals"
    symbol: Mapped[str | None] = mapped_column(String(50), index=True, nullable=True)
    direction: Mapped[str | None] = mapped_column(String(10), nullable=True)


class TradeLog(Base):
    __tablename__ = "trade_log"
    signal_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(50), nullable=True)


# --- Models & learning -------------------------------------------------------

class ModelVersion(Base):
    __tablename__ = "model_versions"
    model_name: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    version: Mapped[str | None] = mapped_column(String(50), nullable=True)


class RetrainingRun(Base):
    __tablename__ = "retraining_runs"
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)


# --- Operations ----------------------------------------------------------------

class SystemMetric(Base):
    __tablename__ = "system_metrics"
    metric_name: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    value: Mapped[float | None] = mapped_column(nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    actor: Mapped[str | None] = mapped_column(String(100), nullable=True)
    action: Mapped[str | None] = mapped_column(String(200), nullable=True)


# --- Market data time series (Volume 2) ---------------------------------------

class OhlcvCandle(Base):
    __tablename__ = "ohlcv_candles"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "ts", name="uq_ohlcv_symbol_tf_ts"),
    )
    symbol: Mapped[str] = mapped_column(String(50), index=True)
    timeframe: Mapped[str] = mapped_column(String(10))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[float] = mapped_column()
    high: Mapped[float] = mapped_column()
    low: Mapped[float] = mapped_column()
    close: Mapped[float] = mapped_column()
    volume: Mapped[int] = mapped_column(BigInteger, default=0)


class RawTick(Base):
    __tablename__ = "raw_ticks"
    symbol: Mapped[str] = mapped_column(String(50), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ltp: Mapped[float] = mapped_column()
