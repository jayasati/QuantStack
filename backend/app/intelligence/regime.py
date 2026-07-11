"""Bayesian Regime Detection (Volume 4, Prompt 4.11).

Every component built so far already produces a probability distribution
over regime labels rather than a hard label (Chapter 15's philosophy was
built into IntelligenceResult.states from the start — see base.py). What
none of them do is remember anything BETWEEN calls: each assess() is a
pure, memoryless read of the latest Feature Store snapshot. "Update
probabilities continuously as new evidence arrives" needs an actual prior
carried across calls, which this component adds as a generic wrapper
around ANY other component's states output, rather than duplicating this
logic once per regime dimension (Trend, Volatility, Liquidity, ...).

The update rule is a confidence-weighted exponential blend — a documented
v1 simplification of full Bayesian posterior updating (which would need an
explicit likelihood function per regime and quickly becomes its own
research project), same spirit as this layer's other v1 heuristics (see
Volatility/Liquidity Intelligence). The evidence weight is clamped to
[MIN_WEIGHT, MAX_WEIGHT]: even maximum-confidence new evidence doesn't
fully overwrite the prior in one step (still no hard switching), and even
minimum-confidence evidence still nudges the belief a little (never frozen
forever). Beliefs persist as ordinary append-only market_events rows
(event_type "regime_belief.update"), the same event-sourcing pattern every
collector already uses — no new table, no upsert, and a free-standing
history of belief evolution for a future "regime transition timeline"
(Chapter 22).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from app.intelligence.base import (
    Contribution,
    IntelligenceComponent,
    IntelligenceResult,
    clamp,
    normalize_states,
)

COMPONENT_PREFIX = "bayesian_regime"
BELIEF_EVENT_TYPE = "regime_belief.update"

# Evidence-weight bounds: even confidence=1.0 evidence blends rather than
# overwrites: even confidence=0.0 evidence still nudges the prior.
MIN_WEIGHT = 0.1
MAX_WEIGHT = 0.9
# Observation count at which a belief is considered "mature" for confidence
# purposes — a heuristic scale, same spirit as elsewhere in this layer.
MATURITY_TARGET = 20


@dataclass(frozen=True)
class RegimeBelief:
    states: dict[str, float]
    observation_count: int


def bayesian_update(
    prior: Mapping[str, float] | None,
    likelihood: Mapping[str, float],
    evidence_confidence: float,
) -> dict[str, float]:
    """Blend a prior belief with a new observation, weighted by how much to
    trust the observation. No prior means the posterior is just the
    (normalized) likelihood."""
    if not prior:
        return normalize_states(dict(likelihood))
    weight = clamp(evidence_confidence, MIN_WEIGHT, MAX_WEIGHT)
    keys = set(prior) | set(likelihood)
    blended = {
        k: (1 - weight) * prior.get(k, 0.0) + weight * likelihood.get(k, 0.0)
        for k in keys
    }
    return normalize_states(blended)


class BayesianRegimeDetector(IntelligenceComponent):
    name = "bayesian_regime_detector"

    async def update(
        self,
        component: str,
        symbol: str,
        timeframe: str,
        likelihood: Mapping[str, float],
        evidence_confidence: float,
    ) -> IntelligenceResult:
        """Blend `likelihood` (a fresh states reading from any other
        component) into the running belief for (component, symbol,
        timeframe), persist it, and return the smoothed result."""
        prior = await self._load_belief(component, symbol, timeframe)
        observation_count = (prior.observation_count if prior else 0) + 1
        posterior = bayesian_update(
            prior.states if prior else None, likelihood, evidence_confidence
        )
        await self._store_belief(component, symbol, timeframe, posterior, observation_count)
        await self._publish(
            BELIEF_EVENT_TYPE,
            {
                "component": component,
                "symbol": symbol,
                "timeframe": timeframe,
                "observation_count": observation_count,
                "dominant_state": (
                    max(posterior, key=lambda s: posterior[s]) if posterior else None
                ),
            },
        )

        dominant = max(posterior, key=lambda s: posterior[s]) if posterior else "unknown"
        maturity = clamp(observation_count / MATURITY_TARGET, 0.0, 1.0)
        confidence = clamp(0.5 * evidence_confidence + 0.5 * maturity, 0.0, 1.0)
        score = clamp(100 * max(posterior.values()), 0.0, 100.0) if posterior else 50.0

        reasoning = [
            f"Blended {'a prior belief' if prior else 'no prior belief (first observation)'} "
            f"with new evidence at confidence {evidence_confidence:.2f}.",
            f"Observation #{observation_count}, maturity {maturity:.0%}.",
            f"Dominant state: {dominant}.",
        ]

        return IntelligenceResult(
            component=f"{COMPONENT_PREFIX}[{component}]",
            score=score,
            confidence=confidence,
            states=posterior,
            metrics={
                "target_component": component,
                "symbol": symbol,
                "timeframe": timeframe,
                "prior": dict(prior.states) if prior else None,
                "likelihood": dict(likelihood),
                "observation_count": observation_count,
                "dominant_state": dominant,
            },
            contributions=[
                Contribution(
                    feature="evidence_confidence", value=evidence_confidence, weight=0.5,
                    effect="strong update" if evidence_confidence > 0.5 else "weak update",
                ),
                Contribution(
                    feature="observation_count", value=float(observation_count), weight=0.5,
                    effect="mature belief" if maturity > 0.5 else "young belief",
                ),
            ],
            reasoning=reasoning,
        )

    async def update_from_result(
        self, component: str, symbol: str, timeframe: str, result: IntelligenceResult
    ) -> IntelligenceResult:
        """Convenience wrapper: blend another component's own IntelligenceResult
        (its states as likelihood, its confidence as the evidence weight)."""
        return await self.update(component, symbol, timeframe, result.states, result.confidence)

    async def _load_belief(
        self, component: str, symbol: str, timeframe: str
    ) -> RegimeBelief | None:
        if self._sessions is None:
            return None
        from sqlalchemy import desc, select

        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            result = await session.execute(
                select(MarketEvent.data)
                .where(
                    MarketEvent.event_type == BELIEF_EVENT_TYPE,
                    MarketEvent.source == component,
                    MarketEvent.data["symbol"].astext == symbol,
                    MarketEvent.data["timeframe"].astext == timeframe,
                )
                .order_by(desc(MarketEvent.id))
                .limit(1)
            )
            row = result.scalar_one_or_none()
        if not row:
            return None
        return RegimeBelief(
            states=row.get("states") or {}, observation_count=row.get("observation_count", 0)
        )

    async def history(
        self, component: str, symbol: str, timeframe: str, limit: int = 20
    ) -> list[dict[str, float]]:
        """The last `limit` posterior belief snapshots, oldest first — the
        input Regime Transition Detection (Prompt 4.12) analyzes for drift."""
        if self._sessions is None:
            return []
        from sqlalchemy import desc, select

        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            result = await session.execute(
                select(MarketEvent.data)
                .where(
                    MarketEvent.event_type == BELIEF_EVENT_TYPE,
                    MarketEvent.source == component,
                    MarketEvent.data["symbol"].astext == symbol,
                    MarketEvent.data["timeframe"].astext == timeframe,
                )
                .order_by(desc(MarketEvent.id))
                .limit(limit)
            )
            rows = result.scalars().all()
        return [row.get("states") or {} for row in reversed(rows)]

    async def _store_belief(
        self,
        component: str,
        symbol: str,
        timeframe: str,
        states: Mapping[str, float],
        observation_count: int,
    ) -> None:
        if self._sessions is None:
            return
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            session.add(MarketEvent(
                event_type=BELIEF_EVENT_TYPE,
                source=component,
                data={
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "states": dict(states),
                    "observation_count": observation_count,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            ))
            await session.commit()
