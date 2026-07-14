"""Tests for the Opportunity Detection Engine (Volume 5, Prompt 5.1)."""

from app.intelligence.base import IntelligenceResult
from app.prediction.opportunity import (
    BREAKOUT_PROBABILITY_THRESHOLD,
    EVENT_RISK_THRESHOLD,
    INSTITUTIONAL_FLOW_THRESHOLD,
    LEADERSHIP_RANKING_THRESHOLD,
    LIQUIDITY_SWEEP_THRESHOLD,
    VOLATILITY_EXPANSION_THRESHOLD,
    OpportunityDetectionEngine,
    evaluate_triggers,
    priority_score,
)


def result(component: str, *, score: float = 50.0, confidence: float = 0.7,
           states: dict[str, float] | None = None, metrics: dict | None = None,
           ) -> IntelligenceResult:
    return IntelligenceResult(
        component=component, score=score, confidence=confidence,
        states=states or {}, metrics=metrics or {},
    )


def empty_universe(**overrides: IntelligenceResult | None) -> dict[str, IntelligenceResult | None]:
    base: dict[str, IntelligenceResult | None] = {
        "market_structure": result("market_structure"),
        "trend_transition": result("regime_transition", metrics={"alert": False}),
        "market_structure_transition": result("regime_transition", metrics={"alert": False}),
        "institutional_flow": result("institutional_flow"),
        "relative_strength": result("relative_strength"),
        "volatility": result("volatility"),
        "events": result("events", score=0.0),
    }
    base.update(overrides)
    return base


def test_no_triggers_on_a_quiet_universe() -> None:
    assert evaluate_triggers(empty_universe()) == []


def test_breakout_probability_fires_above_threshold_only() -> None:
    below = empty_universe(market_structure=result(
        "market_structure", metrics={"breakout_probability": BREAKOUT_PROBABILITY_THRESHOLD - 0.01}
    ))
    above = empty_universe(market_structure=result(
        "market_structure", metrics={"breakout_probability": BREAKOUT_PROBABILITY_THRESHOLD + 0.01}
    ))
    assert evaluate_triggers(below) == []
    triggers = evaluate_triggers(above)
    assert len(triggers) == 1
    assert triggers[0].condition == "significant_breakout_probability"


def test_liquidity_sweep_fires_above_threshold() -> None:
    triggered = empty_universe(market_structure=result(
        "market_structure", states={"liquidity_sweep": LIQUIDITY_SWEEP_THRESHOLD + 0.1}
    ))
    triggers = evaluate_triggers(triggered)
    assert [t.condition for t in triggers] == ["liquidity_sweep_detected"]


def test_structural_trend_change_and_regime_transition_are_independent() -> None:
    only_trend = empty_universe(
        trend_transition=result(
            "regime_transition", metrics={"alert": True, "transition_probability": 0.7}
        )
    )
    triggers = evaluate_triggers(only_trend)
    assert [t.condition for t in triggers] == ["structural_trend_change"]

    only_structure = empty_universe(
        market_structure_transition=result(
            "regime_transition", metrics={"alert": True, "transition_probability": 0.7}
        )
    )
    triggers = evaluate_triggers(only_structure)
    assert [t.condition for t in triggers] == ["regime_transition"]


def test_institutional_accumulation_and_distribution_both_checked() -> None:
    accumulation = empty_universe(institutional_flow=result(
        "institutional_flow",
        states={"institutional_accumulation": INSTITUTIONAL_FLOW_THRESHOLD + 0.1},
    ))
    assert [t.condition for t in evaluate_triggers(accumulation)] == ["institutional_accumulation"]

    distribution = empty_universe(institutional_flow=result(
        "institutional_flow",
        states={"institutional_distribution": INSTITUTIONAL_FLOW_THRESHOLD + 0.1},
    ))
    assert [t.condition for t in evaluate_triggers(distribution)] == ["institutional_distribution"]


