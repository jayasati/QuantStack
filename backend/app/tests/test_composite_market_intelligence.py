import pytest

from app.intelligence.base import IntelligenceResult
from app.intelligence.composite import ALL_COMPONENTS, CompositeMarketIntelligenceEngine, assess_composite


def fake(score: float, confidence: float = 0.7) -> IntelligenceResult:
    return IntelligenceResult(
        component="fake", score=score, confidence=confidence,
        states={"bullish": 0.6, "bearish": 0.4},
    )


class StubSubEngine:
    """Minimal stand-in for any of the 11 sub-engines -- assess() accepts
    either no args or symbol=/timeframe= kwargs, matching every real
    engine's own assess() signature variance."""

    def __init__(self, result: IntelligenceResult) -> None:
        self._result = result

    async def assess(self, *args, **kwargs) -> IntelligenceResult:
        return self._result


class StubRegimeDetector:
    def __init__(self) -> None:
        self.fed: list[tuple[str, str, str]] = []

    async def update_from_result(
        self, component: str, symbol: str, timeframe: str, result: IntelligenceResult
    ) -> None:
        self.fed.append((component, symbol, timeframe))


class StubExplainabilityStore:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, str, str]] = []

    async def record(
        self, component: str, symbol: str, timeframe: str, result: IntelligenceResult
    ) -> None:
        self.recorded.append((component, symbol, timeframe))


def make_engine(regime_detector: StubRegimeDetector, explainability: StubExplainabilityStore):
    stub = StubSubEngine(fake(65.0))
    return CompositeMarketIntelligenceEngine(
        trend_engine=stub, volatility_engine=stub, breadth_engine=stub,
        liquidity_engine=stub, macro_engine=stub, sector_engine=stub,
        institutional_flow_engine=stub, correlation_engine=stub,
        market_structure_engine=stub, event_engine=stub, options_engine=stub,
        momentum_engine=stub,
        regime_detector=regime_detector, explainability_store=explainability,
    )


async def test_assess_feeds_regime_detector_for_every_present_component() -> None:
    regime_detector = StubRegimeDetector()
    engine = make_engine(regime_detector, StubExplainabilityStore())
    await engine.assess(symbol="NIFTY")
    fed_components = {c for c, _, _ in regime_detector.fed}
    assert fed_components == set(ALL_COMPONENTS)
    assert all(symbol == "NIFTY" and timeframe == "D" for _, symbol, timeframe in regime_detector.fed)


async def test_assess_records_explainability_for_every_component_plus_itself() -> None:
    explainability = StubExplainabilityStore()
    engine = make_engine(StubRegimeDetector(), explainability)
    await engine.assess(symbol="NIFTY")
    recorded_components = {c for c, _, _ in explainability.recorded}
    assert recorded_components == set(ALL_COMPONENTS) | {"composite_market_intelligence"}


def bullish_calm_universe(**overrides) -> dict[str, IntelligenceResult]:
    results = {
        "trend": fake(80.0), "breadth": fake(80.0), "macro": fake(80.0),
        "sector": fake(80.0), "institutional_flow": fake(80.0), "market_structure": fake(80.0),
        "options": fake(80.0), "momentum": fake(80.0),
        "volatility": fake(20.0), "liquidity": fake(80.0),
        "correlation": fake(20.0), "event_risk": fake(10.0),
    }
    results.update(overrides)
    return results


def bearish_stressed_universe(**overrides) -> dict[str, IntelligenceResult]:
    results = {
        "trend": fake(20.0), "breadth": fake(20.0), "macro": fake(20.0),
        "sector": fake(20.0), "institutional_flow": fake(20.0), "market_structure": fake(20.0),
        "options": fake(20.0), "momentum": fake(20.0),
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
    assert result.metrics["components_present"] == 10
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
