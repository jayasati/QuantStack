"""Explainability Layer (Volume 4, Prompt 4.16).

Every component already carries contributing features, their weights, and
a human-readable reasoning chain on IntelligenceResult — Chapter 2's
contract, in place since Prompt 4.1. What's missing is persistence (none
of that is ever stored anywhere; a caller only ever sees it in the moment
a score is computed) and a genuine confidence INTERVAL (every component so
far only produces a point confidence, 0-1).

Both are added here as a generic wrapper around any component's already-
computed IntelligenceResult, not duplicated once per component:
compute_confidence_interval() widens the interval around the score as
confidence falls (a low-confidence score is a wide estimate, not a precise
one — MAX_MARGIN points at zero confidence, a heuristic scale like
elsewhere in this layer), and ExplainabilityStore persists the full record
the same event-sourcing way every other persisted Volume 4 component does
(Bayesian Regime Detection's beliefs, Market Confidence's score history,
Market State Report's reports), so a dashboard can query exactly how any
past score was constructed — not just what it was. Prompt 4.17 exposes
this over an API.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.intelligence.base import IntelligenceComponent, IntelligenceResult, clamp

EXPLAINABILITY_EVENT_TYPE = "explainability.observation"

# Confidence-interval half-width in score points at zero confidence — a
# heuristic scale, same spirit as elsewhere in this layer.
MAX_MARGIN = 30.0


def compute_confidence_interval(score: float, confidence: float) -> tuple[float, float]:
    """Widen the interval around `score` as `confidence` falls toward 0;
    at confidence 1.0 the interval collapses to the score itself."""
    margin = MAX_MARGIN * (1 - clamp(confidence, 0.0, 1.0))
    return (clamp(score - margin, 0.0, 100.0), clamp(score + margin, 0.0, 100.0))


@dataclass
class ExplainabilityRecord:
    component: str
    symbol: str
    timeframe: str
    as_of: datetime
    score: float
    confidence: float
    confidence_interval: tuple[float, float]
    contributions: list[dict[str, Any]] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "as_of": self.as_of.isoformat(),
            "score": self.score,
            "confidence": self.confidence,
            "confidence_interval": list(self.confidence_interval),
            "contributions": self.contributions,
            "reasoning": self.reasoning,
        }


def explain(
    component: str, symbol: str, timeframe: str, result: IntelligenceResult
) -> ExplainabilityRecord:
    """Pure: build a full explainability record from any component's result."""
    contributions = [
        {"feature": c.feature, "value": c.value, "weight": c.weight, "effect": c.effect}
        for c in result.contributions
    ]
    return ExplainabilityRecord(
        component=component,
        symbol=symbol,
        timeframe=timeframe,
        as_of=result.as_of,
        score=result.score,
        confidence=result.confidence,
        confidence_interval=compute_confidence_interval(result.score, result.confidence),
        contributions=contributions,
        reasoning=list(result.reasoning),
    )


class ExplainabilityStore(IntelligenceComponent):
    name = "explainability_store"

    async def record(
        self, component: str, symbol: str, timeframe: str, result: IntelligenceResult
    ) -> ExplainabilityRecord:
        """Build and persist the explainability record for `result`."""
        record = explain(component, symbol, timeframe, result)
        await self._persist(record)
        return record

    async def _persist(self, record: ExplainabilityRecord) -> None:
        if self._sessions is None:
            return
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            session.add(MarketEvent(
                event_type=EXPLAINABILITY_EVENT_TYPE,
                source=record.component,
                data=record.to_dict(),
            ))
            await session.commit()

    async def history(
        self, component: str, symbol: str, timeframe: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Past explainability records, oldest first."""
        if self._sessions is None:
            return []
        from sqlalchemy import desc, select

        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            result = await session.execute(
                select(MarketEvent.data)
                .where(
                    MarketEvent.event_type == EXPLAINABILITY_EVENT_TYPE,
                    MarketEvent.source == component,
                    MarketEvent.data["symbol"].astext == symbol,
                    MarketEvent.data["timeframe"].astext == timeframe,
                )
                .order_by(desc(MarketEvent.id))
                .limit(limit)
            )
            rows = result.scalars().all()
        return list(reversed(rows))

    async def latest(
        self, component: str, symbol: str, timeframe: str
    ) -> dict[str, Any] | None:
        history = await self.history(component, symbol, timeframe, limit=1)
        return history[-1] if history else None
