"""Ensemble Prediction Engine (Volume 5, Prompt 5.6).

"Do not trust a single model." Six classifiers are trained against real
Triple Barrier labels (Prompt 5.5, labeling.py) using a point-in-time join
against the same raw feature_store names Volume 4's intelligence engines
already treat as meaningful signal -- trend.py/structure.py/volatility.py/
relative.py/liquidity.py/breadth.py/institutional_flow.py/events.py/
options.py -- not an arbitrarily invented feature list. The as-of lookup
(last observation at or before each label's entry_ts) avoids lookahead
bias: training never sees a feature value from after the trade it's trying
to predict.

LightGBM/CatBoost/XGBoost are optional soft dependencies (module-level
try/except import). If a library isn't installed, that model is simply
skipped from the ensemble -- an honest degrade, not a fabrication, matching
this codebase's established graceful-degradation convention (Redis
outages in cache.py, FinBERT->lexicon fallback in news.py). scikit-learn
(Random Forest, Extra Trees, Logistic Regression baseline) is a hard
dependency since the doc requires those three unconditionally.

Blending is "weighted averaging" using REAL weights, not fixed/arbitrary
ones: each model's weight is its own skill-above-chance holdout accuracy
(a chronological train/holdout split -- no shuffling, since shuffling would
leak future rows into training). Confidence is that same weighted blend of
holdout accuracy: how much the ensemble has actually been shown to know.
Uncertainty is a separate notion -- how close the blended probability sits
to a coin flip -- and Disagreement Score is the cross-model spread, kept
distinct per the doc's own vocabulary (Chapter 8's Model Agreement Engine,
Prompt 5.8, will consume this same disagreement signal to gate trading).

Per-model explanations use a documented v1 proxy (coefficient x
standardized value for Logistic Regression -- exact; feature_importance x
standardized value for every tree model -- a heuristic, not real SHAP,
since Chapter 16 / Prompt 5.16 is where the doc actually introduces SHAP).

Training is on-demand and held in memory per engine instance (train() then
predict()), the same "runs on demand, not scheduled" shape as Prompt 5.5's
label_history() -- there is no model-artifact blob store in this codebase
to persist a serialized estimator into, so model_version is a descriptive
string (algorithm set + training timestamp + sample count), not a
retrievable handle.
"""

import asyncio
import bisect
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import pstdev
from typing import Any

import numpy as np

from app.core.cache import CacheService
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.events.bus import Event, EventBus
from app.features.store import FeatureStore
from app.prediction.labeling import (
    DEFAULT_MAX_HOLDING_BARS,
    Label,
    TripleBarrierLabelingEngine,
)
from app.prediction.snapshot import FeatureSnapshot, FeatureSnapshotEngine

logger = get_logger(__name__)

try:
    from sklearn.ensemble import ExtraTreesClassifier, RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
except ImportError:  # pragma: no cover - scikit-learn is a hard dependency
    RandomForestClassifier = ExtraTreesClassifier = LogisticRegression = None  # type: ignore[assignment, misc]

try:
    from lightgbm import LGBMClassifier
except ImportError:
    LGBMClassifier = None  # type: ignore[assignment, misc]

try:
    from xgboost import XGBClassifier
except ImportError:
    XGBClassifier = None  # type: ignore[assignment, misc]

try:
    from catboost import CatBoostClassifier
except ImportError:
    CatBoostClassifier = None  # type: ignore[assignment, misc]

EVENT_TYPE = "ensemble_prediction.result"

RANDOM_STATE = 42
MIN_TRAINING_SAMPLES = 40  # below this, an untrained/neutral result is returned honestly
MIN_LABEL_QUALITY = 0.2  # matches labeling.py's own quality floor for ambiguous/insufficient labels
MIN_FEATURE_COVERAGE = 0.6  # a training row needs at least 60% of the feature set present
HOLDOUT_FRACTION = 0.2  # chronological, not random -- no lookahead into the training split
FEATURE_HISTORY_LIMIT = 2000  # matches labeling.py's own per-feature fetch cap
TOP_K_EXPLANATION_FEATURES = 5

INSTRUMENT = "instrument"  # symbol_mode: join on the traded symbol itself
MARKET = "MARKET"  # symbol_mode: global-market feature, same value for every instrument

