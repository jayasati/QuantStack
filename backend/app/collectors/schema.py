"""Standard collector output schema (Volume 2, Chapter 5).

Every collector emits this exact structure so downstream components stay
completely agnostic to the original data source.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Direction(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class CollectorCategory(StrEnum):
    MARKET_DATA = "market_data"
    OPTIONS = "options"
    BREADTH = "breadth"
    SECTOR = "sector"
    MACRO = "macro"
    ECONOMIC_CALENDAR = "economic_calendar"
    CORPORATE_ACTIONS = "corporate_actions"
    ORDER_FLOW = "order_flow"
    NEWS = "news"
    SENTIMENT = "sentiment"
    INSTITUTIONAL_FLOW = "institutional_flow"
    GLOBAL_MARKETS = "global_markets"
    VOLATILITY = "volatility"
    COMMODITIES = "commodities"
    CURRENCY = "currency"
    GOVERNMENT = "government"
    ALTERNATIVE = "alternative"
    TECHNICAL_STRUCTURE = "technical_structure"
    LIQUIDITY = "liquidity"
    RISK_EVENTS = "risk_events"


class CollectorOutput(BaseModel):
    """One normalized observation emitted by a collector."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    collector_name: str
    collector_category: CollectorCategory
    source: str
    instrument: str = "MARKET"
    exchange: str = "NSE"
    raw_value: Any = None
    normalized_value: float | None = None
    direction: Direction = Direction.UNKNOWN
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    quality_score: float | None = Field(default=None, ge=0.0, le=100.0)
    latency_ms: float | None = None
    freshness_seconds: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
