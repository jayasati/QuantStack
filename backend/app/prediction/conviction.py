"""Conviction Engine (Volume 5, Prompt 5.11).

"Instead of a simple Rule 40% / ML 60% split, conviction blends eight
weighted evidence sources." Every source here is an already-computed,
already-real value from an engine built earlier this volume or in Volume
4 -- nothing is re-derived a second way:

| Evidence Source          | Weight | Source                                         |
|---------------------------|-------|--------------------------------------------------|
| Calibrated Probability     | 35%   | ProbabilityCalibrationEngine (5.7)               |
| Market Context             | 20%   | MarketContextAdjustmentEngine (5.10)             |
| Historical Analog          | 10%   | HistoricalSimilarityEngine (5.9)                 |
| Institutional Flow         | 10%   | InstitutionalFlowIntelligenceEngine (Vol 4)      |
| Market Structure           | 10%   | MarketStructureIntelligenceEngine (Vol 4)        |
| Liquidity                  | 5%    | LiquidityIntelligenceEngine (Vol 4)               |
| Sector Strength            | 5%    | RelativeStrengthIntelligenceEngine (Vol 4)        |
| Model Agreement            | 5%    | ModelAgreementEngine (5.8)                        |

"Sector Strength": Volume 4 never built a per-instrument, sector-only
strength engine -- SectorIntelligenceEngine (sector.py) is a market-wide
sector-ROTATION read across the whole sector universe, not one candidate's
own standing. RelativeStrengthIntelligenceEngine's outperformance score
already blends sector among its reference set (nifty/sensex/sector/
industry/peers per relative.py) and is the closest real, already-built
per-instrument proxy -- reused directly rather than fabricating an
isolated sector-only score the underlying feature data was never split
out to support. A documented approximation, the same spirit as this
volume's other honest stand-ins (historical_similarity.py's short
drawdown/run-up swap, analogs.py's Euclidean/Mahalanobis approximation).

Three sources (Institutional Flow, Market Structure, Sector Strength) are
50-centered bullish/bearish scores at the source (score > 50 = bullish,
per each engine's own `50 + 50 * level` convention) -- mirrored for a
short candidate (`100 - score`) via `directional_score`, the same sign
convention historical_similarity.py already established for this
codebase. Calibrated Probability and Historical Analog are ALREADY
direction-aware at the source (both take `direction` as a parameter and
compute a win probability specific to it). Liquidity and Model Agreement
are direction-agnostic trust/quality signals, used as-is.

Calling both ProbabilityCalibrationEngine.predict() directly (for the
Calibrated Probability source) and MarketContextAdjustmentEngine.evaluate()
(which internally re-calls calibration.predict() for its OWN Market
Context source) means calibration runs twice per evaluate() call -- an
accepted v1 redundancy, the same category snapshot.py's own docstring
already accepts for Market Confidence/Historical Analogs being fetched
twice per Market State Report.

Conviction Score is a STATIC-weighted blend using exactly the doc's fixed
percentages (renormalized over whichever sources actually returned a
usable reading -- e.g. Historical Analog is dropped, not zeroed, when a
candidate has zero analogs) -- confidence never secretly reweights the
score itself, the same score/confidence separation of concerns
MarketConfidenceEngine (Volume 4, confidence.py) already established.

Conviction Confidence, Conviction Trend (slope of persisted score history,
TREND_EPSILON threshold), and Conviction Grade (A-F letter) all directly
mirror MarketConfidenceEngine's own confidence_trend/confidence_grade
mechanism and thresholds. Conviction Stability is new here -- Volume 4 has
no precedent for it -- a genuinely distinct concept from Trend: Trend is
the SIGN of recent movement (improving/declining/stable), Stability is the
MAGNITUDE of recent variability regardless of sign (a smooth steady climb
is trend="improving" AND stability=high; a score oscillating wildly with
no net direction is trend="stable" AND stability=low). Computed from the
same persisted score history as Trend, via population stdev.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from statistics import pstdev
from typing import Any

from app.core.cache import CacheService
from app.core.config import Settings, get_settings
from app.intelligence.base import IntelligenceResult, clamp, slope
from app.intelligence.institutional_flow import InstitutionalFlowIntelligenceEngine
from app.intelligence.liquidity import LiquidityIntelligenceEngine
from app.intelligence.relative import RelativeStrengthIntelligenceEngine
from app.intelligence.structure import MarketStructureIntelligenceEngine
from app.prediction.agreement import AgreementResult, ModelAgreementEngine
from app.prediction.calibration import CalibratedPrediction, ProbabilityCalibrationEngine
from app.prediction.candidates import CandidateGenerationEngine, TradeCandidate
from app.prediction.historical_similarity import (
    HistoricalSimilarityEngine,
    HistoricalSimilarityResult,
)
from app.prediction.market_context import MarketContextAdjustment, MarketContextAdjustmentEngine

EVENT_TYPE = "conviction.result"

EVIDENCE_WEIGHTS: dict[str, float] = {
    "calibrated_probability": 0.35,
    "market_context": 0.20,
    "historical_analog": 0.10,
    "institutional_flow": 0.10,
    "market_structure": 0.10,
    "liquidity": 0.05,
    "sector_strength": 0.05,
    "model_agreement": 0.05,
}

SCORE_HISTORY_LIMIT = 20
HISTORY_TARGET = 5  # score-history points at which the trend/confidence read is fully backed
TREND_EPSILON = 1.0  # score points/observation below which the trend reads "stable"
STABILITY_SCALE = 25.0  # a 25-point stdev in recent scores is treated as maximally unstable

GRADE_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (80.0, "A"), (65.0, "B"), (50.0, "C"), (35.0, "D"),
)


def _grade(score: float) -> str:
    for threshold, letter in GRADE_THRESHOLDS:
        if score >= threshold:
            return letter
    return "F"


def directional_score(raw_score: float, direction: str) -> float:
    """Mirrors a 50-centered bullish/bearish score for a short candidate
    -- the same sign convention historical_similarity.py's own
    `_direction_sign` already established. "neutral" is treated like
    "long" (no mirroring), consistent with that same precedent."""
    return raw_score if direction != "short" else (100.0 - raw_score)


@dataclass(frozen=True)
class EvidenceContribution:
    name: str
    score: float  # 0-100, direction-aware where applicable
    confidence: float  # 0-1, how much to trust THIS source's own reading

    @property
    def effect(self) -> str:
        return "supportive" if self.score >= 50.0 else "weak"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 4),
            "weight": EVIDENCE_WEIGHTS.get(self.name, 0.0),
            "effect": self.effect,
        }


def build_evidence(
    calibrated: CalibratedPrediction,
    context: MarketContextAdjustment,
    similarity: HistoricalSimilarityResult,
    flow_result: IntelligenceResult,
    structure_result: IntelligenceResult,
    liquidity_result: IntelligenceResult,
    relative_result: IntelligenceResult,
    agreement: AgreementResult,
    direction: str,
) -> list[EvidenceContribution]:
    """Pure assembly of the 8 evidence sources from already-computed
    upstream results -- no DB access. Historical Analog is OMITTED (not
    included at a fabricated neutral score) when the candidate has zero
    historical analogs, so `compute_conviction` renormalizes over the
    remaining 7 rather than diluting the score with an invented reading."""
    evidence = [
        EvidenceContribution(
            name="calibrated_probability",
            score=calibrated.calibrated_probability * 100,
            confidence=calibrated.calibration_confidence,
        ),
        EvidenceContribution(
            name="market_context",
            score=context.adjusted_probability * 100,
            confidence=context.adjusted_confidence,
        ),
        EvidenceContribution(
            name="institutional_flow",
            score=directional_score(flow_result.score, direction),
            confidence=flow_result.confidence,
        ),
        EvidenceContribution(
            name="market_structure",
            score=directional_score(structure_result.score, direction),
            confidence=structure_result.confidence,
        ),
        EvidenceContribution(
            name="liquidity", score=liquidity_result.score, confidence=liquidity_result.confidence,
        ),
        EvidenceContribution(
            name="sector_strength",
            score=directional_score(relative_result.score, direction),
            confidence=relative_result.confidence,
        ),
        EvidenceContribution(
            name="model_agreement",
            score=agreement.agreement_pct * 100,
            confidence=agreement.model_reliability,
        ),
    ]
    if similarity.historical_win_rate is not None:
        evidence.append(EvidenceContribution(
            name="historical_analog",
            score=similarity.historical_win_rate * 100,
            confidence=similarity.mean_similarity or 0.0,
        ))
    return evidence


def compute_conviction(
    evidence: Sequence[EvidenceContribution], weights: Mapping[str, float] = EVIDENCE_WEIGHTS
) -> tuple[float, float, float]:
    """(conviction_score, mean_source_confidence, data_completeness).
    Score uses ONLY the doc's fixed static weights, renormalized over
    whichever sources are actually present -- confidence never secretly
    reweights the score itself."""
    total_weight = sum(weights.get(e.name, 0.0) for e in evidence)
    if total_weight <= 0:
        return 50.0, 0.0, 0.0

    conviction_score = sum(weights.get(e.name, 0.0) * e.score for e in evidence) / total_weight
    mean_source_confidence = (
        sum(weights.get(e.name, 0.0) * e.confidence for e in evidence) / total_weight
    )
    data_completeness = len(evidence) / len(weights)
    return round(conviction_score, 4), round(mean_source_confidence, 4), round(data_completeness, 4)


@dataclass
class ConvictionResult:
    symbol: str
    direction: str
    snapshot_id: str
    as_of: datetime
    conviction_score: float
    conviction_confidence: float
    conviction_stability: float
    conviction_trend: str
    conviction_grade: str
    trend_slope: float
    data_completeness: float
    evidence: list[EvidenceContribution] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of.isoformat(),
            "conviction_score": self.conviction_score,
            "conviction_confidence": self.conviction_confidence,
            "conviction_stability": self.conviction_stability,
            "conviction_trend": self.conviction_trend,
            "conviction_grade": self.conviction_grade,
            "trend_slope": self.trend_slope,
            "data_completeness": self.data_completeness,
            "evidence": [e.to_dict() for e in self.evidence],
        }


def assess_conviction(
    symbol: str,
    direction: str,
    snapshot_id: str,
    as_of: datetime,
    evidence: Sequence[EvidenceContribution],
    score_history: Sequence[float] = (),
) -> ConvictionResult:
    """Pure synthesis from already-built evidence and past persisted
    Conviction Scores for this (symbol, direction) -- no DB access."""
    conviction_score, mean_source_confidence, data_completeness = compute_conviction(evidence)

    trend_series = [*score_history, conviction_score]
    trend_slope = slope(trend_series) if len(trend_series) >= 2 else 0.0
    if trend_slope > TREND_EPSILON:
        trend = "improving"
    elif trend_slope < -TREND_EPSILON:
        trend = "declining"
    else:
        trend = "stable"

    stability = (
        clamp(1 - pstdev(trend_series) / STABILITY_SCALE, 0.0, 1.0)
        if len(trend_series) >= 2 else 1.0
    )

    history_sufficiency = clamp(len(score_history) / HISTORY_TARGET, 0.0, 1.0)
    conviction_confidence = clamp(
        0.4 * data_completeness + 0.3 * mean_source_confidence + 0.3 * history_sufficiency,
        0.0, 1.0,
    )

    return ConvictionResult(
        symbol=symbol, direction=direction, snapshot_id=snapshot_id, as_of=as_of,
        conviction_score=conviction_score, conviction_confidence=conviction_confidence,
        conviction_stability=round(stability, 4), conviction_trend=trend,
        conviction_grade=_grade(conviction_score), trend_slope=round(trend_slope, 4),
        data_completeness=data_completeness, evidence=list(evidence),
    )


class ConvictionEngine:
    name = "conviction_engine"

    def __init__(
        self,
        session_factory: Any = None,
        cache: CacheService | None = None,
        settings: Settings | None = None,
        calibration_engine: ProbabilityCalibrationEngine | None = None,
        market_context_engine: MarketContextAdjustmentEngine | None = None,
        historical_similarity_engine: HistoricalSimilarityEngine | None = None,
        institutional_flow_engine: InstitutionalFlowIntelligenceEngine | None = None,
        market_structure_engine: MarketStructureIntelligenceEngine | None = None,
        liquidity_engine: LiquidityIntelligenceEngine | None = None,
        relative_strength_engine: RelativeStrengthIntelligenceEngine | None = None,
        agreement_engine: ModelAgreementEngine | None = None,
        candidate_engine: CandidateGenerationEngine | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings or get_settings()
        self._calibration = calibration_engine or ProbabilityCalibrationEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._market_context = market_context_engine or MarketContextAdjustmentEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._historical_similarity = historical_similarity_engine or HistoricalSimilarityEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._institutional_flow = institutional_flow_engine or InstitutionalFlowIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._market_structure = market_structure_engine or MarketStructureIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._liquidity = liquidity_engine or LiquidityIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._relative_strength = relative_strength_engine or RelativeStrengthIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._agreement = agreement_engine or ModelAgreementEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._candidates = candidate_engine or CandidateGenerationEngine(
            session_factory=session_factory, settings=self._settings,
        )

    async def evaluate(
        self, symbol: str, timeframe: str = "D", direction: str = "long"
    ) -> ConvictionResult:
        """Fresh reads of all 8 evidence sources, then the conviction
        synthesis over them plus this (symbol, direction)'s persisted
        score history."""
        calibrated = await self._calibration.predict(
            symbol, timeframe=timeframe, direction=direction
        )
        context = await self._market_context.evaluate(
            symbol, timeframe=timeframe, direction=direction
        )
        similarity = await self._historical_similarity.evaluate(symbol, direction=direction)
        flow_result = await self._institutional_flow.assess()
        structure_result = await self._market_structure.assess(symbol, timeframe=timeframe)
        liquidity_result = await self._liquidity.assess(symbol)
        relative_result = await self._relative_strength.assess(symbol, timeframe=timeframe)
        agreement = await self._agreement.evaluate(symbol, timeframe=timeframe, direction=direction)

        evidence = build_evidence(
            calibrated=calibrated, context=context, similarity=similarity,
            flow_result=flow_result, structure_result=structure_result,
            liquidity_result=liquidity_result, relative_result=relative_result,
            agreement=agreement, direction=direction,
        )
        history = await self._load_score_history(symbol, direction)
        result = assess_conviction(
            symbol, direction, calibrated.snapshot_id, calibrated.as_of, evidence, history,
        )
        await self._persist(result)
        return result

    async def evaluate_candidates(
        self, candidates: Sequence[TradeCandidate]
    ) -> list[ConvictionResult]:
        """One Conviction result per already-generated TradeCandidate
        (Prompt 5.2)."""
        return [await self.evaluate(c.instrument, direction=c.direction) for c in candidates]

    async def evaluate_top_candidates(self) -> list[ConvictionResult]:
        """Convenience: a fresh Top-20 candidate scan, then a conviction
        result for every one of them."""
        candidates = await self._candidates.generate()
        return await self.evaluate_candidates(candidates)

    async def _load_score_history(
        self, symbol: str, direction: str, limit: int = SCORE_HISTORY_LIMIT
    ) -> list[float]:
        if self._sessions is None:
            return []
        from sqlalchemy import desc, select

        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            result = await session.execute(
                select(MarketEvent.data)
                .where(
                    MarketEvent.event_type == EVENT_TYPE,
                    MarketEvent.data["symbol"].astext == symbol,
                    MarketEvent.data["direction"].astext == direction,
                )
                .order_by(desc(MarketEvent.id))
                .limit(limit)
            )
            rows = result.scalars().all()
        return [
            row["conviction_score"] for row in reversed(rows)
            if row.get("conviction_score") is not None
        ]

    async def _persist(self, result: ConvictionResult) -> None:
        if self._sessions is None:
            return
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            session.add(MarketEvent(
                event_type=EVENT_TYPE,
                source=self.name,
                data=result.to_dict(),
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