# The exact raw feature_store names Volume 4's intelligence engines
# (app/intelligence/trend.py, structure.py, volatility.py, relative.py,
# liquidity.py, breadth.py, institutional_flow.py, events.py) already read
# as meaningful signal -- reused here rather than invented, so training
# inputs are the same fields this codebase has already validated matter.
ENSEMBLE_FEATURE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("price_momentum_20", INSTRUMENT, "D"),
    ("price_acceleration_20", INSTRUMENT, "D"),
    ("price_dist_from_high_50", INSTRUMENT, "D"),
    ("price_dist_from_low_50", INSTRUMENT, "D"),
    ("volume_rvol_20", INSTRUMENT, "D"),
    ("volume_obv_z", INSTRUMENT, "D"),
    ("ms_trend_direction", INSTRUMENT, "D"),
    ("ms_structural_bias", INSTRUMENT, "D"),
    ("ms_breakout_probability", INSTRUMENT, "D"),
    ("ms_sweep_probability", INSTRUMENT, "D"),
    ("ms_change_of_character", INSTRUMENT, "D"),
    ("ms_break_of_structure", INSTRUMENT, "D"),
    ("volatility_regime_20", INSTRUMENT, "D"),
    ("volatility_expected_move_20", INSTRUMENT, "D"),
    ("liquidity_score", INSTRUMENT, "quote"),
    ("liquidity_spread_pct", INSTRUMENT, "quote"),
    ("liquidity_market_impact_pct", INSTRUMENT, "quote"),
    ("liquidity_order_book_imbalance", INSTRUMENT, "quote"),
    ("rs_nifty_strength_20", INSTRUMENT, "D"),
    ("rs_outperformance_20", INSTRUMENT, "D"),
    ("breadth_health_score", MARKET, "breadth"),
    ("breadth_participation_pct", MARKET, "breadth"),
    ("flow_participation_index", MARKET, "flow"),
    ("event_trading_freeze", MARKET, "events"),
    ("event_expected_volatility", MARKET, "events"),
    # app/intelligence/options.py (added once OptionsIntelligenceEngine
    # closed the gap between these columns and this consumer).
    ("options_pcr", INSTRUMENT, "chain"),
    ("options_max_pain_distance_pct", INSTRUMENT, "chain"),
    ("options_iv_rank", INSTRUMENT, "chain"),
    ("options_gamma_exposure", INSTRUMENT, "chain"),
    ("options_dealer_positioning", INSTRUMENT, "chain"),
)
FEATURE_NAMES: tuple[str, ...] = tuple(spec[0] for spec in ENSEMBLE_FEATURE_SPECS)
# The "D" subset has ~2 years of history; the quote/chain/breadth/flow/events
# subset (options intelligence, institutional flow, market breadth) only
# started being collected in the last ~1-2 days of this project's own build
# history (verified live 2026-07-17: every one of those features' first-ever
# row is between 2026-07-15 23:17 and 2026-07-16 14:55). Coverage is gated on
# this "core" subset alone -- the newer features are still included in every
# training row whenever available (mean-imputed otherwise, the existing
# convention), they just can't be a REQUIREMENT for a row to qualify, or no
# historical label before ~2026-07-16 could ever pass and training could
# never produce a model until ~40 days of new-feature history accumulates.
# Self-maintaining: any future D-timeframe addition to ENSEMBLE_FEATURE_SPECS
# automatically becomes core, any non-D addition automatically stays optional.
CORE_FEATURE_NAMES: tuple[str, ...] = tuple(
    name for name, _, timeframe in ENSEMBLE_FEATURE_SPECS if timeframe == "D"
)


# --- Pure math: disagreement / uncertainty / blending -----------------------


def disagreement_score(probabilities: Sequence[float]) -> float:
    """Population stdev of per-model probabilities, doubled and clipped to
    0..1. Max possible stdev of values confined to [0, 1] is 0.5 (half the
    models at 0.0, half at 1.0), so *2 maps full disagreement to exactly
    1.0. Distinct from `uncertainty`: models can unanimously agree on a
    coin-flip probability (high uncertainty, zero disagreement)."""
    if len(probabilities) < 2:
        return 0.0
    return round(min(pstdev(probabilities) * 2.0, 1.0), 4)


