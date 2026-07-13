"""Bayesian Probability Calibration (Volume 5, Prompt 5.7).

"Raw ML probabilities are often overconfident" -- a classic, well-studied
ML symptom, not specific to this codebase. Three calibration methods are
fit against the SAME out-of-sample (raw_probability, actual_outcome) pairs
the Ensemble Prediction Engine already produces on its own holdout split
during training (EnsembleTraining.calibration_pairs, Prompt 5.6) --
calibration never touches a row any ensemble model was fit on.

Supported methods:
- Platt Scaling: the classical two-parameter sigmoid recalibration,
  sigmoid(A * logit(raw) + B), fit by 1D logistic regression. Classic
  Platt scaling fits on a raw decision-function score; the ensemble here
  only exposes a probability, so logit(raw_probability) is used as that
  score -- a standard, documented adaptation.
- Isotonic Regression: sklearn's non-parametric monotonic fit. More
  flexible than Platt scaling, but needs more calibration data to avoid
  overfitting -- exactly why "choose the best automatically" matters.
- Temperature Scaling: a single scalar T dividing the logit before
  resigmoiding (Guo et al. 2017's neural-net calibration technique) --
  the most constrained of the three, most robust with little data.

"Choose the best" means real out-of-sample selection, not picking whoever
fits its own training data closest: the calibration set is itself split
chronologically (fit half / eval half, no shuffling -- consistent with
every other chronological split in this codebase) and the method with the
lowest eval-half Brier score wins. Calibration Confidence is
1 - that winning eval Brier score (clipped to 0..1): the same "confidence
reflects real validation performance, never fabricated" idiom
ensemble.py's own Confidence field already uses.

If there isn't enough calibration data (MIN_CALIBRATION_SAMPLES), the
engine honestly returns the identity calibration (calibrated ==
raw, confidence 0.0) rather than fitting a method no data supports.
"""

import asyncio
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
from scipy.optimize import minimize_scalar
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

from app.core.cache import CacheService
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.events.bus import Event, EventBus
from app.prediction.ensemble import (
    DEFAULT_MAX_HOLDING_BARS,
    EnsemblePrediction,
    EnsemblePredictionEngine,
)

logger = get_logger(__name__)

EVENT_TYPE = "probability_calibration.result"

MIN_CALIBRATION_SAMPLES = 20
EVAL_FRACTION = 0.5  # chronological fit/eval split of the calibration set itself
LOGIT_EPS = 1e-6  # clip probabilities away from 0/1 before taking a logit
TEMPERATURE_BOUNDS = (0.05, 20.0)


def _logit(probability: float) -> float:
    clipped = min(max(probability, LOGIT_EPS), 1.0 - LOGIT_EPS)
    return math.log(clipped / (1.0 - clipped))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


# --- Calibrators ------------------------------------------------------------


@dataclass
class PlattCalibrator:
    coef: float
    intercept: float

    def predict(self, raw_probability: float) -> float:
        return _sigmoid(self.coef * _logit(raw_probability) + self.intercept)


@dataclass
class IsotonicCalibrator:
    model: IsotonicRegression

    def predict(self, raw_probability: float) -> float:
        return float(self.model.predict([raw_probability])[0])


@dataclass
class TemperatureCalibrator:
    temperature: float

    def predict(self, raw_probability: float) -> float:
        return _sigmoid(_logit(raw_probability) / self.temperature)


Calibrator = PlattCalibrator | IsotonicCalibrator | TemperatureCalibrator


def fit_platt(pairs: Sequence[tuple[float, int]]) -> PlattCalibrator:
    logits = np.array([[_logit(p)] for p, _ in pairs])
    outcomes = np.array([label for _, label in pairs])
    if len(set(outcomes.tolist())) < 2:
        # Can't fit a 2-class sigmoid on a single class -- identity fallback.
        return PlattCalibrator(coef=1.0, intercept=0.0)
    model = LogisticRegression()
    model.fit(logits, outcomes)
    return PlattCalibrator(coef=float(model.coef_[0][0]), intercept=float(model.intercept_[0]))


