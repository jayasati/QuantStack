"""Trade Qualification Engine (Volume 5, Prompt 5.12).

"Even with high conviction, some trades should never be sent." Unlike
every scoring engine built earlier in this volume (ensemble.py,
calibration.py, agreement.py, market_context.py, conviction.py) -- which
stay honestly NEUTRAL when a signal is missing, since fabricating
pessimism would bias a score just as much as fabricating optimism -- this
is a SAFETY GATE, and safety gates get to be conservative: a false
negative (skip a good trade because one data source was momentarily
unavailable) is cheap, a false positive (qualify a trade with an unknown
spread, unknown event risk, or unknown model agreement) is not. Every one
of the seven checks below therefore fails CLOSED (rejects) when its
underlying data is missing, not just when it's genuinely bad. This is a
deliberate, documented divergence from this volume's scoring engines, not
an inconsistency.

Every check reuses an already-real, already-computed value -- nothing is
re-derived:
- Liquidity too low     -> LiquidityIntelligenceEngine.score (Volume 4),
                            floored at labeling.py's own
                            LIQUIDITY_SCORE_THRESHOLD (30.0) for
                            consistency with the rest of this codebase.
- Spread too large      -> the raw `liquidity_spread_pct` feature (Volume
                            3), fetched directly since
                            LiquidityIntelligenceEngine folds it into a
                            blended score/execution_risk rather than
                            passing it through on its own. Ceiling is
                            liquidity.py's own SPREAD_SCORE_CEILING_PCT / 2
                            -- the exact "tight vs wide" boundary that
                            module's own contribution labeling already uses.
- Event Risk too high   -> EventIntelligenceEngine.score (Volume 4) or its
                            own `trading_freeze_recommended` flag.
- Model disagreement high -> `not ModelAgreementEngine.evaluate(...).proceed`
                            -- a direct reuse of Chapter 8's own "only
                            high-agreement predictions proceed" gate, not
                            a second disagreement threshold invented here.
- Feature Quality poor  -> MarketConfidenceEngine's own
                            `metrics["feature_quality"]` submetric
                            (Volume 4, confidence.py) -- the same FeatureDrift
                            breach-rate read that engine already computes.
- Market Confidence poor -> the SAME MarketConfidenceEngine call's own
                            `.score`, floored at confidence.py's own D-grade
                            threshold (35.0) -- "poor" means D/F grade.
- Historical analog reliability poor -> HistoricalSimilarityEngine's own
                            `mean_similarity` (Prompt 5.9) -- how close the
                            top-20 analogs actually are, not merely how
                            many were found.

Every rejection produces an explicit, human-readable reason (the doc's own
requirement); qualified trades carry an empty rejection_reasons list.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.cache import CacheService
from app.core.config import Settings, get_settings
from app.features.liquidity import SPREAD_SCORE_CEILING_PCT
from app.features.store import FeatureStore
from app.intelligence.base import IntelligenceResult
from app.intelligence.confidence import MarketConfidenceEngine
from app.intelligence.events import EventIntelligenceEngine
from app.intelligence.liquidity import LiquidityIntelligenceEngine
from app.prediction.agreement import AgreementResult, ModelAgreementEngine
from app.prediction.candidates import CandidateGenerationEngine, TradeCandidate
from app.prediction.historical_similarity import (
    HistoricalSimilarityEngine,
    HistoricalSimilarityResult,
)

EVENT_TYPE = "trade_qualification.result"
QUOTE_TIMEFRAME = "quote"

MIN_LIQUIDITY_SCORE = 30.0  # matches labeling.py's own LIQUIDITY_SCORE_THRESHOLD
MAX_SPREAD_PCT = SPREAD_SCORE_CEILING_PCT / 2  # matches liquidity.py's own tight/wide boundary
MAX_EVENT_RISK_SCORE = 70.0
MIN_FEATURE_QUALITY = 0.5
MIN_MARKET_CONFIDENCE_SCORE = 35.0  # matches confidence.py's own D-grade floor
MIN_ANALOG_SIMILARITY = 0.5


def check_liquidity(liquidity_result: IntelligenceResult) -> str | None:
    if liquidity_result.score < MIN_LIQUIDITY_SCORE:
        return (
            f"Liquidity too low: Liquidity Score {liquidity_result.score:.0f}/100 "
            f"(floor {MIN_LIQUIDITY_SCORE:.0f})."
        )
    return None


def check_spread(spread_pct: float | None) -> str | None:
    if spread_pct is None or spread_pct > MAX_SPREAD_PCT:
        observed = f"{spread_pct:.2f}%" if spread_pct is not None else "unavailable"
        return f"Spread too large: {observed} (ceiling {MAX_SPREAD_PCT:.2f}%)."
    return None


def check_event_risk(event_result: IntelligenceResult) -> str | None:
    freeze = bool(event_result.metrics.get("trading_freeze_recommended"))
    if freeze or event_result.score > MAX_EVENT_RISK_SCORE:
        detail = ", trading freeze recommended" if freeze else ""
        return (
            f"Event Risk too high: {event_result.score:.0f}/100 "
            f"(ceiling {MAX_EVENT_RISK_SCORE:.0f}){detail}."
        )
    return None


def check_model_agreement(agreement: AgreementResult) -> str | None:
    if not agreement.proceed:
        return (
            f"Model disagreement high: agreement {agreement.agreement_pct:.0%} "
            f"({agreement.agreement_level})."
        )
    return None


def check_feature_quality(market_confidence: IntelligenceResult) -> str | None:
    feature_quality = market_confidence.metrics.get("feature_quality")
    if feature_quality is None or feature_quality < MIN_FEATURE_QUALITY:
        observed = f"{feature_quality:.0%}" if feature_quality is not None else "unavailable"
        return f"Feature Quality poor: {observed} (floor {MIN_FEATURE_QUALITY:.0%})."
    return None


def check_market_confidence(market_confidence: IntelligenceResult) -> str | None:
    if market_confidence.score < MIN_MARKET_CONFIDENCE_SCORE:
        return (
            f"Market Confidence poor: {market_confidence.score:.0f}/100 "
            f"(floor {MIN_MARKET_CONFIDENCE_SCORE:.0f})."
        )
    return None


def check_historical_analog_reliability(similarity: HistoricalSimilarityResult) -> str | None:
    mean_similarity = similarity.mean_similarity
    if mean_similarity is None or mean_similarity < MIN_ANALOG_SIMILARITY:
        observed = f"{mean_similarity:.2f}" if mean_similarity is not None else "no analogs found"
        return (
            f"Historical analog reliability poor: {observed} "
            f"(floor {MIN_ANALOG_SIMILARITY:.2f})."
        )
    return None


def qualify_trade(
    liquidity_result: IntelligenceResult,
    spread_pct: float | None,
    event_result: IntelligenceResult,
    agreement: AgreementResult,
    market_confidence: IntelligenceResult,
    similarity: HistoricalSimilarityResult,
) -> tuple[bool, list[str]]:
    """Pure gate over 6 already-computed upstream results -- no DB access.
    Returns (qualified, rejection_reasons); qualified is True only when
    every one of the 7 checks (Feature Quality and Market Confidence share
    one upstream call) passes."""
    reasons = [
        reason for reason in (
            check_liquidity(liquidity_result),
            check_spread(spread_pct),
            check_event_risk(event_result),
            check_model_agreement(agreement),
            check_feature_quality(market_confidence),
            check_market_confidence(market_confidence),
            check_historical_analog_reliability(similarity),
        )
        if reason is not None
    ]
    return not reasons, reasons


@dataclass
class QualificationResult:
    symbol: str
    direction: str
    as_of: datetime
    qualified: bool
    rejection_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "as_of": self.as_of.isoformat(),
            "qualified": self.qualified,
            "rejection_reasons": self.rejection_reasons,
        }


class TradeQualificationEngine:
    name = "trade_qualification_engine"

    def __init__(
        self,
        session_factory: Any = None,
        cache: CacheService | None = None,
        settings: Settings | None = None,
        liquidity_engine: LiquidityIntelligenceEngine | None = None,
        event_engine: EventIntelligenceEngine | None = None,
        agreement_engine: ModelAgreementEngine | None = None,
        market_confidence_engine: MarketConfidenceEngine | None = None,
        historical_similarity_engine: HistoricalSimilarityEngine | None = None,
        candidate_engine: CandidateGenerationEngine | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings or get_settings()
        self.store = FeatureStore(session_factory=session_factory, cache=cache)
        self._liquidity = liquidity_engine or LiquidityIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._events = event_engine or EventIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._agreement = agreement_engine or ModelAgreementEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._market_confidence = market_confidence_engine or MarketConfidenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._historical_similarity = historical_similarity_engine or HistoricalSimilarityEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._candidates = candidate_engine or CandidateGenerationEngine(
            session_factory=session_factory, settings=self._settings,
        )

    async def evaluate(
        self, symbol: str, timeframe: str = "D", direction: str = "long"
    ) -> QualificationResult:
        """Fresh reads of all 6 upstream sources, then the qualification
        gate over them."""
        liquidity_result = await self._liquidity.assess(symbol)
        spread_pct = await self._spread_pct(symbol)
        event_result = await self._events.assess()
        agreement = await self._agreement.evaluate(symbol, timeframe=timeframe, direction=direction)
        market_confidence = await self._market_confidence.assess(symbol)
        similarity = await self._historical_similarity.evaluate(symbol, direction=direction)

        qualified, reasons = qualify_trade(
            liquidity_result, spread_pct, event_result, agreement, market_confidence, similarity,
        )
        result = QualificationResult(
            symbol=symbol, direction=direction, as_of=datetime.now(UTC),
            qualified=qualified, rejection_reasons=reasons,
        )
        await self._persist(result)
        return result

    async def evaluate_candidates(
        self, candidates: Sequence[TradeCandidate]
    ) -> list[QualificationResult]:
        """One qualification result per already-generated TradeCandidate
        (Prompt 5.2). Only qualified trades should continue downstream."""
        return [await self.evaluate(c.instrument, direction=c.direction) for c in candidates]

    async def evaluate_top_candidates(self) -> list[QualificationResult]:
        """Convenience: a fresh Top-20 candidate scan, then a
        qualification result for every one of them."""
        candidates = await self._candidates.generate()
        return await self.evaluate_candidates(candidates)

    async def _spread_pct(self, symbol: str) -> float | None:
        latest = await self.store.latest(symbol, QUOTE_TIMEFRAME)
        entry = latest.get("liquidity_spread_pct")
        if not isinstance(entry, dict):
            return None
        return entry.get("value")

    async def _persist(self, result: QualificationResult) -> None:
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
