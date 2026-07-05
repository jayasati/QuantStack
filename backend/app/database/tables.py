"""Initial database schema — the 20 foundation tables.

Volume 1 creates these tables empty; the business logic that populates them
arrives in later volumes. Every table gets a surrogate id, a created_at
timestamp, and a JSONB payload column so early volumes can persist structured
records before their final schemas are specified. Domain-specific columns are
added by later volumes through Alembic migrations — never by editing history.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, MetaData, String, func
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


# --- Features ----------------------------------------------------------------

class FeatureStoreRow(Base):
    __tablename__ = "feature_store"
    feature_name: Mapped[str] = mapped_column(String(200), index=True)
    feature_version: Mapped[str] = mapped_column(String(20), default="v1")


class FeatureVersion(Base):
    __tablename__ = "feature_versions"
    feature_name: Mapped[str] = mapped_column(String(200), index=True)
    version: Mapped[str] = mapped_column(String(20))


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