def fit_isotonic(pairs: Sequence[tuple[float, int]]) -> IsotonicCalibrator:
    raw = np.array([p for p, _ in pairs])
    outcomes = np.array([label for _, label in pairs])
    model = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    model.fit(raw, outcomes)
    return IsotonicCalibrator(model=model)


def fit_temperature(pairs: Sequence[tuple[float, int]]) -> TemperatureCalibrator:
    """Minimizes negative log-likelihood over a single scalar temperature
    -- a 1-parameter fit, the most constrained (and most data-efficient)
    of the three methods."""
    logits = np.array([_logit(p) for p, _ in pairs])
    outcomes = np.array([label for _, label in pairs], dtype=float)

    def negative_log_likelihood(temperature: float) -> float:
        temperature = max(temperature, TEMPERATURE_BOUNDS[0])
        probabilities = 1.0 / (1.0 + np.exp(-logits / temperature))
        probabilities = np.clip(probabilities, LOGIT_EPS, 1.0 - LOGIT_EPS)
        return -float(np.mean(
            outcomes * np.log(probabilities) + (1 - outcomes) * np.log(1 - probabilities)
        ))

    result = minimize_scalar(negative_log_likelihood, bounds=TEMPERATURE_BOUNDS, method="bounded")
    temperature = float(result.x) if result.success else 1.0
    return TemperatureCalibrator(temperature=temperature)


CALIBRATION_METHODS: dict[str, Callable[[Sequence[tuple[float, int]]], Calibrator]] = {
    "platt_scaling": fit_platt,
    "isotonic_regression": fit_isotonic,
    "temperature_scaling": fit_temperature,
}


def brier_score(calibrator: Calibrator, pairs: Sequence[tuple[float, int]]) -> float:
    """Mean squared error between calibrated probability and actual
    outcome -- the standard proper scoring rule for probability quality,
    lower is better."""
    if not pairs:
        return 1.0  # worst possible score -- honest default when there's nothing to evaluate
    errors = [(calibrator.predict(raw) - outcome) ** 2 for raw, outcome in pairs]
    return sum(errors) / len(errors)


@dataclass
class CalibrationFit:
    method: str
    calibrator: Calibrator
    eval_brier_score: float
    n_fit_samples: int
    n_eval_samples: int

    @property
    def calibration_confidence(self) -> float:
        return round(max(0.0, 1.0 - self.eval_brier_score), 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "eval_brier_score": self.eval_brier_score,
            "calibration_confidence": self.calibration_confidence,
            "n_fit_samples": self.n_fit_samples,
            "n_eval_samples": self.n_eval_samples,
        }


def choose_best_calibration(pairs: Sequence[tuple[float, int]]) -> CalibrationFit | None:
    """Fits every method on the chronological first half of `pairs`,
    scores each on the second half, and returns whichever generalizes
    best. Returns None (never a fabricated fit) when there isn't enough
    calibration data."""
    if len(pairs) < MIN_CALIBRATION_SAMPLES:
        return None

    split = max(1, min(len(pairs) - 1, round(len(pairs) * (1 - EVAL_FRACTION))))
    fit_pairs, eval_pairs = pairs[:split], pairs[split:]

    fits: list[CalibrationFit] = []
    for name, fit_fn in CALIBRATION_METHODS.items():
        try:
            calibrator = fit_fn(fit_pairs)
        except Exception:
            logger.warning("calibration method failed to fit: %s", name, exc_info=True)
            continue
        fits.append(CalibrationFit(
            method=name, calibrator=calibrator,
            eval_brier_score=round(brier_score(calibrator, eval_pairs), 6),
            n_fit_samples=len(fit_pairs), n_eval_samples=len(eval_pairs),
        ))
    if not fits:
        return None
    return min(fits, key=lambda f: f.eval_brier_score)


