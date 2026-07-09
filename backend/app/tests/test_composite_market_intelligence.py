import pytest

from app.intelligence.base import IntelligenceResult
from app.intelligence.composite import ALL_COMPONENTS, assess_composite


def fake(score: float, confidence: float = 0.7) -> IntelligenceResult:
    return IntelligenceResult(component="fake", score=score, confidence=confidence, states={})


def bullish_calm_universe(**overrides) -> dict[str, IntelligenceResult]:
    results = {
        "trend": fake(80.0), "breadth": fake(80.0), "macro": fake(80.0),
        "sector": fake(80.0), "institutional_flow": fake(80.0), "market_structure": fake(80.0),
        "volatility": fake(20.0), "liquidity": fake(80.0),
        "correlation": fake(20.0), "event_risk": fake(10.0),
    }
    results.update(overrides)
    return results


def bearish_stressed_universe(**overrides) -> dict[str, IntelligenceResult]:
    results = {
        "trend": fake(20.0), "breadth": fake(20.0), "macro": fake(20.0),
        "sector": fake(20.0), "institutional_flow": fake(20.0), "market_structure": fake(20.0),
        "volatility": fake(80.0), "liquidity": fake(20.0),
        "correlation": fake(80.0), "event_risk": fake(80.0),
    }
    results.update(overrides)
    return results


def test_bullish_calm_universe_scores_high_with_high_stability() -> None:
    result = assess_composite(bullish_calm_universe())
    assert result.score == pytest.approx(80.0)
    assert result.metrics["bullishness"] == pytest.approx(60.0)  # (80-50)/50*100
    assert result.metrics["bearishness"] == 0.0
    assert result.metrics["market_stability"] > 70
    assert result.metrics["expected_risk"] < 30
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "bullish"  # score 80 -> level_0_1 0.8, closer to the "bullish" anchor (0.75)


def test_bearish_stressed_universe_scores_low_with_low_stability() -> None:
    result = assess_composite(bearish_stressed_universe())
    assert result.score == pytest.approx(20.0)
    assert result.metrics["bearishness"] == pytest.approx(60.0)
    assert result.metrics["bullishness"] == 0.0
    assert result.metrics["market_stability"] < 30
    assert result.metrics["expected_risk"] > 70
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "bearish"  # score 20 -> level_0_1 0.2, closer to the "bearish" anchor (0.25)


def test_market_stability_ignores_event_risk() -> None:
    calm = assess_composite(bullish_calm_universe())
    with_event_risk = assess_composite(bullish_calm_universe(event_risk=fake(95.0)))
    assert with_event_risk.metrics["market_stability"] == calm.metrics["market_stability"]
    assert with_event_risk.metrics["expected_risk"] > calm.metrics["expected_risk"]


def test_expected_risk_ignores_liquidity() -> None:
    calm = assess_composite(bullish_calm_universe())
    illiquid = assess_composite(bullish_calm_universe(liquidity=fake(5.0)))
    assert illiquid.metrics["expected_risk"] == calm.metrics["expected_risk"]
    assert illiquid.metrics["market_stability"] < calm.metrics["market_stability"]


def test_mixed_direction_lowers_conviction_and_opportunity() -> None:
    mixed = bullish_calm_universe(
        trend=fake(80.0), breadth=fake(20.0), macro=fake(80.0),
        sector=fake(20.0), institutional_flow=fake(80.0), market_structure=fake(20.0),
    )
    result = assess_composite(mixed)
    confident_bull = assess_composite(bullish_calm_universe())
    assert result.metrics["expected_opportunity"] < confident_bull.metrics["expected_opportunity"]


def test_missing_components_reduce_data_completeness_and_confidence() -> None:
    partial = bullish_calm_universe()
    del partial["event_risk"]
    del partial["correlation"]
    full = assess_composite(bullish_calm_universe())
    result = assess_composite(partial)
    assert result.metrics["components_present"] == 8
    assert result.confidence < full.confidence


def test_component_scores_reports_none_for_missing() -> None:
    partial = bullish_calm_universe()
    del partial["event_risk"]
    result = assess_composite(partial)
    assert result.metrics["component_scores"]["event_risk"] is None
    assert result.metrics["component_scores"]["trend"] == 80.0


def test_contributions_only_include_present_components() -> None:
    partial = bullish_calm_universe()
    del partial["event_risk"]
    result = assess_composite(partial)
    assert {c.feature for c in result.contributions} == set(ALL_COMPONENTS) - {"event_risk"}


def test_states_sum_to_one() -> None:
    result = assess_composite(bullish_calm_universe())
    assert abs(sum(result.states.values()) - 1.0) < 1e-9


def test_no_components_defaults_to_neutral_with_zero_confidence() -> None:
    result = assess_composite({name: None for name in ALL_COMPONENTS})
    assert result.score == 50.0
    assert result.confidence == 0.0
    assert result.metrics["market_stability"] == 50.0
    assert result.metrics["expected_risk"] == 50.0
    assert result.metrics["expected_opportunity"] == 0.0
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "neutral"
