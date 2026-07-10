"""Model Agreement Engine (Volume 5, Prompt 5.8).

"Very important. If the models disagree, do not trade." This engine
consumes an already-computed EnsemblePrediction (Prompt 5.6) -- it never
retrains or re-predicts, purely a downstream evaluator of the six models'
per-model outputs.

Two of the doc's five outputs are honest reuse rather than fresh
computation, since they're already real, correctly-weighted numbers
produced upstream and re-deriving them a second way would just be a
second, potentially-inconsistent definition of the same thing:
- Consensus Probability = EnsemblePrediction.probability (Prompt 5.6's own
  real holdout-accuracy-weighted blend).
- Model Reliability = EnsemblePrediction.confidence (that same weighting
  applied to holdout accuracy instead of probability).

The three genuinely new metrics this chapter introduces:
- Prediction Variance: population variance of the raw per-model
  probabilities -- real units, distinct from ensemble.py's own
  `disagreement_score` (which is a *2, clipped-to-[0,1] transform of the
  same spread, tuned for the Uncertainty/Disagreement Score fields of
  Chapter 6, not for this chapter's own vocabulary).
- Agreement %: the fraction of models whose own direction (bullish /
  bearish / neutral, thresholded around the 0.5 coin-flip line) matches
  the ensemble's consensus direction -- this is the literal
  "LightGBM -> Bullish, CatBoost -> Bearish -> Don't trade" check the doc
  illustrates.
- Confidence Spread: the range (max - min) of per-model holdout accuracy
  -- how much the models disagree not on direction, but on how much to
  trust themselves.

"Only high-agreement predictions proceed": agreement_pct is bucketed into
high/medium/low (documented v1 thresholds, the same style as this
codebase's other regime-threshold heuristics, e.g. volatility.py), and
`proceed` is True only at the "high" bucket -- the actual trading gate
this chapter exists to build.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from statistics import pvariance
from typing import Any

from app.core.cache import CacheService
from app.core.config import Settings, get_settings
from app.prediction.ensemble import (
    DEFAULT_MAX_HOLDING_BARS,
    EnsemblePrediction,
    EnsemblePredictionEngine,
    ModelPrediction,
)

EVENT_TYPE = "model_agreement.result"

# Neutral band around the 0.5 coin flip: probabilities inside
# (0.5 - AGREEMENT_EPSILON, 0.5 + AGREEMENT_EPSILON) read as "neutral", not
# a directional call -- matches candidates.py's DIRECTION_EPSILON convention,
# adapted from a -1..1 signal scale to this module's 0..1 probability scale.
AGREEMENT_EPSILON = 0.05

HIGH_AGREEMENT_THRESHOLD = 0.8
MEDIUM_AGREEMENT_THRESHOLD = 0.5


def model_direction(probability: float, epsilon: float = AGREEMENT_EPSILON) -> str:
    if probability > 0.5 + epsilon:
        return "bullish"
    if probability < 0.5 - epsilon:
        return "bearish"
    return "neutral"


def agreement_level_for(agreement_pct: float) -> str:
    if agreement_pct >= HIGH_AGREEMENT_THRESHOLD:
        return "high"
    if agreement_pct >= MEDIUM_AGREEMENT_THRESHOLD:
        return "medium"
    return "low"


@dataclass(frozen=True)
class ModelReliability:
    name: str
    direction: str
    holdout_accuracy: float
    weight: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "direction": self.direction,
            "holdout_accuracy": self.holdout_accuracy,
            "weight": round(self.weight, 6),
        }


@dataclass
class AgreementResult:
    symbol: str
    snapshot_id: str
    as_of: datetime
    prediction_variance: float
    agreement_pct: float
    confidence_spread: float
    consensus_probability: float
    model_reliability: float
    agreement_level: str
    proceed: bool
    per_model_reliability: list[ModelReliability] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of.isoformat(),
            "prediction_variance": self.prediction_variance,
            "agreement_pct": self.agreement_pct,
            "confidence_spread": self.confidence_spread,
            "consensus_probability": self.consensus_probability,
            "model_reliability": self.model_reliability,
            "agreement_level": self.agreement_level,
            "proceed": self.proceed,
            "per_model_reliability": [m.to_dict() for m in self.per_model_reliability],
        }


def evaluate_agreement(prediction: EnsemblePrediction) -> AgreementResult:
    """Pure computation from an already-computed EnsemblePrediction -- no
    DB access, no retraining. With zero models (an untrained ensemble),
    there is no real agreement signal to report: every new metric is an
    honest zero and `proceed` is False, never a fabricated go-ahead."""
    model_predictions: Sequence[ModelPrediction] = prediction.model_predictions
    if not model_predictions:
        return AgreementResult(
            symbol=prediction.symbol, snapshot_id=prediction.snapshot_id, as_of=prediction.as_of,
            prediction_variance=0.0, agreement_pct=0.0, confidence_spread=0.0,
            consensus_probability=prediction.probability, model_reliability=prediction.confidence,
            agreement_level="low", proceed=False, per_model_reliability=[],
        )

    probabilities = [m.probability for m in model_predictions]
    accuracies = [m.holdout_accuracy for m in model_predictions]
    directions = [model_direction(p) for p in probabilities]
    consensus_direction = model_direction(prediction.probability)

    agreement_pct = round(
        sum(1 for d in directions if d == consensus_direction) / len(directions), 4
    )
    variance = round(pvariance(probabilities), 6) if len(probabilities) > 1 else 0.0
    spread = round(max(accuracies) - min(accuracies), 4) if len(accuracies) > 1 else 0.0
    level = agreement_level_for(agreement_pct)

    return AgreementResult(
        symbol=prediction.symbol, snapshot_id=prediction.snapshot_id, as_of=prediction.as_of,
        prediction_variance=variance, agreement_pct=agreement_pct, confidence_spread=spread,
        consensus_probability=prediction.probability, model_reliability=prediction.confidence,
        agreement_level=level, proceed=(level == "high"),
        per_model_reliability=[
            ModelReliability(
                name=m.name, direction=d, holdout_accuracy=m.holdout_accuracy, weight=m.weight
            )
            for m, d in zip(model_predictions, directions, strict=True)
        ],
    )


class ModelAgreementEngine:
    name = "model_agreement_engine"

    def __init__(
        self,
        session_factory: Any = None,
        cache: CacheService | None = None,
        settings: Settings | None = None,
        ensemble_engine: EnsemblePredictionEngine | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings or get_settings()
        self._ensemble = ensemble_engine or EnsemblePredictionEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )

    async def evaluate(
        self,
        symbol: str,
        timeframe: str = "D",
        direction: str = "long",
        lookback_bars: int = 500,
        max_holding_bars: int = DEFAULT_MAX_HOLDING_BARS,
    ) -> AgreementResult:
        """Fresh ensemble prediction (training on first call if not
        already cached), then the agreement evaluation over it."""
        prediction = await self._ensemble.predict(
            symbol, timeframe=timeframe, direction=direction,
            lookback_bars=lookback_bars, max_holding_bars=max_holding_bars,
        )
        result = evaluate_agreement(prediction)
        await self._persist(result)
        return result

    async def _persist(self, result: AgreementResult) -> None:
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