def uncertainty(probability: float) -> float:
    """Symmetric around 0.5: maximally uncertain (1.0) when the blended
    probability is a coin flip, 0.0 when fully resolved toward 0 or 1."""
    return round(1.0 - 2.0 * abs(probability - 0.5), 4)


def blend_predictions(model_predictions: Sequence["ModelPrediction"]) -> tuple[float, float]:
    """Weighted average by each model's holdout skill-above-chance weight.
    Confidence is that same weighted blend, but of each model's holdout
    accuracy -- how much the ensemble has actually been shown to know, as
    opposed to `uncertainty`, which only looks at the blended probability
    itself."""
    if not model_predictions:
        return 0.5, 0.0
    total_weight = sum(m.weight for m in model_predictions)
    if total_weight <= 0:
        n = len(model_predictions)
        probability = sum(m.probability for m in model_predictions) / n
        confidence = sum(m.holdout_accuracy for m in model_predictions) / n
        return probability, confidence
    probability = sum(m.probability * m.weight for m in model_predictions) / total_weight
    confidence = sum(m.holdout_accuracy * m.weight for m in model_predictions) / total_weight
    return probability, confidence


# --- Point-in-time feature join ----------------------------------------------


def _as_of_value(series: Sequence[tuple[datetime, float]], at: datetime) -> float | None:
    """Last observation at or before `at` in a (ts ascending, value) series
    -- a point-in-time lookup. Never returns a value from after `at`, so
    training never leaks the future into a label's feature row."""
    if not series:
        return None
    timestamps = [ts for ts, _ in series]
    idx = bisect.bisect_right(timestamps, at) - 1
    return series[idx][1] if idx >= 0 else None


@dataclass(frozen=True)
class TrainingRow:
    ts: datetime
    features: dict[str, float]
    label: int  # 1 = win/partial_success, 0 = loss/timeout


def assemble_dataset(
    labels: Sequence[Label],
    feature_series: Mapping[str, Sequence[tuple[datetime, float]]],
    feature_names: Sequence[str] = FEATURE_NAMES,
    min_coverage: float = MIN_FEATURE_COVERAGE,
    core_feature_names: Sequence[str] = CORE_FEATURE_NAMES,
) -> list[TrainingRow]:
    """One row per label with enough feature coverage as-of its entry_ts.
    Labels are pre-filtered by label_quality upstream (train()); this
    function only gates on feature availability.

    Coverage is checked against `core_feature_names` (the long-history "D"
    subset) only -- newer, shorter-history features are still opportunistically
    included in `values` whenever available, they just aren't REQUIRED for a
    row to qualify. Gating on the full feature list would mean no label
    predating a feature's very first collected row could ever qualify; with
    several features only ~1-2 days old against ~2 years of label history,
    that would make training impossible until the newer features accumulate
    ~40 days of their own history."""
    rows: list[TrainingRow] = []
    for label in labels:
        values: dict[str, float] = {}
        for name in feature_names:
            value = _as_of_value(feature_series.get(name, ()), label.entry_ts)
            if value is not None:
                values[name] = value
        core_present = sum(1 for name in core_feature_names if name in values)
        if core_present / len(core_feature_names) < min_coverage:
            continue
        target = 1 if label.label in ("win", "partial_success") else 0
        rows.append(TrainingRow(ts=label.entry_ts, features=values, label=target))
    return rows


def feature_stats(
    rows: Sequence[TrainingRow], feature_names: Sequence[str] = FEATURE_NAMES
) -> tuple[dict[str, float], dict[str, float]]:
    """Per-feature mean/std across whatever rows actually have that feature
    -- used both to mean-impute rows missing that feature and to
    standardize values for per-model explanations."""
    means: dict[str, float] = {}
    stds: dict[str, float] = {}
    for name in feature_names:
        values = [row.features[name] for row in rows if name in row.features]
        if not values:
            means[name] = 0.0
            stds[name] = 0.0
            continue
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        means[name] = mean
        stds[name] = math.sqrt(variance)
    return means, stds


