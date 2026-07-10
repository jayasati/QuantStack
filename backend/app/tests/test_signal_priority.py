"""Tests for the Signal Priority Engine (Volume 5, Prompt 5.13)."""

from datetime import UTC, datetime

import pytest

from app.intelligence.base import IntelligenceResult
from app.prediction.candidates import TradeCandidate
from app.prediction.conviction import ConvictionResult
from app.prediction.historical_similarity import HistoricalSimilarityResult
from app.prediction.priority import (
    LIFETIME_SATURATION_MINUTES,
    PRIORITY_WEIGHTS,
    RISK_VAR_CEILING_PCT,
    PriorityFactor,
    SignalPriorityEngine,
    build_priority_factors,
    compute_priority,
    lifetime_score,
    opportunity_quality_score,
    reward_score,
    risk_quality_score,
)

BASE_TS = datetime(2026, 7, 1, tzinfo=UTC)


def make_candidate(priority_score: float = 1.0, lifetime_minutes: float = 240.0) -> TradeCandidate:
    return TradeCandidate(
        instrument="NIFTY", direction="long", reason="x", priority=1,
        priority_score=priority_score, estimated_lifetime_minutes=lifetime_minutes, as_of=BASE_TS,
    )


def make_conviction(score: float = 70.0) -> ConvictionResult:
    return ConvictionResult(
        symbol="NIFTY", direction="long", snapshot_id="snap-1", as_of=BASE_TS,
        conviction_score=score, conviction_confidence=0.6, conviction_stability=0.8,
        conviction_trend="stable", conviction_grade="B", trend_slope=0.0, data_completeness=1.0,
    )


def make_intel(score: float, metrics: dict | None = None) -> IntelligenceResult:
    return IntelligenceResult(component="x", score=score, confidence=0.7, metrics=metrics or {})


def make_similarity(
    mean_similarity: float | None = 0.8, average_return: float | None = 0.02
) -> HistoricalSimilarityResult:
    return HistoricalSimilarityResult(
        symbol="NIFTY", direction="long", as_of=BASE_TS, n_analogs=20,
        historical_win_rate=0.6, average_return=average_return, worst_drawdown=-0.03,
        best_runup=0.05, probability_distribution=None,
        mean_similarity=mean_similarity, method_agreement=0.6,
    )


# --- transform helpers -------------------------------------------------------


def test_opportunity_quality_score_is_zero_with_no_trigger_evidence() -> None:
    assert opportunity_quality_score(0.0) == 0.0


def test_opportunity_quality_score_saturates_toward_100() -> None:
    assert opportunity_quality_score(100.0) == pytest.approx(100.0, abs=0.01)


def test_risk_quality_score_is_100_at_zero_risk() -> None:
    assert risk_quality_score(0.0) == 100.0


def test_risk_quality_score_is_0_at_the_ceiling() -> None:
    assert risk_quality_score(RISK_VAR_CEILING_PCT) == 0.0


def test_reward_score_is_neutral_at_zero_return() -> None:
    assert reward_score(0.0) == pytest.approx(50.0)


def test_reward_score_is_above_50_for_a_positive_return() -> None:
    assert reward_score(0.05) > 50.0


def test_lifetime_score_saturates_at_a_full_day() -> None:
    assert lifetime_score(LIFETIME_SATURATION_MINUTES) == pytest.approx(100.0)
    assert lifetime_score(LIFETIME_SATURATION_MINUTES * 2) == pytest.approx(100.0)  # clamped


def test_lifetime_score_is_zero_at_zero_minutes() -> None:
    assert lifetime_score(0.0) == 0.0


# --- build_priority_factors ---------------------------------------------


def test_build_priority_factors_includes_all_eight_when_everything_available() -> None:
    factors = build_priority_factors(
        candidate=make_candidate(), conviction=make_conviction(),
        liquidity_result=make_intel(80.0),
        relative_result=make_intel(60.0, metrics={"leadership_ranking": 75.0}),
        similarity=make_similarity(), risk_var_pct=1.0,
    )
    assert {f.name for f in factors} == set(PRIORITY_WEIGHTS)


def test_build_priority_factors_omits_missing_risk_leadership_and_analogs() -> None:
    factors = build_priority_factors(
        candidate=make_candidate(), conviction=make_conviction(),
        liquidity_result=make_intel(80.0),
        relative_result=make_intel(60.0, metrics={}),  # no leadership_ranking
        similarity=make_similarity(mean_similarity=None, average_return=None),
        risk_var_pct=None,
    )
    names = {f.name for f in factors}
    assert names == {
        "conviction", "opportunity_quality", "liquidity", "expected_opportunity_lifetime",
    }


def test_build_priority_factors_conviction_passes_through_directly() -> None:
    factors = build_priority_factors(
        candidate=make_candidate(), conviction=make_conviction(score=88.0),
        liquidity_result=make_intel(80.0), relative_result=make_intel(60.0),
        similarity=make_similarity(), risk_var_pct=1.0,
    )
    conviction_factor = next(f for f in factors if f.name == "conviction")
    assert conviction_factor.score == 88.0


# --- compute_priority ----------------------------------------------------


