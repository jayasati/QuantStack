"""Explainability Report (Volume 5, Prompt 5.16).

"Every signal must be fully explainable." This is the moment ensemble.py's
own docstring deferred to: "real SHAP deferred to Prompt 5.16 / Chapter
16 -- this uses feature_importance x standardized value, a documented v1
proxy." That proxy is now replaced with real SHAP values (the `shap`
library's TreeExplainer for every tree model, LinearExplainer for
Logistic Regression), averaged across models using the exact same
holdout-accuracy weighting the ensemble itself already blends
probabilities with -- not a fresh, second weighting scheme. If `shap`
isn't installed, this gracefully falls back to ensemble.py's own proxy
explanation (`explain_model`) rather than crashing, the same
optional-dependency degrade this codebase already established for
LightGBM/CatBoost/XGBoost themselves; `shap_available` on the report says
honestly which path was taken.

TreeExplainer needs no background dataset (its path-dependent mode reads
the tree structure directly); LinearExplainer does, but EnsembleTraining
doesn't retain the raw training matrix (only summary means/stds) --
Logistic Regression's SHAP values here are computed against a single-row
"mean case" background, a documented simplification, not the full
training-distribution background SHAP's own docs prefer.

Every other section of the report is honest reuse of an already-real,
already-computed value from an earlier engine -- nothing beyond Top SHAP
Features is new computation:
- Market Regime            -> MarketStateReportEngine.current_regimes (Vol 4, 4.15)
- Historical Analogs       -> HistoricalSimilarityEngine (5.9)
- Model Agreement          -> ModelAgreementEngine (5.8)
- Confidence Breakdown     -> the confidence value at each pipeline stage
                               (ensemble -> calibration -> market context ->
                               conviction), not a single number -- showing
                               where confidence was gained or lost along
                               the way.
- Conviction Breakdown     -> ConvictionResult.evidence (5.11) -- already a
                               complete per-source contribution list.
- Reason Codes             -> short machine-readable codes derived from
                               documented thresholds on the above (e.g.
                               HIGH_CONVICTION, MODEL_AGREEMENT_LOW).
- Natural Language Summary -> a template-assembled paragraph over the
                               above, the same "reasoning" idiom Volume 4's
                               IntelligenceResult.reasoning already uses,
                               joined into one coherent paragraph instead
                               of a list.

Calling calibration.predict() (via market_context and conviction, both of
which call it internally) multiple times per generate() call is an
accepted v1 redundancy, the same category snapshot.py's own docstring
already accepts for Market Confidence/Historical Analogs being fetched
twice per Market State Report.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from app.core.cache import CacheService
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.intelligence.report import MarketStateReportEngine
from app.prediction.agreement import AgreementResult, ModelAgreementEngine
from app.prediction.calibration import ProbabilityCalibrationEngine
from app.prediction.conviction import ConvictionEngine, ConvictionResult
from app.prediction.ensemble import (
    EnsemblePredictionEngine,
    EnsembleTraining,
    FeatureContribution,
    TrainedModel,
    explain_model,
    predict_from_training,
    standardize,
)
from app.prediction.historical_similarity import (
    HistoricalSimilarityEngine,
    HistoricalSimilarityResult,
)
from app.prediction.market_context import MarketContextAdjustmentEngine
from app.prediction.snapshot import FeatureSnapshotEngine

logger = get_logger(__name__)

try:
    import shap
except ImportError:
    shap = None  # type: ignore[assignment]

EVENT_TYPE = "explainability.report"

TOP_K_FEATURES = 5
STRONG_WIN_RATE_THRESHOLD = 0.6
WEAK_WIN_RATE_THRESHOLD = 0.4
HIGH_CONVICTION_GRADES = ("A", "B")


# --- Top SHAP Features -------------------------------------------------------


def _positive_class_shap(raw_values: Any) -> np.ndarray:
    """Normalizes across the shapes different shap explainers/model kinds
    return for one row of binary-classification input: sklearn tree
    ensembles return (1, n_features, n_classes); gradient-boosting
    libraries and LinearExplainer return (1, n_features) directly."""
    if isinstance(raw_values, list):
        return np.asarray(raw_values[-1])[0]
    array = np.asarray(raw_values)
    if array.ndim == 3:
        return array[0, :, -1]
    return array[0]


def _model_shap_values(
    trained: TrainedModel, x_raw: np.ndarray, background: np.ndarray
) -> np.ndarray:
    if trained.kind == "linear":
        explainer = shap.LinearExplainer(trained.model, background)
    else:
        explainer = shap.TreeExplainer(trained.model)
    return _positive_class_shap(explainer.shap_values(x_raw))


def compute_top_features(
    training: EnsembleTraining,
    feature_values: Mapping[str, float],
    top_k: int = TOP_K_FEATURES,
) -> tuple[list[FeatureContribution], bool]:
    """(top features, shap_available). Averages per-model contributions
    weighted by each model's own holdout-accuracy weight -- the same
    weighting `blend_predictions` uses for probabilities."""
    if not training.is_trained:
        return [], shap is not None

    feature_names = training.feature_names
    x_raw = np.array([[
        feature_values.get(name, training.feature_means.get(name, 0.0)) for name in feature_names
    ]])
    background = np.array([[training.feature_means.get(name, 0.0) for name in feature_names]])
    x_std = standardize(
        feature_values, feature_names, training.feature_means, training.feature_stds
    )

    accumulated = np.zeros(len(feature_names))
    total_weight = 0.0
    used_shap = shap is not None
    for trained in training.models:
        try:
            if used_shap:
                values = _model_shap_values(trained, x_raw, background)
            else:
                proxy = {
                    c.feature: c.contribution
                    for c in explain_model(trained, feature_names, x_std)
                }
                values = np.array([proxy.get(name, 0.0) for name in feature_names])
        except Exception:
            logger.warning("SHAP computation failed for model: %s", trained.name, exc_info=True)
            continue
        accumulated += trained.weight * values
        total_weight += trained.weight

    if total_weight <= 0:
        return [], used_shap

    averaged = accumulated / total_weight
    contributions = [
        FeatureContribution(feature=name, contribution=round(float(value), 6))
        for name, value in zip(feature_names, averaged, strict=True)
    ]
    contributions.sort(key=lambda c: abs(c.contribution), reverse=True)
    return contributions[:top_k], used_shap


# --- Reason Codes / Natural Language Summary ---------------------------


def build_reason_codes(
    conviction: ConvictionResult,
    agreement: AgreementResult,
    similarity: HistoricalSimilarityResult,
) -> list[str]:
    codes: list[str] = []

    if conviction.conviction_grade in HIGH_CONVICTION_GRADES:
        codes.append("HIGH_CONVICTION")
    elif conviction.conviction_grade == "F":
        codes.append("LOW_CONVICTION")

    if conviction.conviction_trend == "improving":
        codes.append("CONVICTION_IMPROVING")
    elif conviction.conviction_trend == "declining":
        codes.append("CONVICTION_DECLINING")

    if agreement.agreement_level == "high":
        codes.append("MODEL_AGREEMENT_HIGH")
    elif agreement.agreement_level == "low":
        codes.append("MODEL_AGREEMENT_LOW")

    if similarity.historical_win_rate is not None:
        if similarity.historical_win_rate >= STRONG_WIN_RATE_THRESHOLD:
            codes.append("STRONG_HISTORICAL_PRECEDENT")
        elif similarity.historical_win_rate <= WEAK_WIN_RATE_THRESHOLD:
            codes.append("WEAK_HISTORICAL_PRECEDENT")

    return codes


def _dominant_regime_phrase(regime: Mapping[str, str | None]) -> str:
    parts = [value.replace("_", " ") for value in regime.values() if value]
    return ", ".join(parts) if parts else "regime data unavailable"


def build_natural_language_summary(
    symbol: str,
    direction: str,
    conviction: ConvictionResult,
    agreement: AgreementResult,
    similarity: HistoricalSimilarityResult,
    regime: Mapping[str, str | None],
    top_features: Sequence[FeatureContribution],
) -> str:
    sentences = [
        f"{symbol} ({direction}): Conviction grade {conviction.conviction_grade} "
        f"({conviction.conviction_score:.0f}/100, {conviction.conviction_trend}).",
        f"{agreement.agreement_pct:.0%} of models agree ({agreement.agreement_level} agreement), "
        f"consensus probability {agreement.consensus_probability:.0%}.",
    ]
    if similarity.historical_win_rate is not None:
        sentences.append(
            f"Historical analogs show a {similarity.historical_win_rate:.0%} win rate "
            f"over {similarity.n_analogs} similar setups "
            f"(avg return {similarity.average_return:+.2%})."
        )
    else:
        sentences.append("No reliable historical analogs were found for this setup.")

    sentences.append(f"Current regime: {_dominant_regime_phrase(regime)}.")

    if top_features:
        drivers = ", ".join(
            f"{f.feature} ({'+' if f.contribution >= 0 else '-'})" for f in top_features[:3]
        )
        sentences.append(f"Top drivers: {drivers}.")

    return " ".join(sentences)


# --- Report -----------------------------------------------------------------


@dataclass
class ExplainabilityReport:
    symbol: str
    direction: str
    snapshot_id: str
    as_of: datetime
    top_features: list[FeatureContribution]
    shap_available: bool
    market_regime: dict[str, str | None]
    historical_analogs: dict[str, Any]
    model_agreement: dict[str, Any]
    confidence_breakdown: dict[str, float]
    conviction_breakdown: list[dict[str, Any]]
    reason_codes: list[str] = field(default_factory=list)
    natural_language_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of.isoformat(),
            "top_features": [f.to_dict() for f in self.top_features],
            "shap_available": self.shap_available,
            "market_regime": self.market_regime,
            "historical_analogs": self.historical_analogs,
            "model_agreement": self.model_agreement,
            "confidence_breakdown": self.confidence_breakdown,
            "conviction_breakdown": self.conviction_breakdown,
            "reason_codes": self.reason_codes,
            "natural_language_summary": self.natural_language_summary,
        }


class ExplainabilityReportEngine:
    name = "explainability_report_engine"

    def __init__(
        self,
        session_factory: Any = None,
        cache: CacheService | None = None,
        settings: Settings | None = None,
        ensemble_engine: EnsemblePredictionEngine | None = None,
        calibration_engine: ProbabilityCalibrationEngine | None = None,
        market_context_engine: MarketContextAdjustmentEngine | None = None,
        conviction_engine: ConvictionEngine | None = None,
        agreement_engine: ModelAgreementEngine | None = None,
        historical_similarity_engine: HistoricalSimilarityEngine | None = None,
        snapshot_engine: FeatureSnapshotEngine | None = None,
        report_engine: MarketStateReportEngine | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings or get_settings()
        self._ensemble = ensemble_engine or EnsemblePredictionEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._calibration = calibration_engine or ProbabilityCalibrationEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._market_context = market_context_engine or MarketContextAdjustmentEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._conviction = conviction_engine or ConvictionEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._agreement = agreement_engine or ModelAgreementEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._historical_similarity = historical_similarity_engine or HistoricalSimilarityEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._snapshots = snapshot_engine or FeatureSnapshotEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._report = report_engine or MarketStateReportEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )

    async def generate(
        self, symbol: str, timeframe: str = "D", direction: str = "long"
    ) -> ExplainabilityReport:
        training = await self._ensemble.train(symbol, timeframe=timeframe, direction=direction)
        snapshot = await self._snapshots.capture(symbol, timeframe=timeframe)
        top_features, shap_available = compute_top_features(training, snapshot.feature_values)
        raw_prediction = predict_from_training(training, snapshot)

        market_report = await self._report.generate(symbol)
        calibrated = await self._calibration.predict(
            symbol, timeframe=timeframe, direction=direction
        )
        context = await self._market_context.evaluate(
            symbol, timeframe=timeframe, direction=direction
        )
        conviction = await self._conviction.evaluate(symbol, direction=direction)
        agreement = await self._agreement.evaluate(
            symbol, timeframe=timeframe, direction=direction
        )
        similarity = await self._historical_similarity.evaluate(symbol, direction=direction)

        confidence_breakdown = {
            "ensemble_confidence": raw_prediction.confidence,
            "calibration_confidence": calibrated.calibration_confidence,
            "market_context_confidence": context.adjusted_confidence,
            "conviction_confidence": conviction.conviction_confidence,
        }
        reason_codes = build_reason_codes(conviction, agreement, similarity)
        summary = build_natural_language_summary(
            symbol, direction, conviction, agreement, similarity,
            market_report.current_regimes, top_features,
        )

        report = ExplainabilityReport(
            symbol=symbol, direction=direction, snapshot_id=snapshot.snapshot_id,
            as_of=snapshot.as_of, top_features=top_features, shap_available=shap_available,
            market_regime=dict(market_report.current_regimes),
            historical_analogs=similarity.to_dict(), model_agreement=agreement.to_dict(),
            confidence_breakdown=confidence_breakdown,
            conviction_breakdown=[e.to_dict() for e in conviction.evidence],
            reason_codes=reason_codes, natural_language_summary=summary,
        )
        await self._persist(report)
        return report

    async def _persist(self, report: ExplainabilityReport) -> None:
        if self._sessions is None:
            return
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            session.add(MarketEvent(
                event_type=EVENT_TYPE,
                source=self.name,
                data=report.to_dict(),
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