def standardize(
    feature_values: Mapping[str, float],
    feature_names: Sequence[str],
    means: Mapping[str, float],
    stds: Mapping[str, float],
) -> list[float]:
    result = []
    for name in feature_names:
        value = feature_values.get(name, means.get(name, 0.0))
        std = stds.get(name, 0.0)
        result.append((value - means.get(name, 0.0)) / std if std > 0 else 0.0)
    return result


# --- Models -------------------------------------------------------------


def _model_factories() -> list[tuple[str, str, Any]]:
    """(name, kind, fresh estimator). Only libraries actually importable
    are included -- LightGBM/CatBoost/XGBoost are optional; scikit-learn's
    three (the doc's mandatory baseline trio) are not."""
    factories: list[tuple[str, str, Any]] = []
    if LGBMClassifier is not None:
        factories.append((
            "lightgbm", "tree",
            LGBMClassifier(
                n_estimators=200, learning_rate=0.05,
                random_state=RANDOM_STATE, verbose=-1,
            ),
        ))
    if CatBoostClassifier is not None:
        factories.append((
            "catboost", "tree",
            CatBoostClassifier(
                iterations=200, depth=6, learning_rate=0.05,
                random_state=RANDOM_STATE, verbose=False,
            ),
        ))
    if XGBClassifier is not None:
        factories.append((
            "xgboost", "tree",
            XGBClassifier(
                n_estimators=200, max_depth=6, learning_rate=0.05,
                random_state=RANDOM_STATE, eval_metric="logloss",
            ),
        ))
    if RandomForestClassifier is not None:
        factories.append((
            "random_forest", "tree",
            RandomForestClassifier(n_estimators=200, max_depth=8, random_state=RANDOM_STATE),
        ))
    if ExtraTreesClassifier is not None:
        factories.append((
            "extra_trees", "tree",
            ExtraTreesClassifier(n_estimators=200, max_depth=8, random_state=RANDOM_STATE),
        ))
    if LogisticRegression is not None:
        factories.append(("logistic_regression", "linear", LogisticRegression(max_iter=1000)))
    return factories


@dataclass
class TrainedModel:
    name: str
    model: Any
    kind: str  # "linear" | "tree"
    weight: float
    holdout_accuracy: float


def train_models(
    rows: Sequence[TrainingRow],
    feature_names: Sequence[str] = FEATURE_NAMES,
    means: Mapping[str, float] | None = None,
) -> tuple[list[TrainedModel], int]:
    """Chronological (not shuffled) train/holdout split -- rows must
    already be time-ordered. Each model's weight is its own
    skill-above-chance holdout accuracy, floored at a small epsilon so a
    model no better than a coin flip still contributes almost nothing
    rather than a hard zero (documented v1 choice: a real weighted
    average, not fabricated equal weights)."""
    means = means or feature_stats(rows, feature_names)[0]
    X = np.array([[row.features.get(f, means[f]) for f in feature_names] for row in rows])
    y = np.array([row.label for row in rows])

    split = max(1, min(len(rows) - 1, round(len(rows) * (1 - HOLDOUT_FRACTION))))
    X_train, X_holdout = X[:split], X[split:]
    y_train, y_holdout = y[:split], y[split:]

    trained: list[TrainedModel] = []
    if len(set(y_train.tolist())) < 2:
        return trained, split  # can't fit a classifier on a single class

    for name, kind, estimator in _model_factories():
        try:
            estimator.fit(X_train, y_train)
        except Exception:
            logger.warning("ensemble model failed to fit: %s", name, exc_info=True)
            continue
        if len(X_holdout):
            accuracy = float((estimator.predict(X_holdout) == y_holdout).mean())
        else:
            accuracy = 0.5  # no holdout available -- neutral, undocked weight
        weight = max(accuracy - 0.5, 0.0) + 1e-6
        trained.append(TrainedModel(
            name=name, model=estimator, kind=kind,
            weight=weight, holdout_accuracy=round(accuracy, 4),
        ))
    return trained, split