def test_compute_priority_matches_manual_weighted_average() -> None:
    factors = [
        PriorityFactor(name="conviction", score=80.0),
        PriorityFactor(name="liquidity", score=60.0),
    ]
    score, completeness = compute_priority(factors)
    assert score == pytest.approx((80.0 + 60.0) / 2)  # equal weights
    assert completeness == pytest.approx(2 / len(PRIORITY_WEIGHTS))


def test_compute_priority_on_empty_factors_is_an_honest_zero() -> None:
    score, completeness = compute_priority([])
    assert score == 0.0
    assert completeness == 0.0


# --- engine: ranking and Top-N behavior --------------------------------------


async def test_rank_drops_unqualified_candidates(monkeypatch) -> None:
    """Only qualified trades continue -- an unqualified candidate must
    never appear in the ranked output regardless of its scores."""
    from app.prediction.qualification import QualificationResult

    engine = SignalPriorityEngine(session_factory=None)

    candidates = [make_candidate(), make_candidate()]

    async def fake_generate():
        return candidates

    async def fake_qualify(symbol, direction="long"):
        return QualificationResult(
            symbol=symbol, direction=direction, as_of=BASE_TS,
            qualified=False, rejection_reasons=["Liquidity too low: 10/100 (floor 30)."],
        )

    monkeypatch.setattr(engine._candidates, "generate", fake_generate)
    monkeypatch.setattr(engine._qualification, "evaluate", fake_qualify)

    signals = await engine.rank()
    assert signals == []


async def test_rank_orders_by_priority_score_descending(monkeypatch) -> None:
    from app.prediction.qualification import QualificationResult

    engine = SignalPriorityEngine(session_factory=None)

    weak_candidate = TradeCandidate(
        instrument="WEAK", direction="long", reason="x", priority=1,
        priority_score=0.1, estimated_lifetime_minutes=10.0, as_of=BASE_TS,
    )
    strong_candidate = TradeCandidate(
        instrument="STRONG", direction="long", reason="x", priority=2,
        priority_score=2.0, estimated_lifetime_minutes=1440.0, as_of=BASE_TS,
    )

    async def fake_generate():
        return [weak_candidate, strong_candidate]

    async def fake_qualify(symbol, direction="long"):
        return QualificationResult(
            symbol=symbol, direction=direction, as_of=BASE_TS, qualified=True
        )

    async def fake_conviction_evaluate(symbol, direction="long"):
        score = 30.0 if symbol == "WEAK" else 90.0
        return make_conviction(score=score)

    async def fake_liquidity_assess(symbol):
        return make_intel(80.0)

    async def fake_relative_assess(symbol):
        return make_intel(60.0)

    async def fake_similarity_evaluate(symbol, direction="long"):
        return make_similarity()

    monkeypatch.setattr(engine._candidates, "generate", fake_generate)
    monkeypatch.setattr(engine._qualification, "evaluate", fake_qualify)
    monkeypatch.setattr(engine._conviction, "evaluate", fake_conviction_evaluate)
    monkeypatch.setattr(engine._liquidity, "assess", fake_liquidity_assess)
    monkeypatch.setattr(engine._relative_strength, "assess", fake_relative_assess)
    monkeypatch.setattr(engine._historical_similarity, "evaluate", fake_similarity_evaluate)

    signals = await engine.rank()
    assert [s.symbol for s in signals] == ["STRONG", "WEAK"]
    assert signals[0].rank == 1
    assert signals[1].rank == 2
    assert signals[0].priority_score > signals[1].priority_score


async def test_rank_respects_top_n(monkeypatch) -> None:
    from app.prediction.qualification import QualificationResult

    engine = SignalPriorityEngine(session_factory=None)
    candidates = [
        TradeCandidate(
            instrument=f"SYM{i}", direction="long", reason="x", priority=i,
            priority_score=float(i), estimated_lifetime_minutes=240.0, as_of=BASE_TS,
        )
        for i in range(5)
    ]

    async def fake_generate():
        return candidates

    async def fake_qualify(symbol, direction="long"):
        return QualificationResult(
            symbol=symbol, direction=direction, as_of=BASE_TS, qualified=True
        )

    async def fake_conviction_evaluate(symbol, direction="long"):
        return make_conviction()

    async def fake_liquidity_assess(symbol):
        return make_intel(80.0)

    async def fake_relative_assess(symbol):
        return make_intel(60.0)

    async def fake_similarity_evaluate(symbol, direction="long"):
        return make_similarity()

    monkeypatch.setattr(engine._candidates, "generate", fake_generate)
    monkeypatch.setattr(engine._qualification, "evaluate", fake_qualify)
    monkeypatch.setattr(engine._conviction, "evaluate", fake_conviction_evaluate)
    monkeypatch.setattr(engine._liquidity, "assess", fake_liquidity_assess)
    monkeypatch.setattr(engine._relative_strength, "assess", fake_relative_assess)
    monkeypatch.setattr(engine._historical_similarity, "evaluate", fake_similarity_evaluate)

    signals = await engine.rank(top_n=2)
    assert len(signals) == 2


# --- engine, no DB: honest degradation ---------------------------------------


async def test_rank_without_a_db_returns_nothing_to_rank() -> None:
    engine = SignalPriorityEngine(session_factory=None)
    signals = await engine.rank()
    assert signals == []


async def test_recent_returns_empty_list_without_a_session_factory() -> None:
    engine = SignalPriorityEngine(session_factory=None)
    assert await engine.recent("NIFTY") == []