@dataclass
class CalibratedPrediction:
    symbol: str
    snapshot_id: str
    as_of: datetime
    raw_probability: float
    calibrated_probability: float
    calibration_confidence: float
    calibration_method: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of.isoformat(),
            "raw_probability": self.raw_probability,
            "calibrated_probability": self.calibrated_probability,
            "calibration_confidence": self.calibration_confidence,
            "calibration_method": self.calibration_method,
        }


def apply_calibration(
    raw_prediction: EnsemblePrediction, calibration: CalibrationFit | None
) -> CalibratedPrediction:
    """Pure computation from an already-chosen calibration method and an
    already-computed ensemble prediction -- no DB access, no refitting."""
    if calibration is None:
        # Never fabricate a correction with no data behind it: identity.
        return CalibratedPrediction(
            symbol=raw_prediction.symbol, snapshot_id=raw_prediction.snapshot_id,
            as_of=raw_prediction.as_of, raw_probability=raw_prediction.probability,
            calibrated_probability=raw_prediction.probability,
            calibration_confidence=0.0, calibration_method="none",
        )
    calibrated = calibration.calibrator.predict(raw_prediction.probability)
    return CalibratedPrediction(
        symbol=raw_prediction.symbol, snapshot_id=raw_prediction.snapshot_id,
        as_of=raw_prediction.as_of, raw_probability=raw_prediction.probability,
        calibrated_probability=round(calibrated, 4),
        calibration_confidence=calibration.calibration_confidence,
        calibration_method=calibration.method,
    )


class ProbabilityCalibrationEngine:
    name = "probability_calibration_engine"

    def __init__(
        self,
        session_factory: Any = None,
        cache: CacheService | None = None,
        settings: Settings | None = None,
        bus: EventBus | None = None,
        ensemble_engine: EnsemblePredictionEngine | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings or get_settings()
        self._bus = bus
        self._ensemble = ensemble_engine or EnsemblePredictionEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._calibrations: dict[tuple[str, str, str], CalibrationFit | None] = {}

    async def calibrate(
        self,
        symbol: str,
        timeframe: str = "D",
        direction: str = "long",
        lookback_bars: int = 500,
        max_holding_bars: int = DEFAULT_MAX_HOLDING_BARS,
    ) -> CalibrationFit | None:
        """Trains (or reuses a cached) ensemble for this key, then chooses
        the best calibration method against its holdout calibration set."""
        training = await self._ensemble.train(
            symbol, timeframe=timeframe, direction=direction,
            lookback_bars=lookback_bars, max_holding_bars=max_holding_bars,
        )
        # Three model fits (Platt/Isotonic/Temperature) are individually
        # cheap but still CPU-bound and blocking -- offloaded for the same
        # reason ensemble.py's own training is (see _fit_and_calibrate).
        result = await asyncio.to_thread(choose_best_calibration, training.calibration_pairs)
        self._calibrations[(symbol, timeframe, direction)] = result
        return result

    async def predict(
        self,
        symbol: str,
        timeframe: str = "D",
        direction: str = "long",
        lookback_bars: int = 500,
        max_holding_bars: int = DEFAULT_MAX_HOLDING_BARS,
    ) -> CalibratedPrediction:
        """Convenience: calibrate (if not already cached for this key),
        then get a fresh ensemble prediction and apply the calibration."""
        key = (symbol, timeframe, direction)
        if key in self._calibrations:
            calibration = self._calibrations[key]
        else:
            calibration = await self.calibrate(
                symbol, timeframe=timeframe, direction=direction,
                lookback_bars=lookback_bars, max_holding_bars=max_holding_bars,
            )
        raw_prediction = await self._ensemble.predict(
            symbol, timeframe=timeframe, direction=direction,
            lookback_bars=lookback_bars, max_holding_bars=max_holding_bars,
        )
        result = apply_calibration(raw_prediction, calibration)
        await self._persist(result)
        return result

    async def _persist(self, prediction: CalibratedPrediction) -> None:
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