def blended_probabilities(models: Sequence[TrainedModel], X: np.ndarray) -> list[float]:
    """Weighted-average probability across an already-trained ensemble for
    every row in X at once (vectorized `blend_predictions`, without the
    per-model ModelPrediction bookkeeping that only matters at single-row
    inference time). Used to build the out-of-sample (raw_probability,
    outcome) calibration set below: one call over the holdout split
    already carved out by train_models(), so calibration is fit on rows no
    model was trained on."""
    if not models or len(X) == 0:
        return [0.5] * len(X)
    total_weight = sum(m.weight for m in models)
    per_model = np.array([m.model.predict_proba(X)[:, 1] for m in models])
    if total_weight <= 0:
        return per_model.mean(axis=0).tolist()
    weights = np.array([m.weight for m in models]).reshape(-1, 1)
    return ((per_model * weights).sum(axis=0) / total_weight).tolist()


def _fit_and_calibrate(
    rows: Sequence[TrainingRow], means: Mapping[str, float]
) -> tuple[list[TrainedModel], int, list[tuple[float, int]]]:
    """Synchronous, CPU-bound: fits every model (train_models -- six
    estimator.fit() calls) and builds the out-of-sample calibration set
    (blended_probabilities over the holdout). Bundled into one function so
    EnsemblePredictionEngine.train() can dispatch the whole unit of
    blocking work to a worker thread via asyncio.to_thread in a single
    call, rather than freezing the event loop for the full training
    duration -- scikit-learn/LightGBM/XGBoost/CatBoost all release the GIL
    during their actual fit/predict computation, so this yields real
    concurrency, not just cosmetic scheduling."""
    models, split = train_models(rows, means=means)
    holdout_rows = rows[split:]
    calibration_pairs: list[tuple[float, int]] = []
    if models and holdout_rows:
        X_holdout = np.array([
            [row.features.get(f, means[f]) for f in FEATURE_NAMES] for row in holdout_rows
        ])
        probabilities = blended_probabilities(models, X_holdout)
        calibration_pairs = list(
            zip(probabilities, [row.label for row in holdout_rows], strict=True)
        )
    return models, split, calibration_pairs


# --- Explanations ---------------------------------------------------------


@dataclass(frozen=True)
class FeatureContribution:
    feature: str
    contribution: float

    def to_dict(self) -> dict[str, Any]:
        return {"feature": self.feature, "contribution": self.contribution}


def explain_model(
    model: TrainedModel, feature_names: Sequence[str], x_std: Sequence[float]
) -> list[FeatureContribution]:
    """Per-model explanation. Logistic Regression: coefficient x
    standardized value, exact. Every tree model: feature_importance x
    standardized value, a documented v1 proxy for real SHAP (Prompt 5.16 /
    Chapter 16 is where the doc actually introduces SHAP)."""
    weights: np.ndarray | None
    if model.kind == "linear":
        weights = np.asarray(model.model.coef_).reshape(-1)
    else:
        weights = getattr(model.model, "feature_importances_", None)
    if weights is None:
        return []
    contributions = [
        FeatureContribution(feature=name, contribution=round(float(w * v), 6))
        for name, w, v in zip(feature_names, weights, x_std, strict=True)
    ]
    contributions.sort(key=lambda c: abs(c.contribution), reverse=True)
    return contributions[:TOP_K_EXPLANATION_FEATURES]


# --- Records --------------------------------------------------------------


@dataclass
class ModelPrediction:
    name: str
    probability: float
    weight: float
    holdout_accuracy: float
    explanation: list[FeatureContribution] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "probability": self.probability,
            "weight": round(self.weight, 6),
            "holdout_accuracy": self.holdout_accuracy,
            "explanation": [c.to_dict() for c in self.explanation],
        }


