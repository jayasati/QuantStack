"""Tests for Market Context Adjustment (Volume 5, Prompt 5.10)."""

from datetime import UTC, datetime

import pytest

from app.intelligence.base import IntelligenceResult
from app.prediction.calibration import CalibratedPrediction
from app.prediction.market_context import (
    ContextDimension,
    MarketContextAdjustmentEngine,
    apply_market_context,
    compute_market_quality,
    event_risk_quality,
    institutional_participation_quality,
    liquidity_quality,
    market_confidence_quality,
    regime_stability_quality,
    volatility_quality,
)

BASE_TS = datetime(2026, 7, 1, tzinfo=UTC)


def make_result(score: float, confidence: float = 0.8) -> IntelligenceResult:
    return IntelligenceResult(component="x", score=score, confidence=confidence)


def make_calibrated(probability: float = 0.8, confidence: float = 0.7) -> CalibratedPrediction:
    return CalibratedPrediction(
        symbol="NIFTY", snapshot_id="snap-1", as_of=BASE_TS,
        raw_probability=0.85, calibrated_probability=probability,
        calibration_confidence=confidence, calibration_method="isotonic_regression",
    )


# --- per-dimension quality extraction ---------------------------------------


def test_market_confidence_and_liquidity_quality_are_a_direct_passthrough() -> None:
    assert market_confidence_quality(make_result(80.0)).quality == pytest.approx(0.8)
    assert liquidity_quality(make_result(30.0)).quality == pytest.approx(0.3)


def test_event_risk_and_volatility_and_regime_stability_are_inverted() -> None:
    """These three scores are RISK/INSTABILITY/EXTREMITY magnitudes -- high
    score means degraded quality, so quality = 1 - score/100."""
    assert event_risk_quality(make_result(20.0)).quality == pytest.approx(0.8)
    assert volatility_quality(make_result(90.0)).quality == pytest.approx(0.1)
    assert regime_stability_quality(make_result(0.0)).quality == pytest.approx(1.0)


def test_institutional_participation_quality_is_distance_from_neutral() -> None:
    """Direction-agnostic: both strong accumulation and strong
    distribution read as high quality; a neutral, thin market reads low."""
    assert institutional_participation_quality(make_result(50.0)).quality == pytest.approx(0.0)
    assert institutional_participation_quality(make_result(90.0)).quality == pytest.approx(0.8)
    assert institutional_participation_quality(make_result(10.0)).quality == pytest.approx(0.8)


def test_quality_is_clamped_to_the_unit_interval() -> None:
    assert event_risk_quality(make_result(150.0)).quality == 0.0  # would be negative unclamped
    assert market_confidence_quality(make_result(-10.0)).quality == 0.0


# --- compute_market_quality ---------------------------------------------------


def test_compute_market_quality_confidence_weights_each_dimension() -> None:
    high_conf_good = ContextDimension(name="a", quality=1.0, confidence=1.0, raw_score=100.0)
    low_conf_bad = ContextDimension(name="b", quality=0.0, confidence=0.01, raw_score=0.0)
    weights = {"a": 1.0, "b": 1.0}
    quality, quality_confidence = compute_market_quality([high_conf_good, low_conf_bad], weights)
    assert quality is not None
    assert quality > 0.9  # dominated by the high-confidence dimension


def test_compute_market_quality_returns_none_with_zero_confidence_everywhere() -> None:
    dims = [
        ContextDimension(name="a", quality=0.9, confidence=0.0, raw_score=90.0),
        ContextDimension(name="b", quality=0.1, confidence=0.0, raw_score=10.0),
    ]
    quality, quality_confidence = compute_market_quality(dims, {"a": 1.0, "b": 1.0})
    assert quality is None
    assert quality_confidence == 0.0


# --- apply_market_context ----------------------------------------------------


