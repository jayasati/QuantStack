"""Tests for the Candidate Generation Engine (Volume 5, Prompt 5.2)."""

from datetime import UTC, datetime

from app.intelligence.base import IntelligenceResult
from app.prediction.candidates import (
    DEFAULT_LIFETIME_MINUTES,
    EVENT_LIFETIME_CAP_MINUTES,
    CandidateGenerationEngine,
    build_reason,
    build_supporting_features,
    current_market_regime,
    estimate_lifetime_minutes,
    generate_candidate,
    infer_direction,
)
from app.prediction.opportunity import OpportunityCandidate, TriggerReason


def result(component: str, *, confidence: float = 0.7,
           states: dict[str, float] | None = None, metrics: dict | None = None,
           ) -> IntelligenceResult:
    return IntelligenceResult(
        component=component, score=50.0, confidence=confidence,
        states=states or {}, metrics=metrics or {},
    )


def make_opportunity(
    triggers: list[TriggerReason],
    component_results: dict[str, IntelligenceResult | None] | None = None,
) -> OpportunityCandidate:
    return OpportunityCandidate(
        symbol="NIFTY",
        as_of=datetime(2026, 7, 9, 10, 0, tzinfo=UTC),
        triggers=triggers,
        priority_score=sum(t.weight for t in triggers),
        market_confidence=72.5,
        component_results=component_results or {},
    )


def test_infer_direction_long_when_signals_agree_positive() -> None:
    components = {
        "trend": result("trend", confidence=0.8, metrics={"trend_direction": 0.6}),
        "market_structure": result(
            "market_structure", confidence=0.6, metrics={"structural_bias": 0.4}
        ),
    }
    assert infer_direction(components) == "long"


def test_infer_direction_short_when_signals_agree_negative() -> None:
    components = {
        "institutional_flow": result(
            "institutional_flow", confidence=0.9, metrics={"net_flow_level": -0.5}
        ),
        "relative_strength": result(
            "relative_strength", confidence=0.5, metrics={"relative_trend": -0.3}
        ),
    }
    assert infer_direction(components) == "short"


def test_infer_direction_neutral_without_signals_or_when_they_cancel() -> None:
    assert infer_direction({}) == "neutral"
    tied = {
        "trend": result("trend", confidence=1.0, metrics={"trend_direction": 0.5}),
        "market_structure": result(
            "market_structure", confidence=1.0, metrics={"structural_bias": -0.5}
        ),
    }
    assert infer_direction(tied) == "neutral"


def test_current_market_regime_reads_dominant_states() -> None:
    components = {
        "trend": result("trend", states={"strong_bull_trend": 0.8, "range_bound": 0.2}),
        "market_structure": result(
            "market_structure", states={"markup": 0.9, "consolidation": 0.1}
        ),
        "volatility": result("volatility", states={"expansion": 0.6, "normal": 0.4}),
    }
    regime = current_market_regime(components)
    assert regime == {
        "trend": "strong_bull_trend",
        "market_structure": "markup",
        "volatility": "expansion",
    }


def test_current_market_regime_missing_component_is_none() -> None:
    assert current_market_regime({})["trend"] is None


def test_estimate_lifetime_uses_tightest_active_trigger() -> None:
    opportunity = make_opportunity([
        TriggerReason("institutional_accumulation", "flow", 0.6, 0.6),  # 3 days
        TriggerReason("liquidity_sweep_detected", "sweep", 0.6, 0.6),   # 2 hours -- tightest
    ])
    assert estimate_lifetime_minutes(opportunity) == 2 * 60


def test_estimate_lifetime_event_driven_uses_real_hours_until_event() -> None:
    opportunity = make_opportunity(
        [TriggerReason("event_driven_opportunity", "events.score", 60.0, 0.6)],
        component_results={"events": result("events", metrics={"hours_until_event": 6.0})},
    )
    assert estimate_lifetime_minutes(opportunity) == 6.0 * 60


def test_estimate_lifetime_event_driven_caps_and_falls_back_without_hours() -> None:
    far_future = make_opportunity(
        [TriggerReason("event_driven_opportunity", "events.score", 60.0, 0.6)],
        component_results={"events": result("events", metrics={"hours_until_event": 1000.0})},
    )
    assert estimate_lifetime_minutes(far_future) == EVENT_LIFETIME_CAP_MINUTES

    no_hours = make_opportunity(
        [TriggerReason("event_driven_opportunity", "events.score", 60.0, 0.6)],
        component_results={"events": result("events", metrics={})},
    )
    assert estimate_lifetime_minutes(no_hours) == DEFAULT_LIFETIME_MINUTES


def test_build_reason_and_supporting_features() -> None:
    opportunity = make_opportunity([
        TriggerReason(
            "liquidity_sweep_detected", "market_structure.states.liquidity_sweep", 0.65, 0.8
        ),
    ])
    reason = build_reason(opportunity, "long")
    assert "Long" in reason
    assert "liquidity sweep detected" in reason

    features = build_supporting_features(opportunity)
    assert len(features) == 1
    assert features[0].name == "market_structure.states.liquidity_sweep"
    assert features[0].value == 0.65


def test_generate_candidate_assembles_every_field() -> None:
    opportunity = make_opportunity(
        [TriggerReason("significant_breakout_probability", "ms_breakout_probability", 0.7, 0.8)],
        component_results={
            "trend": result("trend", confidence=0.8, metrics={"trend_direction": 0.5},
                             states={"strong_bull_trend": 1.0}),
        },
    )
    candidate = generate_candidate(opportunity, priority=3, feature_snapshot_id="snap-abc123")

    assert candidate.instrument == "NIFTY"
    assert candidate.direction == "long"
    assert candidate.priority == 3
    assert candidate.priority_score == opportunity.priority_score
    assert len(candidate.supporting_features) == 1
    assert candidate.feature_snapshot_id == "snap-abc123"
    assert candidate.estimated_lifetime_minutes == 4 * 60
    assert candidate.current_market_regime["trend"] == "strong_bull_trend"
    assert candidate.market_confidence == 72.5
    assert candidate.as_of == opportunity.as_of


def test_generate_candidate_threads_through_whatever_snapshot_id_is_given() -> None:
    """Pure function: it doesn't mint its own id, it uses exactly what the
    caller (CandidateGenerationEngine, which owns the async snapshot
    capture) passes in."""
    opportunity = make_opportunity([
        TriggerReason("high_volatility_expansion", "volatility.states.expansion", 0.6, 0.5),
    ])
    a = generate_candidate(opportunity, priority=1, feature_snapshot_id="id-a")
    b = generate_candidate(opportunity, priority=1, feature_snapshot_id="id-b")
    assert a.feature_snapshot_id == "id-a"
    assert b.feature_snapshot_id == "id-b"


async def test_generate_caps_at_top_20_and_runs_cleanly_without_a_db() -> None:
    """No session_factory -> underlying scan() finds nothing to trigger on;
    generate() must still complete without raising and respect the cap."""
    engine = CandidateGenerationEngine(session_factory=None)
    candidates = await engine.generate()
    assert isinstance(candidates, list)
    assert len(candidates) <= 20


async def test_recent_returns_empty_list_without_a_session_factory() -> None:
    engine = CandidateGenerationEngine(session_factory=None)
    assert await engine.recent("NIFTY") == []