@dataclass
class EnsembleTraining:
    """In-memory training artifact for one (symbol, timeframe, direction).
    No blob store exists in this codebase to persist serialized estimators
    into, so this lives for the lifetime of the engine instance -- train()
    then predict(), the same on-demand shape as Prompt 5.5's
    label_history()."""

    symbol: str
    timeframe: str
    direction: str
    trained_at: datetime
    n_samples: int
    n_holdout: int
    feature_names: tuple[str, ...]
    feature_means: dict[str, float]
    feature_stds: dict[str, float]
    model_version: str
    models: list[TrainedModel] = field(default_factory=list)
    # Out-of-sample (raw blended probability, actual outcome) pairs from
    # this same training's holdout split -- Prompt 5.7's Probability
    # Calibration Engine (app/prediction/calibration.py) fits against
    # these rather than re-deriving its own split, so calibration is
    # always evaluated on rows no model here was trained on.
    calibration_pairs: list[tuple[float, int]] = field(default_factory=list)

    @property
    def is_trained(self) -> bool:
        return bool(self.models)

    def to_dict(self) -> dict[str, Any]:
        """Excludes the raw fitted estimators -- only their names/weights/
        holdout accuracy are meaningful to a caller."""
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "trained_at": self.trained_at.isoformat(),
            "n_samples": self.n_samples,
            "n_holdout": self.n_holdout,
            "feature_names": list(self.feature_names),
            "model_version": self.model_version,
            "models": [
                {"name": m.name, "kind": m.kind, "weight": round(m.weight, 6),
                 "holdout_accuracy": m.holdout_accuracy}
                for m in self.models
            ],
            "n_calibration_pairs": len(self.calibration_pairs),
        }


@dataclass
class EnsemblePrediction:
    symbol: str
    snapshot_id: str
    as_of: datetime
    probability: float
    confidence: float
    uncertainty: float
    disagreement_score: float
    model_version: str
    training_samples: int
    model_predictions: list[ModelPrediction] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of.isoformat(),
            "probability": self.probability,
            "confidence": self.confidence,
            "uncertainty": self.uncertainty,
            "disagreement_score": self.disagreement_score,
            "model_version": self.model_version,
            "training_samples": self.training_samples,
            "model_predictions": [m.to_dict() for m in self.model_predictions],
        }


def predict_from_training(
    training: EnsembleTraining, snapshot: FeatureSnapshot
) -> EnsemblePrediction:
    """Pure computation from an already-trained ensemble and an
    already-frozen snapshot -- no DB access. This IS the "Snapshot ->
    Prediction" flow Prompt 5.3 requires: re-running this against the same
    training artifact and the same persisted snapshot always reproduces
    the same prediction exactly."""
    if not training.is_trained:
        # Never fabricate confidence a model doesn't have: honestly neutral.
        return EnsemblePrediction(
            symbol=snapshot.symbol, snapshot_id=snapshot.snapshot_id, as_of=snapshot.as_of,
            probability=0.5, confidence=0.0, uncertainty=1.0, disagreement_score=0.0,
            model_version=training.model_version, training_samples=training.n_samples,
            model_predictions=[],
        )

    x_raw = [
        snapshot.feature_values.get(name, training.feature_means.get(name, 0.0))
        for name in training.feature_names
    ]
    x_std = standardize(
        snapshot.feature_values, training.feature_names,
        training.feature_means, training.feature_stds,
    )
    X = np.array([x_raw])

    model_predictions = []
    for trained in training.models:
        probability = float(trained.model.predict_proba(X)[0][1])
        model_predictions.append(ModelPrediction(
            name=trained.name,
            probability=round(probability, 4),
            weight=trained.weight,
            holdout_accuracy=trained.holdout_accuracy,
            explanation=explain_model(trained, training.feature_names, x_std),
        ))

    probability, confidence = blend_predictions(model_predictions)
    return EnsemblePrediction(
        symbol=snapshot.symbol, snapshot_id=snapshot.snapshot_id, as_of=snapshot.as_of,
        probability=round(probability, 4),
        confidence=round(confidence, 4),
        uncertainty=uncertainty(probability),
        disagreement_score=disagreement_score([m.probability for m in model_predictions]),
        model_version=training.model_version,
        training_samples=training.n_samples,
        model_predictions=model_predictions,
    )


