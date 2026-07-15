"""Tests for the Candidate Generation Engine (Volume 5, Prompt 5.2)."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.intelligence.base import IntelligenceResult
from app.prediction.candidates import (
    DEFAULT_LIFETIME_MINUTES,
    EVENT_LIFETIME_CAP_MINUTES,
    CandidateGenerationEngine,
    _streak_start,
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


class _StubDetector:
    """Returns a fixed batch of opportunities without touching a DB."""

    def __init__(self, n: int) -> None:
        self._opportunities = [
            make_opportunity([
                TriggerReason("trend_shift", "trend.states.transition", 0.5, 0.6),
            ])
            for _ in range(n)
        ]

    async def scan(self):
        return self._opportunities


class _ConcurrencyTrackingSnapshotEngine:
    """Records how many capture() calls were ever in flight at once --
    the actual claim under test (MAX_CONCURRENT_SNAPSHOT_CAPTURES), not
    just wall-clock time, which is too scale-dependent to assert
    precisely (see test_load_and_performance.py's own notes on this)."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.max_observed = 0

    async def market_wide_context(self):
        return {}

    async def capture(self, symbol: str, precomputed=None):
        self.in_flight += 1
        self.max_observed = max(self.max_observed, self.in_flight)
        try:
            await asyncio.sleep(0.01)  # force real overlap between concurrent calls
            return SimpleNamespace(snapshot_id=f"snap-{symbol}-{self.in_flight}")
        finally:
            self.in_flight -= 1


async def test_generate_bounds_concurrent_snapshot_captures() -> None:
    """The actual bug found live (2026-07-14): CandidateGenerationEngine.generate()
    used to fan out ALL candidates' snapshot captures at once via a bare
    asyncio.gather. Each capture() internally runs a 12-way intelligence
    fan-out of its own, so with MAX_CANDIDATES=20 that's up to 240
    simultaneous DB-touching calls regardless of connection pool size --
    live-measured to keep /prediction/candidates at ~13-14s even after
    fixing the sequential-loop bug and adding missing indexes. Fixed with
    a semaphore capping concurrent captures at
    MAX_CONCURRENT_SNAPSHOT_CAPTURES; this proves the cap actually holds
    under real overlapping load, not just that the code compiles."""
    from app.prediction.candidates import MAX_CONCURRENT_SNAPSHOT_CAPTURES

    detector = _StubDetector(n=10)  # more than MAX_CONCURRENT_SNAPSHOT_CAPTURES
    snapshots = _ConcurrencyTrackingSnapshotEngine()
    engine = CandidateGenerationEngine(
        session_factory=None, detector=detector, snapshot_engine=snapshots,
    )

    candidates = await engine.generate()

    assert len(candidates) == 10
    assert snapshots.max_observed <= MAX_CONCURRENT_SNAPSHOT_CAPTURES
    assert snapshots.max_observed > 1  # still genuinely concurrent, not accidentally serial


def _record(direction: str, minutes_ago: float) -> dict:
    return {
        "direction": direction,
        "as_of": (datetime(2026, 7, 15, 12, 0, tzinfo=UTC) - timedelta(minutes=minutes_ago)).isoformat(),
    }


def test_streak_start_walks_back_through_matching_direction() -> None:
    """Newest-first history, all "long", no gaps -- streak runs all the way
    back to the oldest record."""
    history = [_record("long", m) for m in (0, 5, 10, 15)]
    since = _streak_start(history, "long", timedelta(minutes=20))
    assert since == datetime.fromisoformat(history[-1]["as_of"])


def test_streak_start_stops_at_direction_change() -> None:
    history = [_record("long", 0), _record("long", 5), _record("short", 10), _record("long", 15)]
    since = _streak_start(history, "long", timedelta(minutes=20))
    # Streak is only the two most recent "long" records -- the older "long"
    # at minutes_ago=15 is a different, earlier episode separated by a
    # "short" in between, not part of the current run.
    assert since == datetime.fromisoformat(history[1]["as_of"])


def test_streak_start_stops_at_a_gap_wider_than_the_threshold() -> None:
    history = [_record("long", 0), _record("long", 5), _record("long", 200)]
    since = _streak_start(history, "long", timedelta(minutes=20))
    assert since == datetime.fromisoformat(history[1]["as_of"])


def test_streak_start_none_when_newest_record_already_mismatches() -> None:
    history = [_record("short", 0), _record("long", 5)]
    assert _streak_start(history, "long", timedelta(minutes=20)) is None


def test_streak_start_none_on_empty_history() -> None:
    assert _streak_start([], "long", timedelta(minutes=20)) is None


async def test_enrich_with_signal_since_without_a_session_factory_still_returns_dicts() -> None:
    """No DB -> signal_since can't be computed, but the base candidate
    dicts must still come back (graceful degradation, same convention as
    every other engine in this codebase)."""
    opportunity = make_opportunity([
        TriggerReason("liquidity_sweep_detected", "sweep", 0.6, 0.6),
    ])
    candidate = generate_candidate(opportunity, priority=1, feature_snapshot_id="snap-1")
    engine = CandidateGenerationEngine(session_factory=None)
    result = await engine.enrich_with_signal_since([candidate])
    assert len(result) == 1
    assert result[0]["instrument"] == "NIFTY"
    assert "signal_since" not in result[0]


@pytest.mark.db
async def test_enrich_with_signal_since_reads_the_real_streak_from_postgres(
    test_session_factory,
) -> None:
    """DB-backed: inserts a realistic persisted history directly (current
    "long" detection + two older "long" ones 5/10 min apart, then a "short"
    further back), and confirms enrich_with_signal_since's actual SQL query
    -- source filter, instrument IN (...), id-ordered -- finds the right
    streak start against real Postgres, not just the pure _streak_start
    unit tests above."""
    from app.database.tables import MarketEvent

    engine = CandidateGenerationEngine(session_factory=test_session_factory)
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

    async with test_session_factory() as session:
        # Inserted oldest-first, matching how production actually writes
        # this table: MarketEvent.id is assigned in insertion order, and
        # _persist_all() always appends the newest scan's row last as real
        # time advances -- so id order and as_of order agree in production.
        # enrich_with_signal_since()'s ORDER BY id DESC relies on that.
        for minutes_ago, direction in [(30, "short"), (10, "long"), (5, "long"), (0, "long")]:
            session.add(MarketEvent(
                event_type="trade_candidate.generated",
                source=engine.name,
                data={
                    "instrument": "NIFTY",
                    "direction": direction,
                    "as_of": (now - timedelta(minutes=minutes_ago)).isoformat(),
                },
            ))
        await session.commit()

    opportunity = make_opportunity([
        TriggerReason("liquidity_sweep_detected", "sweep", 0.6, 0.6),
    ])
    candidate = generate_candidate(opportunity, priority=1, feature_snapshot_id="snap-1")
    candidate.direction = "long"

    result = await engine.enrich_with_signal_since([candidate])

    assert len(result) == 1
    assert result[0]["signal_since"] == (now - timedelta(minutes=10)).isoformat()
    assert "IST" in result[0]["signal_since_ist"]


async def test_recent_returns_empty_list_without_a_session_factory() -> None:
    engine = CandidateGenerationEngine(session_factory=None)
    assert await engine.recent("NIFTY") == []