def test_perfect_market_quality_leaves_the_probability_unchanged() -> None:
    dims = [ContextDimension(name=n, quality=1.0, confidence=1.0, raw_score=100.0)
            for n in ("market_confidence", "liquidity", "event_risk",
                      "regime_stability", "institutional_participation", "volatility")]
    calibrated = make_calibrated(probability=0.8, confidence=0.7)
    result = apply_market_context(calibrated, dims)
    assert result.market_quality_score == pytest.approx(1.0)
    assert result.adjusted_probability == pytest.approx(0.8)
    assert result.adjusted_confidence == pytest.approx(0.7)


def test_zero_market_quality_shrinks_probability_all_the_way_to_a_coin_flip() -> None:
    dims = [ContextDimension(name=n, quality=0.0, confidence=1.0, raw_score=0.0)
            for n in ("market_confidence", "liquidity", "event_risk",
                      "regime_stability", "institutional_participation", "volatility")]
    calibrated = make_calibrated(probability=0.9, confidence=0.7)
    result = apply_market_context(calibrated, dims)
    assert result.market_quality_score == pytest.approx(0.0)
    assert result.adjusted_probability == pytest.approx(0.5)
    assert result.adjusted_confidence == pytest.approx(0.0)


def test_partial_market_quality_shrinks_proportionally() -> None:
    dims = [ContextDimension(name=n, quality=0.5, confidence=1.0, raw_score=50.0)
            for n in ("market_confidence", "liquidity", "event_risk",
                      "regime_stability", "institutional_participation", "volatility")]
    calibrated = make_calibrated(probability=0.9, confidence=0.8)
    result = apply_market_context(calibrated, dims)
    assert result.adjusted_probability == pytest.approx(0.5 + (0.9 - 0.5) * 0.5)
    assert result.adjusted_confidence == pytest.approx(0.8 * 0.5)


def test_no_real_context_signal_is_an_honest_identity_no_op() -> None:
    dims = [ContextDimension(name=n, quality=0.9, confidence=0.0, raw_score=90.0)
            for n in ("market_confidence", "liquidity", "event_risk",
                      "regime_stability", "institutional_participation", "volatility")]
    calibrated = make_calibrated(probability=0.85, confidence=0.6)
    result = apply_market_context(calibrated, dims)
    assert result.market_quality_score is None
    assert result.adjusted_probability == calibrated.calibrated_probability
    assert result.adjusted_confidence == calibrated.calibration_confidence


def test_to_dict_carries_the_calibration_method_and_snapshot_id_through() -> None:
    dims = [ContextDimension(name="market_confidence", quality=0.8, confidence=0.9, raw_score=80.0)]
    calibrated = make_calibrated()
    result = apply_market_context(calibrated, dims)
    payload = result.to_dict()
    assert payload["snapshot_id"] == "snap-1"
    assert payload["calibration_method"] == "isotonic_regression"
    assert len(payload["dimensions"]) == 1


# --- engine, no DB: honest degradation ---------------------------------------


async def test_evaluate_without_a_db_never_fabricates_confidence() -> None:
    """No DB means no real calibration data (Prompt 5.7's own honest
    degrade), so the input probability is already the neutral 0.5
    default -- context adjustment is a no-op on that value regardless of
    market_quality, and adjusted_confidence stays exactly 0.0 since
    input_confidence itself is already 0.0."""
    engine = MarketContextAdjustmentEngine(session_factory=None)
    result = await engine.evaluate("NIFTY")
    assert result.input_probability == 0.5
    assert result.input_confidence == 0.0
    assert result.adjusted_probability == pytest.approx(0.5)
    assert result.adjusted_confidence == 0.0
    if result.market_quality_score is not None:
        assert 0.0 <= result.market_quality_score <= 1.0


async def test_recent_returns_empty_list_without_a_session_factory() -> None:
    engine = MarketContextAdjustmentEngine(session_factory=None)
    assert await engine.recent("NIFTY") == []