class EnsemblePredictionEngine:
    name = "ensemble_prediction_engine"

    def __init__(
        self,
        session_factory: Any = None,
        cache: CacheService | None = None,
        settings: Settings | None = None,
        bus: EventBus | None = None,
        labeling_engine: TripleBarrierLabelingEngine | None = None,
        snapshot_engine: FeatureSnapshotEngine | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings or get_settings()
        self._bus = bus
        self.store = FeatureStore(session_factory=session_factory, cache=cache)
        self._labeling = labeling_engine or TripleBarrierLabelingEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._snapshots = snapshot_engine or FeatureSnapshotEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._trained: dict[tuple[str, str, str], EnsembleTraining] = {}

    async def train(
        self,
        symbol: str,
        timeframe: str = "D",
        direction: str = "long",
        lookback_bars: int = 500,
        max_holding_bars: int = DEFAULT_MAX_HOLDING_BARS,
    ) -> EnsembleTraining:
        """Assembles a point-in-time-joined training set from Triple Barrier
        labels (Prompt 5.5) + historical feature_store values, then fits
        every available model. Result is cached in memory for predict()."""
        labels = await self._labeling.label_history(
            symbol, timeframe=timeframe, direction=direction,
            lookback_bars=lookback_bars, max_holding_bars=max_holding_bars,
        )
        quality_labels = [label for label in labels if label.label_quality >= MIN_LABEL_QUALITY]
        feature_series = await self._fetch_feature_series(symbol)
        rows = assemble_dataset(quality_labels, feature_series)

        trained_at = datetime.now(UTC)
        if len(rows) < MIN_TRAINING_SAMPLES:
            training = EnsembleTraining(
                symbol=symbol, timeframe=timeframe, direction=direction,
                trained_at=trained_at, n_samples=len(rows), n_holdout=0,
                feature_names=FEATURE_NAMES, feature_means={}, feature_stds={},
                model_version=f"ensemble_v1-untrained-n{len(rows)}", models=[],
            )
        else:
            means, stds = feature_stats(rows)
            # Offloaded to a worker thread: fitting six models is CPU-bound
            # and would otherwise block the entire event loop for the full
            # training duration (see _fit_and_calibrate's own docstring).
            models, split, calibration_pairs = await asyncio.to_thread(
                _fit_and_calibrate, rows, means
            )
            training = EnsembleTraining(
                symbol=symbol, timeframe=timeframe, direction=direction,
                trained_at=trained_at, n_samples=len(rows), n_holdout=len(rows) - split,
                feature_names=FEATURE_NAMES, feature_means=means, feature_stds=stds,
                model_version=f"ensemble_v1-{trained_at.strftime('%Y%m%dT%H%M%S')}-n{len(rows)}",
                models=models, calibration_pairs=calibration_pairs,
            )
        self._trained[(symbol, timeframe, direction)] = training
        return training

    async def predict(
        self,
        symbol: str,
        timeframe: str = "D",
        direction: str = "long",
        lookback_bars: int = 500,
        max_holding_bars: int = DEFAULT_MAX_HOLDING_BARS,
    ) -> EnsemblePrediction:
        """Convenience: train (if not already cached for this key), capture
        a fresh snapshot, then predict from it."""
        key = (symbol, timeframe, direction)
        training = self._trained.get(key)
        if training is None:
            training = await self.train(
                symbol, timeframe=timeframe, direction=direction,
                lookback_bars=lookback_bars, max_holding_bars=max_holding_bars,
            )
        snapshot = await self._snapshots.capture(symbol, timeframe=timeframe)
        prediction = predict_from_training(training, snapshot)
        await self._persist(prediction)
        return prediction

    async def _fetch_feature_series(
        self, symbol: str
    ) -> dict[str, list[tuple[datetime, float]]]:
        """Fetched once per train() call (not once per label), matching
        labeling.py's own `_breach_dates` convention."""
        series: dict[str, list[tuple[datetime, float]]] = {}
        for feature_name, symbol_mode, timeframe in ENSEMBLE_FEATURE_SPECS:
            key_symbol = symbol if symbol_mode == INSTRUMENT else symbol_mode
            rows = await self.store.history(
                feature_name, symbol=key_symbol, timeframe=timeframe, limit=FEATURE_HISTORY_LIMIT,
            )
            series[feature_name] = sorted(
                (datetime.fromisoformat(row["ts"]), row["value"])
                for row in rows if row["value"] is not None
            )
        return series

    async def _persist(self, prediction: EnsemblePrediction) -> None:
        if self._bus is not None:
            await self._bus.publish(
                Event(type=EVENT_TYPE, payload=prediction.to_dict(), source=self.name)
            )
        if self._sessions is None:
            return
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            session.add(MarketEvent(
                event_type=EVENT_TYPE,
                source=self.name,
                data=prediction.to_dict(),
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
