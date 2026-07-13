"""Signal Priority Engine (Volume 5, Prompt 5.13).

"Suppose 40 signals appear at once -- Telegram should not receive all
40." This ranks QUALIFIED trades (Prompt 5.12's own gate: unqualified
candidates are dropped before ranking even begins, never merely
down-ranked) across eight factors, each reusing an already-real value:

- Conviction               -> ConvictionEngine.conviction_score (5.11),
                               already 0-100.
- Opportunity Quality      -> TradeCandidate.priority_score (5.2) -- a
                               confidence-weighted sum of trigger weights,
                               unbounded and non-negative, saturated here
                               via tanh (0 -> 0, not 50: no meaningful
                               trigger evidence genuinely means no quality,
                               not a "neutral" reading).
- Risk                     -> the raw `risk_var_95_20` feature (Volume 3,
                               risk.py) -- no Volume 4 intelligence engine
                               wraps Risk, so it's fetched directly, the
                               same move qualification.py made for
                               `liquidity_spread_pct`. Inverted and
                               saturated: lower VaR = higher priority.
- Liquidity                -> LiquidityIntelligenceEngine.score (Volume 4),
                               already 0-100.
- Sector Leadership        -> RelativeStrengthIntelligenceEngine's own
                               `leadership_ranking` submetric (a genuine
                               percentile-rank field, distinct from the
                               `outperformance_score` Conviction's own
                               "Sector Strength" factor uses off the same
                               engine).
- Historical Reliability   -> HistoricalSimilarityEngine.mean_similarity
                               (5.9) -- the same field
                               qualification.py's own "Historical analog
                               reliability poor" check already reuses.
- Expected Reward          -> HistoricalSimilarityEngine.average_return
                               (5.9), saturated via the exact
                               `50 + 50 * tanh(x / scale)` idiom
                               analogs.py's own score already uses.
- Expected Opportunity Lifetime -> TradeCandidate.estimated_lifetime_minutes
                               (5.2) -- a LONGER lifetime scores higher:
                               candidates.py's own TRIGGER_LIFETIME_MINUTES
                               table already gives its longest lifetimes to
                               its most durable trigger types (institutional
                               accumulation/distribution), so durability is
                               already this codebase's own notion of quality
                               here, not urgency.

Unlike Prompt 5.12's qualification gate (which fails CLOSED on missing
data, since it's a safety filter), this is a SCORING/ranking engine, so it
follows Conviction Engine's own convention: a missing factor (e.g. no
historical analogs, no leadership percentile data) is dropped, not
zeroed, and the remaining factors' weights are renormalized -- never a
fabricated neutral fill-in.

Ranking pool is a fresh Top-20 candidate scan (Prompt 5.2) filtered down
to only the qualified ones (Prompt 5.12); Top N of THOSE are returned,
by construction never more than 20 and typically far fewer once
unqualified candidates are dropped.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.cache import CacheService
from app.core.config import Settings, get_settings
from app.events.bus import Event, EventBus
from app.features.store import FeatureStore
from app.intelligence.base import IntelligenceResult, clamp
from app.intelligence.liquidity import LiquidityIntelligenceEngine
from app.intelligence.relative import RelativeStrengthIntelligenceEngine
from app.prediction.candidates import CandidateGenerationEngine, TradeCandidate
from app.prediction.conviction import ConvictionEngine, ConvictionResult
from app.prediction.historical_similarity import (
    HistoricalSimilarityEngine,
    HistoricalSimilarityResult,
)
from app.prediction.qualification import TradeQualificationEngine

EVENT_TYPE = "signal_priority.result"

TOP_N_DEFAULT = 10
RISK_TIMEFRAME = "D"
RISK_FEATURE = "risk_var_95_20"  # canonical window=20, matching TRAILING_VOL_WINDOW elsewhere

PRIORITY_WEIGHTS: dict[str, float] = dict.fromkeys((
    "conviction", "opportunity_quality", "risk", "liquidity",
    "sector_leadership", "historical_reliability", "expected_reward",
    "expected_opportunity_lifetime",
), 1.0)

OPPORTUNITY_QUALITY_SATURATION = 2.0  # priority_score at which trigger evidence saturates
EXPECTED_REWARD_SATURATION = 0.05  # matches analogs.py's own RETURN_SATURATION
RISK_VAR_CEILING_PCT = 5.0  # a 5% 95%-VaR is treated as maximally risky for ranking purposes
LIFETIME_SATURATION_MINUTES = 24 * 60  # a full day or more of durability saturates the score


def opportunity_quality_score(priority_score: float) -> float:
    """0 -> 0 (no trigger evidence genuinely means no quality, not a
    fabricated neutral reading), saturating toward 100."""
    return round(100 * math.tanh(max(priority_score, 0.0) / OPPORTUNITY_QUALITY_SATURATION), 4)


def risk_quality_score(var95_pct: float) -> float:
    """Lower VaR = higher priority score."""
    return round(clamp(100 - 100 * (var95_pct / RISK_VAR_CEILING_PCT), 0.0, 100.0), 4)


def reward_score(average_return: float) -> float:
    """The exact `50 + 50 * tanh(x / scale)` idiom analogs.py's own score
    already uses."""
    level = 50 + 50 * math.tanh(average_return / EXPECTED_REWARD_SATURATION)
    return round(clamp(level, 0.0, 100.0), 4)


def lifetime_score(lifetime_minutes: float) -> float:
    return round(clamp(100 * lifetime_minutes / LIFETIME_SATURATION_MINUTES, 0.0, 100.0), 4)


@dataclass(frozen=True)
class PriorityFactor:
    name: str
    score: float  # 0-100

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": round(self.score, 4),
            "weight": PRIORITY_WEIGHTS.get(self.name, 0.0),
        }


def build_priority_factors(
    candidate: TradeCandidate,
    conviction: ConvictionResult,
    liquidity_result: IntelligenceResult,
    relative_result: IntelligenceResult,
    similarity: HistoricalSimilarityResult,
    risk_var_pct: float | None,
) -> list[PriorityFactor]:
    """Pure assembly of the 8 ranking factors from already-computed
    upstream results -- no DB access. Factors with no real underlying
    data (missing leadership percentile, zero historical analogs, no
    risk feature yet) are OMITTED, not filled with a fabricated neutral
    value -- compute_priority renormalizes over whichever are present."""
    factors = [
        PriorityFactor(name="conviction", score=conviction.conviction_score),
        PriorityFactor(
            name="opportunity_quality", score=opportunity_quality_score(candidate.priority_score)
        ),
        PriorityFactor(name="liquidity", score=liquidity_result.score),
        PriorityFactor(
            name="expected_opportunity_lifetime",
            score=lifetime_score(candidate.estimated_lifetime_minutes),
        ),
    ]
    if risk_var_pct is not None:
        factors.append(PriorityFactor(name="risk", score=risk_quality_score(risk_var_pct)))
    leadership = relative_result.metrics.get("leadership_ranking")
    if leadership is not None:
        factors.append(PriorityFactor(name="sector_leadership", score=leadership))
    if similarity.mean_similarity is not None:
        factors.append(PriorityFactor(
            name="historical_reliability", score=similarity.mean_similarity * 100
        ))
    if similarity.average_return is not None:
        factors.append(PriorityFactor(
            name="expected_reward", score=reward_score(similarity.average_return)
        ))
    return factors


def compute_priority(
    factors: Sequence[PriorityFactor], weights: Mapping[str, float] = PRIORITY_WEIGHTS
) -> tuple[float, float]:
    """(priority_score, data_completeness). Renormalizes over whichever
    factors are actually present -- never fabricates a missing one."""
    total_weight = sum(weights.get(f.name, 0.0) for f in factors)
    if total_weight <= 0:
        return 0.0, 0.0
    priority_score = sum(weights.get(f.name, 0.0) * f.score for f in factors) / total_weight
    data_completeness = len(factors) / len(weights)
    return round(priority_score, 4), round(data_completeness, 4)


@dataclass
class RankedSignal:
    rank: int
    symbol: str
    direction: str
    priority_score: float
    data_completeness: float
    conviction_score: float
    conviction_grade: str
    as_of: datetime
    factors: list[PriorityFactor] = field(default_factory=list)
    # The originating TradeCandidate's own reason string (Prompt 5.2) --
    # carried through so a downstream consumer (Prompt 5.14's Duplicate
    # Signal Engine, which needs to recognize "this is another breakout
    # signal") doesn't have to re-fetch or re-derive it. Empty when no
    # candidate reason was available.
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "symbol": self.symbol,
            "direction": self.direction,
            "priority_score": self.priority_score,
            "data_completeness": self.data_completeness,
            "conviction_score": self.conviction_score,
            "conviction_grade": self.conviction_grade,
            "as_of": self.as_of.isoformat(),
            "factors": [f.to_dict() for f in self.factors],
            "reason": self.reason,
        }


class SignalPriorityEngine:
    name = "signal_priority_engine"

    def __init__(
        self,
        session_factory: Any = None,
        cache: CacheService | None = None,
        settings: Settings | None = None,
        bus: EventBus | None = None,
        candidate_engine: CandidateGenerationEngine | None = None,
        qualification_engine: TradeQualificationEngine | None = None,
        conviction_engine: ConvictionEngine | None = None,
        liquidity_engine: LiquidityIntelligenceEngine | None = None,
        relative_strength_engine: RelativeStrengthIntelligenceEngine | None = None,
        historical_similarity_engine: HistoricalSimilarityEngine | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings or get_settings()
        self._bus = bus
        self.store = FeatureStore(session_factory=session_factory, cache=cache)
        self._candidates = candidate_engine or CandidateGenerationEngine(
            session_factory=session_factory, settings=self._settings,
        )
        self._qualification = qualification_engine or TradeQualificationEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._conviction = conviction_engine or ConvictionEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._liquidity = liquidity_engine or LiquidityIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._relative_strength = relative_strength_engine or RelativeStrengthIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._historical_similarity = historical_similarity_engine or HistoricalSimilarityEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )

    async def rank(self, top_n: int = TOP_N_DEFAULT) -> list[RankedSignal]:
        """Fresh Top-20 candidate scan -> drop unqualified candidates ->
        score the rest across the 8 factors -> Top N by priority_score."""
        candidates = await self._candidates.generate()

        scored: list[RankedSignal] = []
        for candidate in candidates:
            qualification = await self._qualification.evaluate(
                candidate.instrument, direction=candidate.direction
            )
            if not qualification.qualified:
                continue

            conviction = await self._conviction.evaluate(
                candidate.instrument, direction=candidate.direction
            )
            liquidity_result = await self._liquidity.assess(candidate.instrument)
            relative_result = await self._relative_strength.assess(candidate.instrument)
            similarity = await self._historical_similarity.evaluate(
                candidate.instrument, direction=candidate.direction
            )
            risk_var_pct = await self._risk_var_pct(candidate.instrument)

            factors = build_priority_factors(
                candidate=candidate, conviction=conviction, liquidity_result=liquidity_result,
                relative_result=relative_result, similarity=similarity,
                risk_var_pct=risk_var_pct,
            )
            priority_score, data_completeness = compute_priority(factors)
            scored.append(RankedSignal(
                rank=0, symbol=candidate.instrument, direction=candidate.direction,
                priority_score=priority_score, data_completeness=data_completeness,
                conviction_score=conviction.conviction_score,
                conviction_grade=conviction.conviction_grade, as_of=candidate.as_of,
                factors=factors, reason=candidate.reason,
            ))

        scored.sort(key=lambda signal: signal.priority_score, reverse=True)
        top = scored[:top_n]
        for index, signal in enumerate(top, start=1):
            signal.rank = index

        await self._persist_all(top)
        return top

    async def _risk_var_pct(self, symbol: str) -> float | None:
        latest = await self.store.latest(symbol, RISK_TIMEFRAME)
        entry = latest.get(RISK_FEATURE)
        if not isinstance(entry, dict):
            return None
        return entry.get("value")

    async def _persist_all(self, signals: Sequence[RankedSignal]) -> None:
        if self._bus is not None:
            for signal in signals:
                await self._bus.publish(
                    Event(type=EVENT_TYPE, payload=signal.to_dict(), source=self.name)
                )
        if self._sessions is None:
            return
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            for signal in signals:
                session.add(MarketEvent(
                    event_type=EVENT_TYPE,
                    source=self.name,
                    data=signal.to_dict(),
                ))
            await session.commit()

    async def recent(self, symbol: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if self._sessions is None:
            return []
        from sqlalchemy import desc, select

        from app.database.tables import MarketEvent

        query = select(MarketEvent.data).where(MarketEvent.event_type == EVENT_TYPE)
        if symbol is not None:
            query = query.where(MarketEvent.data["symbol"].astext == symbol)
        query = query.order_by(desc(MarketEvent.id)).limit(limit)
        async with self._sessions() as session:
            result = await session.execute(query)
            return list(result.scalars().all())
