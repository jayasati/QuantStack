"""Shared contract for market intelligence components (Volume 4, Chapter 2).

Every intelligence engine consumes the Feature Store and produces an
IntelligenceResult: a normalized 0-100 score, a confidence in that score,
probabilistic regime states (never hard labels — Chapter 15's philosophy
applies everywhere), the metrics behind it, and a built-in explanation
(contributing features, weights, reasoning chain) so no score is ever a
black box (Chapter 20).

Components read the latest features from the online store (offline fallback)
and their history from the offline store; they never touch collectors —
the Feature Store is the single source of truth (Volume 3, Chapter 1).
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.cache import CacheService
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.features.store import FeatureStore

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession] | async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class Contribution:
    """One feature's contribution to a score — the explainability unit."""

    feature: str
    value: float | None
    weight: float
    effect: str  # e.g. "bullish", "bearish", "raises score", "lowers confidence"


@dataclass
class IntelligenceResult:
    component: str
    score: float  # 0-100, 50 = neutral
    confidence: float  # 0-1: how much this assessment can be trusted
    states: dict[str, float] = field(default_factory=dict)  # regime -> probability
    metrics: dict[str, Any] = field(default_factory=dict)
    contributions: list[Contribution] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    as_of: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["as_of"] = self.as_of.isoformat()
        payload["score"] = round(self.score, 2)
        payload["confidence"] = round(self.confidence, 4)
        payload["states"] = {k: round(v, 4) for k, v in self.states.items()}
        return payload


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def normalize_states(raw: dict[str, float]) -> dict[str, float]:
    """Turn non-negative evidence weights into a probability distribution."""
    total = sum(max(v, 0.0) for v in raw.values())
    if total <= 0:
        return {k: 1.0 / len(raw) for k in raw} if raw else {}
    return {k: max(v, 0.0) / total for k, v in raw.items()}


class IntelligenceComponent:
    name = "intelligence_component"

    def __init__(
        self,
        session_factory: SessionFactory | None = None,
        cache: CacheService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._sessions = session_factory
        self.store = FeatureStore(session_factory=session_factory, cache=cache)

    async def latest_values(self, symbol: str, timeframe: str) -> dict[str, float]:
        """Latest feature values flattened to {feature_name: value}."""
        latest = await self.store.latest(symbol, timeframe)
        return {
            name: entry["value"]
            for name, entry in latest.items()
            if isinstance(entry, dict) and entry.get("value") is not None
        }

    async def feature_history(
        self, feature_name: str, symbol: str, timeframe: str, limit: int = 200
    ) -> list[float]:
        """Recent values of one feature, oldest first."""
        rows = await self.store.history(
            feature_name, symbol=symbol, timeframe=timeframe, limit=limit
        )
        return [row["value"] for row in reversed(rows)]

    async def assess(self) -> IntelligenceResult:
        raise NotImplementedError
