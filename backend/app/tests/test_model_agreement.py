"""Tests for the Model Agreement Engine (Volume 5, Prompt 5.8)."""

from datetime import UTC, datetime
from statistics import pvariance

import pytest

from app.prediction.agreement import (
    AGREEMENT_EPSILON,
    HIGH_AGREEMENT_THRESHOLD,
    MEDIUM_AGREEMENT_THRESHOLD,
    ModelAgreementEngine,
    agreement_level_for,
    evaluate_agreement,
    model_direction,
)
from app.prediction.ensemble import EnsemblePrediction, ModelPrediction

BASE_TS = datetime(2026, 7, 1, tzinfo=UTC)


def make_model(
    name: str, probability: float, accuracy: float, weight: float = 0.3
) -> ModelPrediction:
    return ModelPrediction(
        name=name, probability=probability, weight=weight, holdout_accuracy=accuracy
    )


def make_prediction(
    model_predictions: list[ModelPrediction], probability: float, confidence: float = 0.7
) -> EnsemblePrediction:
    return EnsemblePrediction(
        symbol="X", snapshot_id="snap-1", as_of=BASE_TS,
        probability=probability, confidence=confidence, uncertainty=0.2, disagreement_score=0.1,
        model_version="test-v1", training_samples=100, model_predictions=model_predictions,
    )


# --- direction / bucketing --------------------------------------------------


def test_model_direction_thresholds_match_the_documented_epsilon() -> None:
    assert model_direction(0.5) == "neutral"
    assert model_direction(0.5 + AGREEMENT_EPSILON + 0.001) == "bullish"
    assert model_direction(0.5 - AGREEMENT_EPSILON - 0.001) == "bearish"
    assert model_direction(0.5 + AGREEMENT_EPSILON) == "neutral"  # exactly at the boundary


def test_agreement_level_for_matches_documented_thresholds() -> None:
    assert agreement_level_for(HIGH_AGREEMENT_THRESHOLD) == "high"
    assert agreement_level_for(HIGH_AGREEMENT_THRESHOLD - 0.01) == "medium"
    assert agreement_level_for(MEDIUM_AGREEMENT_THRESHOLD) == "medium"
    assert agreement_level_for(MEDIUM_AGREEMENT_THRESHOLD - 0.01) == "low"


# --- evaluate_agreement: the doc's literal example --------------------------


def test_lightgbm_bullish_catboost_bearish_is_low_agreement_and_does_not_proceed() -> None:
    """The doc's own example: two models flatly disagree on direction."""
    models = [
        make_model("lightgbm", probability=0.8, accuracy=0.7),
        make_model("catboost", probability=0.2, accuracy=0.7),
    ]
    prediction = make_prediction(models, probability=0.5)  # consensus lands neutral
    result = evaluate_agreement(prediction)
    assert result.agreement_level == "low"
    assert result.proceed is False


def test_unanimous_agreement_is_high_and_proceeds() -> None:
    models = [
        make_model("lightgbm", probability=0.8, accuracy=0.75),
        make_model("catboost", probability=0.82, accuracy=0.7),
        make_model("random_forest", probability=0.78, accuracy=0.65),
    ]
    prediction = make_prediction(models, probability=0.8)
    result = evaluate_agreement(prediction)
    assert result.agreement_pct == 1.0
    assert result.agreement_level == "high"
    assert result.proceed is True


def test_prediction_variance_matches_manual_population_variance() -> None:
    models = [
        make_model("a", probability=0.9, accuracy=0.7),
        make_model("b", probability=0.1, accuracy=0.7),
        make_model("c", probability=0.5, accuracy=0.7),
    ]
    prediction = make_prediction(models, probability=0.5)
    result = evaluate_agreement(prediction)
    assert result.prediction_variance == pytest.approx(pvariance([0.9, 0.1, 0.5]), abs=1e-5)


def test_confidence_spread_is_the_range_of_holdout_accuracy() -> None:
    models = [
        make_model("a", probability=0.8, accuracy=0.9),
        make_model("b", probability=0.8, accuracy=0.55),
    ]
    prediction = make_prediction(models, probability=0.8)
    result = evaluate_agreement(prediction)
    assert result.confidence_spread == pytest.approx(0.35)


def test_consensus_probability_and_model_reliability_reuse_the_ensemble_fields() -> None:
    """Honest reuse, not re-derivation: these two fields must exactly equal
    what Prompt 5.6's ensemble already computed."""
    models = [make_model("a", probability=0.7, accuracy=0.8)]
    prediction = make_prediction(models, probability=0.7, confidence=0.8)
    result = evaluate_agreement(prediction)
    assert result.consensus_probability == prediction.probability
    assert result.model_reliability == prediction.confidence


def test_single_model_agrees_with_itself_but_reliability_is_independent() -> None:
    models = [make_model("only_model", probability=0.9, accuracy=0.55)]
    prediction = make_prediction(models, probability=0.9, confidence=0.55)
    result = evaluate_agreement(prediction)
    assert result.agreement_pct == 1.0  # trivially agrees with itself
    assert result.prediction_variance == 0.0  # variance undefined with n=1 -- honestly zero
    assert result.confidence_spread == 0.0
    assert result.model_reliability == 0.55  # low reliability is a SEPARATE fact from agreement


def test_per_model_reliability_reports_direction_and_accuracy_per_model() -> None:
    models = [
        make_model("lightgbm", probability=0.85, accuracy=0.7, weight=0.4),
        make_model("catboost", probability=0.15, accuracy=0.6, weight=0.1),
    ]
    prediction = make_prediction(models, probability=0.6)
    result = evaluate_agreement(prediction)
    assert [m.name for m in result.per_model_reliability] == ["lightgbm", "catboost"]
    assert result.per_model_reliability[0].direction == "bullish"
    assert result.per_model_reliability[1].direction == "bearish"


def test_evaluate_agreement_with_no_models_never_fabricates_a_go_ahead() -> None:
    prediction = make_prediction([], probability=0.5, confidence=0.0)
    result = evaluate_agreement(prediction)
    assert result.prediction_variance == 0.0
    assert result.agreement_pct == 0.0
    assert result.confidence_spread == 0.0
    assert result.agreement_level == "low"
    assert result.proceed is False
    assert result.per_model_reliability == []


# --- engine, no DB: honest degradation ---------------------------------------


async def test_evaluate_without_a_db_does_not_proceed() -> None:
    engine = ModelAgreementEngine(session_factory=None)
    result = await engine.evaluate("NIFTY")
    assert result.proceed is False
    assert result.agreement_level == "low"


async def test_recent_returns_empty_list_without_a_session_factory() -> None:
    engine = ModelAgreementEngine(session_factory=None)
    assert await engine.recent("NIFTY") == []
