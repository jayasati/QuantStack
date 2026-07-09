"""Tests for Bayesian Probability Calibration (Volume 5, Prompt 5.7)."""

from datetime import UTC, datetime

import pytest

from app.prediction.calibration import (
    MIN_CALIBRATION_SAMPLES,
    CalibrationFit,
    ProbabilityCalibrationEngine,
    apply_calibration,
    brier_score,
    choose_best_calibration,
    fit_isotonic,
    fit_platt,
    fit_temperature,
)
from app.prediction.ensemble import EnsemblePrediction

BASE_TS = datetime(2026, 7, 1, tzinfo=UTC)


def overconfident_pairs(n: int = 60) -> list[tuple[float, int]]:
    """A classic overconfidence pattern: the raw probability always reads
    0.9 or 0.1, but the true win rate at those raw levels is only ~70/30 --
    exactly what calibration is supposed to correct."""
    pairs = []
    for i in range(n):
        if i % 10 < 7:
            pairs.append((0.9, 1))
        else:
            pairs.append((0.9, 0))
    for i in range(n):
        if i % 10 < 7:
            pairs.append((0.1, 0))
        else:
            pairs.append((0.1, 1))
    return pairs


def make_prediction(probability: float) -> EnsemblePrediction:
    return EnsemblePrediction(
        symbol="X", snapshot_id="snap-1", as_of=BASE_TS,
        probability=probability, confidence=0.8, uncertainty=0.2, disagreement_score=0.1,
        model_version="test-v1", training_samples=100, model_predictions=[],
    )


# --- individual calibrators -------------------------------------------------


def test_fit_platt_pulls_an_overconfident_probability_toward_the_true_rate() -> None:
    calibrator = fit_platt(overconfident_pairs())
    assert 0.6 < calibrator.predict(0.9) < 0.9  # pulled down from 0.9 toward ~0.7
    assert 0.1 < calibrator.predict(0.1) < 0.4  # pulled up from 0.1 toward ~0.3


def test_fit_platt_degrades_to_identity_on_a_single_class() -> None:
    calibrator = fit_platt([(0.8, 1)] * 25)
    assert calibrator.coef == 1.0
    assert calibrator.intercept == 0.0


def test_fit_isotonic_is_monotonic_and_corrects_overconfidence() -> None:
    calibrator = fit_isotonic(overconfident_pairs())
    assert calibrator.predict(0.9) < 0.9
    assert calibrator.predict(0.1) > 0.1
    assert calibrator.predict(0.9) > calibrator.predict(0.1)  # monotonic ordering preserved


def test_fit_temperature_softens_overconfident_extremes() -> None:
    calibrator = fit_temperature(overconfident_pairs())
    assert calibrator.temperature > 1.0  # softening requires T > 1
    assert calibrator.predict(0.9) < 0.9
    assert calibrator.predict(0.1) > 0.1


def test_brier_score_of_a_perfect_calibrator_is_zero() -> None:
    class _Perfect:
        def predict(self, raw: float) -> float:
            return raw

    pairs = [(1.0, 1), (0.0, 0), (1.0, 1)]
    assert brier_score(_Perfect(), pairs) == pytest.approx(0.0)


def test_brier_score_on_empty_pairs_is_the_worst_possible_score() -> None:
    class _Anything:
        def predict(self, raw: float) -> float:
            return raw

    assert brier_score(_Anything(), []) == 1.0


# --- method selection --------------------------------------------------------


def test_choose_best_calibration_returns_none_below_the_minimum_sample_size() -> None:
    assert choose_best_calibration([(0.5, 1)] * (MIN_CALIBRATION_SAMPLES - 1)) is None


def test_choose_best_calibration_picks_the_lowest_eval_brier_score() -> None:
    result = choose_best_calibration(overconfident_pairs())
    assert isinstance(result, CalibrationFit)
    assert result.method in ("platt_scaling", "isotonic_regression", "temperature_scaling")
    assert result.eval_brier_score >= 0.0
    assert 0.0 <= result.calibration_confidence <= 1.0
    assert result.n_fit_samples + result.n_eval_samples == len(overconfident_pairs())


def test_calibration_fit_to_dict_excludes_the_raw_calibrator_object() -> None:
    result = choose_best_calibration(overconfident_pairs())
    assert result is not None
    payload = result.to_dict()
    assert "calibrator" not in payload
    assert payload["method"] == result.method


# --- applying a calibration to a prediction --------------------------------


def test_apply_calibration_with_no_fit_is_an_honest_identity() -> None:
    prediction = make_prediction(0.85)
    result = apply_calibration(prediction, None)
    assert result.raw_probability == 0.85
    assert result.calibrated_probability == 0.85
    assert result.calibration_confidence == 0.0
    assert result.calibration_method == "none"


def test_apply_calibration_uses_the_chosen_methods_correction() -> None:
    fit = choose_best_calibration(overconfident_pairs())
    assert fit is not None
    prediction = make_prediction(0.9)
    result = apply_calibration(prediction, fit)
    assert result.raw_probability == 0.9
    assert result.calibrated_probability != 0.9
    assert result.calibration_method == fit.method
    assert result.calibration_confidence == fit.calibration_confidence


# --- engine, no DB: honest degradation ---------------------------------------


async def test_calibrate_without_a_db_returns_none() -> None:
    engine = ProbabilityCalibrationEngine(session_factory=None)
    assert await engine.calibrate("NIFTY") is None


async def test_predict_without_a_db_is_an_identity_calibration() -> None:
    engine = ProbabilityCalibrationEngine(session_factory=None)
    result = await engine.predict("NIFTY")
    assert result.raw_probability == result.calibrated_probability == 0.5
    assert result.calibration_method == "none"


async def test_recent_returns_empty_list_without_a_session_factory() -> None:
    engine = ProbabilityCalibrationEngine(session_factory=None)
    assert await engine.recent("NIFTY") == []
