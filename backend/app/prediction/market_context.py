"""Market Context Adjustment (Volume 5, Prompt 5.10).

"Models do not know context. This engine does." Model probabilities
(Prompt 5.6, refined by Prompt 5.7's calibration) are adjusted using six
already-built Volume 4 intelligence signals -- nothing here re-derives
market quality from raw features; each dimension is read straight from
that engine's own real IntelligenceResult.score/.confidence:

- Market Confidence (confidence.py): score is already a 0-100 "how good
  are conditions" read, higher = better -- quality = score / 100.
- Liquidity (liquidity.py): Liquidity Score is already 0-100, higher =
  more liquid -- quality = score / 100.
- Event Risk (events.py): score is a RISK magnitude (0 = clear, 100 =
  high risk) -- inverted: quality = 1 - score / 100.
- Regime Stability (transitions.py, RegimeTransitionEngine): score is an
  INSTABILITY magnitude (0 = stable, 100 = unstable) -- inverted the same
  way MarketConfidenceEngine's own regime_certainty submetric already
  does (confidence.py: `1 - regime.score / 100`), reused here rather than
  re-derived a second way.
- Institutional Participation (institutional_flow.py): score is
  50-centered (50 = neutral flow, away from 50 in either direction =
  accumulation or distribution). Direction doesn't matter for market
  QUALITY here -- an informed, decisive market (whichever way it's
  leaning) is a higher-quality one than a directionless, thin one --
  quality = |score - 50| / 50.
- Volatility (volatility.py): score is an extremity magnitude (0 = calm,
  100 = extreme) -- per this chapter's own framing (volatility listed
  alongside event risk/liquidity as a *degrading* factor, not a
  two-sided "some volatility is good" read), quality = 1 - score / 100.

Each dimension's own `.confidence` (how much to trust THAT specific
reading, not the market quality itself) weights it in the composite --
a dimension with no real data underneath it (e.g. an index symbol with no
liquidity microstructure) contributes almost nothing rather than a
misleadingly precise number. If every dimension has zero confidence, this
engine honestly returns market_quality_score=None and passes the input
probability/confidence through unchanged -- an identity no-op, the same
idiom calibration.py's own `calibration=None` fallback already uses,
never a fabricated shrink with no data behind it.

Adjustment itself is a shrinkage toward 0.5 (a coin flip) proportional to
`1 - market_quality`: perfect market quality leaves the probability
untouched; degraded quality pulls conviction toward neutral. This is
literally "adjust probabilities using context" (the number itself moves)
and simultaneously "reduce confidence whenever market quality
deteriorates" (a shrunk-toward-neutral probability IS a less confident
one), so the multiplicative confidence reduction and the probability
shrink share the same market_quality factor by construction.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.cache import CacheService
from app.core.config import Settings, get_settings
from app.intelligence.base import IntelligenceResult, clamp
from app.intelligence.confidence import MarketConfidenceEngine
from app.intelligence.events import EventIntelligenceEngine
from app.intelligence.institutional_flow import InstitutionalFlowIntelligenceEngine
from app.intelligence.liquidity import LiquidityIntelligenceEngine
from app.intelligence.transitions import RegimeTransitionEngine
from app.intelligence.volatility import VolatilityIntelligenceEngine
from app.prediction.calibration import CalibratedPrediction, ProbabilityCalibrationEngine

EVENT_TYPE = "market_context_adjustment.result"

DIMENSION_NAMES: tuple[str, ...] = (
    "market_confidence", "liquidity", "event_risk",
    "regime_stability", "institutional_participation", "volatility",
)
# Equal weighting (documented v1 -- doc doesn't specify relative weights;
# same "configurable, eventually learnable" spirit as Chapter 11's own
# Conviction Engine weights).
DIMENSION_WEIGHTS: dict[str, float] = dict.fromkeys(DIMENSION_NAMES, 1.0)


@dataclass(frozen=True)
class ContextDimension:
    name: str
    quality: float  # 0..1, 1 = excellent market quality along this dimension
    confidence: float  # 0..1, how much to trust THIS dimension's own reading
    raw_score: float  # the underlying IntelligenceResult.score, for transparency

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "quality": round(self.quality, 4),
            "confidence": round(self.confidence, 4),
            "raw_score": round(self.raw_score, 4),
        }


def market_confidence_quality(result: IntelligenceResult) -> ContextDimension:
    return ContextDimension(
        name="market_confidence", quality=clamp(result.score / 100, 0.0, 1.0),
        confidence=result.confidence, raw_score=result.score,
    )


def liquidity_quality(result: IntelligenceResult) -> ContextDimension:
    return ContextDimension(
        name="liquidity", quality=clamp(result.score / 100, 0.0, 1.0),
        confidence=result.confidence, raw_score=result.score,
    )


def event_risk_quality(result: IntelligenceResult) -> ContextDimension:
    return ContextDimension(
        name="event_risk", quality=clamp(1 - result.score / 100, 0.0, 1.0),
        confidence=result.confidence, raw_score=result.score,
    )


def regime_stability_quality(result: IntelligenceResult) -> ContextDimension:
    return ContextDimension(
        name="regime_stability", quality=clamp(1 - result.score / 100, 0.0, 1.0),
        confidence=result.confidence, raw_score=result.score,
    )


def institutional_participation_quality(result: IntelligenceResult) -> ContextDimension:
    return ContextDimension(
        name="institutional_participation",
        quality=clamp(abs(result.score - 50) / 50, 0.0, 1.0),
        confidence=result.confidence, raw_score=result.score,
    )


def volatility_quality(result: IntelligenceResult) -> ContextDimension:
    return ContextDimension(
        name="volatility", quality=clamp(1 - result.score / 100, 0.0, 1.0),
        confidence=result.confidence, raw_score=result.score,
    )


def compute_market_quality(
    dimensions: Sequence[ContextDimension], weights: Mapping[str, float] = DIMENSION_WEIGHTS
) -> tuple[float | None, float]:
    """(market_quality_score, market_quality_confidence). Returns
    (None, 0.0) -- never a fabricated 0.5 -- when every dimension came
    back with zero trust in its own reading."""
    confidence_weighted_total = sum(weights.get(d.name, 0.0) * d.confidence for d in dimensions)
    if confidence_weighted_total <= 0:
        return None, 0.0

    market_quality = sum(
        weights.get(d.name, 0.0) * d.confidence * d.quality for d in dimensions
    ) / confidence_weighted_total

    static_total = sum(weights.get(d.name, 0.0) for d in dimensions)
    market_quality_confidence = (
        confidence_weighted_total / static_total if static_total > 0 else 0.0
    )
    return round(market_quality, 4), round(market_quality_confidence, 4)


@dataclass
class MarketContextAdjustment:
    symbol: str
    snapshot_id: str
    as_of: datetime
    input_probability: float
    adjusted_probability: float
    input_confidence: float
    adjusted_confidence: float
    market_quality_score: float | None
    market_quality_confidence: float
    calibration_method: str
    dimensions: list[ContextDimension] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of.isoformat(),
            "input_probability": self.input_probability,
            "adjusted_probability": self.adjusted_probability,
            "input_confidence": self.input_confidence,
            "adjusted_confidence": self.adjusted_confidence,
            "market_quality_score": self.market_quality_score,
            "market_quality_confidence": self.market_quality_confidence,
            "calibration_method": self.calibration_method,
            "dimensions": [d.to_dict() for d in self.dimensions],
        }


def apply_market_context(
    calibrated: CalibratedPrediction, dimensions: Sequence[ContextDimension]
) -> MarketContextAdjustment:
    """Pure computation from an already-calibrated prediction (Prompt 5.7)
    and already-computed context dimensions -- no DB access."""
    market_quality, market_quality_confidence = compute_market_quality(dimensions)
    input_probability = calibrated.calibrated_probability
    input_confidence = calibrated.calibration_confidence

    if market_quality is None:
        # No real context signal anywhere -- an honest no-op, the same
        # idiom as calibration.py's own identity fallback.
        return MarketContextAdjustment(
            symbol=calibrated.symbol, snapshot_id=calibrated.snapshot_id, as_of=calibrated.as_of,
            input_probability=input_probability, adjusted_probability=input_probability,
            input_confidence=input_confidence, adjusted_confidence=input_confidence,
            market_quality_score=None, market_quality_confidence=0.0,
            calibration_method=calibrated.calibration_method, dimensions=list(dimensions),
        )

    adjusted_probability = 0.5 + (input_probability - 0.5) * market_quality
    adjusted_confidence = input_confidence * market_quality
    return MarketContextAdjustment(
        symbol=calibrated.symbol, snapshot_id=calibrated.snapshot_id, as_of=calibrated.as_of,
        input_probability=input_probability, adjusted_probability=round(adjusted_probability, 4),
        input_confidence=input_confidence, adjusted_confidence=round(adjusted_confidence, 4),
        market_quality_score=market_quality, market_quality_confidence=market_quality_confidence,
        calibration_method=calibrated.calibration_method, dimensions=list(dimensions),
    )


class MarketContextAdjustmentEngine:
    name = "market_context_adjustment_engine"

    def __init__(
        self,
        session_factory: Any = None,
        cache: CacheService | None = None,
        settings: Settings | None = None,
        calibration_engine: ProbabilityCalibrationEngine | None = None,
        market_confidence_engine: MarketConfidenceEngine | None = None,
        liquidity_engine: LiquidityIntelligenceEngine | None = None,
        event_engine: EventIntelligenceEngine | None = None,
        regime_transition_engine: RegimeTransitionEngine | None = None,
        institutional_flow_engine: InstitutionalFlowIntelligenceEngine | None = None,
        volatility_engine: VolatilityIntelligenceEngine | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings or get_settings()
        self._calibration = calibration_engine or ProbabilityCalibrationEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._market_confidence = market_confidence_engine or MarketConfidenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._liquidity = liquidity_engine or LiquidityIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._events = event_engine or EventIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._regime_transitions = regime_transition_engine or RegimeTransitionEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._institutional_flow = institutional_flow_engine or InstitutionalFlowIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._volatility = volatility_engine or VolatilityIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )

    async def evaluate(
        self, symbol: str, timeframe: str = "D", direction: str = "long"
    ) -> MarketContextAdjustment:
        """Fresh calibrated prediction (Prompt 5.7), six fresh context
        reads, then the context adjustment over both."""
        calibrated = await self._calibration.predict(
            symbol, timeframe=timeframe, direction=direction
        )
        dimensions = [
            market_confidence_quality(await self._market_confidence.assess(symbol)),
            liquidity_quality(await self._liquidity.assess(symbol)),
            event_risk_quality(await self._events.assess()),
            regime_stability_quality(
                await self._regime_transitions.assess(symbol=symbol, timeframe=timeframe)
            ),
            institutional_participation_quality(await self._institutional_flow.assess()),
            volatility_quality(await self._volatility.assess(symbol, timeframe=timeframe)),
        ]
        result = apply_market_context(calibrated, dimensions)
        await self._persist(result)
        return result

    async def _persist(self, result: MarketContextAdjustment) -> None:
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
