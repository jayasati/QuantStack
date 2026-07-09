import pytest

from app.intelligence.base import IntelligenceResult
from app.intelligence.breadth import BreadthIntelligenceEngine
from app.intelligence.confidence import (
    MarketConfidenceEngine,
    _grade,
    assess_market_confidence,
)
from app.intelligence.correlation import CorrelationIntelligenceEngine
from app.intelligence.institutional_flow import InstitutionalFlowIntelligenceEngine
from app.intelligence.transitions import RegimeTransitionEngine


def all_high(**overrides) -> dict[str, float]:
    inputs = {
        "data_quality": 0.9, "feature_quality": 0.9, "regime_certainty": 0.9,
        "breadth": 0.9, "institutional_agreement": 0.9, "correlation_stability": 0.9,
    }
    inputs.update(overrides)
    return inputs


def all_low(**overrides) -> dict[str, float]:
    inputs = {
        "data_quality": 0.2, "feature_quality": 0.2, "regime_certainty": 0.2,
        "breadth": 0.2, "institutional_agreement": 0.2, "correlation_stability": 0.2,
    }
    inputs.update(overrides)
    return inputs


def test_grade_boundaries() -> None:
    assert _grade(80.0) == "A"
    assert _grade(79.9) == "B"
    assert _grade(65.0) == "B"
    assert _grade(50.0) == "C"
    assert _grade(35.0) == "D"
    assert _grade(34.9) == "F"


def test_all_high_inputs_score_high_grade_a() -> None:
    result = assess_market_confidence(all_high())
    assert result.score == pytest.approx(90.0)
    assert result.metrics["confidence_grade"] == "A"
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "high_confidence"


def test_all_low_inputs_score_low_grade_f() -> None:
    result = assess_market_confidence(all_low())
    assert result.score == pytest.approx(20.0)
    assert result.metrics["confidence_grade"] == "F"
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "low_confidence"


def test_partial_inputs_average_only_present_values() -> None:
    inputs = {"data_quality": 0.8, "feature_quality": None, "regime_certainty": 0.6,
              "breadth": None, "institutional_agreement": None, "correlation_stability": None}
    result = assess_market_confidence(inputs)
    assert result.score == pytest.approx(70.0)  # mean(0.8, 0.6) * 100
    assert result.metrics["data_completeness"] == pytest.approx(2 / 6, abs=1e-4)


def test_no_inputs_defaults_to_neutral_grade_c() -> None:
    result = assess_market_confidence({name: None for name in (
        "data_quality", "feature_quality", "regime_certainty",
        "breadth", "institutional_agreement", "correlation_stability",
    )})
    assert result.score == 50.0
    assert result.metrics["confidence_grade"] == "C"
    assert result.metrics["data_completeness"] == 0.0


def test_only_present_inputs_become_contributions() -> None:
    inputs = {"data_quality": 0.8, "feature_quality": None, "regime_certainty": 0.6,
              "breadth": None, "institutional_agreement": None, "correlation_stability": None}
    result = assess_market_confidence(inputs)
    assert {c.feature for c in result.contributions} == {"data_quality", "regime_certainty"}


def test_rising_history_reads_improving_trend() -> None:
    result = assess_market_confidence(all_high(), score_history=[60.0, 70.0, 80.0])
    assert result.metrics["confidence_trend"] == "improving"


def test_falling_history_reads_declining_trend() -> None:
    result = assess_market_confidence(all_low(), score_history=[80.0, 70.0, 60.0])
    assert result.metrics["confidence_trend"] == "declining"


def test_flat_history_reads_stable_trend() -> None:
    result = assess_market_confidence(
        {"data_quality": 0.5, "feature_quality": None, "regime_certainty": None,
         "breadth": None, "institutional_agreement": None, "correlation_stability": None},
        score_history=[50.0, 50.0, 50.0],
    )
    assert result.metrics["confidence_trend"] == "stable"


def test_states_sum_to_one() -> None:
    result = assess_market_confidence(all_high())
    assert abs(sum(result.states.values()) - 1.0) < 1e-9


async def test_data_quality_returns_none_without_a_session() -> None:
    engine = MarketConfidenceEngine()
    assert await engine._data_quality() is None


async def test_feature_quality_returns_none_without_a_session() -> None:
    engine = MarketConfidenceEngine()
    assert await engine._feature_quality() is None


async def test_score_history_returns_empty_without_a_session() -> None:
    engine = MarketConfidenceEngine()
    assert await engine._load_score_history("NIFTY") == []


async def test_store_score_is_a_noop_without_a_session() -> None:
    engine = MarketConfidenceEngine()
    await engine._store_score("NIFTY", 75.0)  # must not raise


async def test_engine_orchestrates_sub_components(monkeypatch) -> None:
    async def fake_regime_assess(self, *args, **kwargs):
        return IntelligenceResult(
            component="regime_transition", score=30.0, confidence=0.5, states={}
        )

    async def fake_breadth_assess(self, *args, **kwargs):
        return IntelligenceResult(component="breadth", score=60.0, confidence=0.7, states={})

    async def fake_flow_assess(self, *args, **kwargs):
        return IntelligenceResult(
            component="institutional_flow", score=55.0, confidence=0.65, states={}
        )

    async def fake_correlation_assess(self, *args, **kwargs):
        return IntelligenceResult(
            component="correlation", score=40.0, confidence=0.6, states={},
            metrics={"correlation_stability": 0.8},
        )

    monkeypatch.setattr(RegimeTransitionEngine, "assess", fake_regime_assess)
    monkeypatch.setattr(BreadthIntelligenceEngine, "assess", fake_breadth_assess)
    monkeypatch.setattr(InstitutionalFlowIntelligenceEngine, "assess", fake_flow_assess)
    monkeypatch.setattr(CorrelationIntelligenceEngine, "assess", fake_correlation_assess)

    engine = MarketConfidenceEngine()
    result = await engine.assess(symbol="NIFTY")

    assert result.metrics["regime_certainty"] == pytest.approx(0.7)  # 1 - 30/100
    assert result.metrics["breadth"] == pytest.approx(0.7)
    assert result.metrics["institutional_agreement"] == pytest.approx(0.65)
    assert result.metrics["correlation_stability"] == pytest.approx(0.8)
    assert result.metrics["data_quality"] is None  # no session
    assert result.metrics["feature_quality"] is None
    assert result.metrics["symbol"] == "NIFTY"
    # mean of the 4 available inputs * 100
    assert result.score == pytest.approx(100 * (0.7 + 0.7 + 0.65 + 0.8) / 4)