def test_exceptional_relative_strength_uses_leadership_ranking() -> None:
    below = empty_universe(relative_strength=result(
        "relative_strength", metrics={"leadership_ranking": LEADERSHIP_RANKING_THRESHOLD - 1}
    ))
    above = empty_universe(relative_strength=result(
        "relative_strength", metrics={"leadership_ranking": LEADERSHIP_RANKING_THRESHOLD + 1}
    ))
    assert evaluate_triggers(below) == []
    assert [t.condition for t in evaluate_triggers(above)] == ["exceptional_relative_strength"]


def test_high_volatility_expansion_uses_expansion_state() -> None:
    triggered = empty_universe(volatility=result(
        "volatility", states={"expansion": VOLATILITY_EXPANSION_THRESHOLD + 0.1}
    ))
    assert [t.condition for t in evaluate_triggers(triggered)] == ["high_volatility_expansion"]


def test_event_driven_opportunity_uses_score_not_states() -> None:
    triggered = empty_universe(events=result("events", score=EVENT_RISK_THRESHOLD + 1))
    assert [t.condition for t in evaluate_triggers(triggered)] == ["event_driven_opportunity"]


def test_missing_components_never_crash_evaluation() -> None:
    """A component that failed to compute (None, matching report.py's own
    safe() swallowing) must not raise — same resilience contract as every
    other Volume 4 component."""
    sparse: dict[str, IntelligenceResult | None] = {"market_structure": None}
    assert evaluate_triggers(sparse) == []


def test_priority_score_is_confidence_weighted_not_a_raw_count() -> None:
    universe = empty_universe(
        market_structure=result(
            "market_structure",
            confidence=0.95,
            metrics={"breakout_probability": BREAKOUT_PROBABILITY_THRESHOLD + 0.2},
            states={"liquidity_sweep": LIQUIDITY_SWEEP_THRESHOLD + 0.2},
        ),
    )
    single_high_confidence_triggers = evaluate_triggers(universe)
    assert len(single_high_confidence_triggers) == 2  # breakout + sweep, same component

    weak_universe = empty_universe(
        institutional_flow=result(
            "institutional_flow", confidence=0.05,
            states={"institutional_accumulation": INSTITUTIONAL_FLOW_THRESHOLD + 0.01},
        ),
    )
    weak_triggers = evaluate_triggers(weak_universe)
    assert priority_score(single_high_confidence_triggers) > priority_score(weak_triggers)


async def test_scan_runs_cleanly_against_a_db_less_container() -> None:
    """No session_factory -> every intelligence engine reads no features and
    degrades gracefully; scan() must complete without raising."""
    engine = OpportunityDetectionEngine(session_factory=None)
    candidates = await engine.scan()
    assert isinstance(candidates, list)


async def test_recent_returns_empty_list_without_a_session_factory() -> None:
    engine = OpportunityDetectionEngine(session_factory=None)
    assert await engine.recent("NIFTY") == []


class StubExplainability:
    def __init__(self, record):
        self._record = record

    async def latest(self, component, symbol, timeframe):
        assert component == "composite_market_intelligence"
        return self._record


async def test_composite_context_reads_the_persisted_composite_score() -> None:
    """Reads via ExplainabilityStore (populated by main.py's scheduled
    composite_intelligence_sweep) rather than calling
    CompositeMarketIntelligenceEngine directly -- the whole point of this
    fix is NOT recomputing a 6th/7th time per detect() call."""
    engine = OpportunityDetectionEngine(
        session_factory=lambda: None,
        explainability_store=StubExplainability({"score": 72.5, "confidence": 0.8}),
    )
    score, confidence = await engine._composite_context("NIFTY")
    assert score == 72.5
    assert confidence == 0.8


async def test_composite_context_is_honestly_none_without_a_persisted_record() -> None:
    engine = OpportunityDetectionEngine(
        session_factory=lambda: None,
        explainability_store=StubExplainability(None),
    )
    score, confidence = await engine._composite_context("NIFTY")
    assert score is None
    assert confidence is None
