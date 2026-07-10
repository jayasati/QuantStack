"""Tests for the Conviction Engine (Volume 5, Prompt 5.11)."""

from datetime import UTC, datetime

import pytest

from app.intelligence.base import IntelligenceResult
from app.prediction.agreement import AgreementResult
from app.prediction.calibration import CalibratedPrediction
from app.prediction.conviction import (
    EVIDENCE_WEIGHTS,
    HISTORY_TARGET,
    TREND_EPSILON,
    ConvictionEngine,
    EvidenceContribution,
    assess_conviction,
    build_evidence,
    compute_conviction,
    directional_score,
)
from app.prediction.historical_similarity import HistoricalSimilarityResult
from app.prediction.market_context import MarketContextAdjustment

BASE_TS = datetime(2026, 7, 1, tzinfo=UTC)


def make_calibrated(probability: float = 0.8, confidence: float = 0.7) -> CalibratedPrediction:
    return CalibratedPrediction(
        symbol="NIFTY", snapshot_id="snap-1", as_of=BASE_TS,
        raw_probability=probability, calibrated_probability=probability,
        calibration_confidence=confidence, calibration_method="isotonic_regression",
    )


def make_context(probability: float = 0.75, confidence: float = 0.6) -> MarketContextAdjustment:
    return MarketContextAdjustment(
        symbol="NIFTY", snapshot_id="snap-1", as_of=BASE_TS,
        input_probability=probability, adjusted_probability=probability,
        input_confidence=confidence, adjusted_confidence=confidence,
        market_quality_score=0.8, market_quality_confidence=0.7,
        calibration_method="isotonic_regression", dimensions=[],
    )


def make_similarity(
    win_rate: float | None = 0.7, mean_similarity: float | None = 0.85
) -> HistoricalSimilarityResult:
    has_analogs = win_rate is not None
    return HistoricalSimilarityResult(
        symbol="NIFTY", direction="long", as_of=BASE_TS, n_analogs=20 if has_analogs else 0,
        historical_win_rate=win_rate, average_return=0.02 if has_analogs else None,
        worst_drawdown=-0.03 if has_analogs else None,
        best_runup=0.05 if has_analogs else None,
        probability_distribution=None, mean_similarity=mean_similarity, method_agreement=0.6,
    )


def make_intel(score: float, confidence: float = 0.7) -> IntelligenceResult:
    return IntelligenceResult(component="x", score=score, confidence=confidence)


def make_agreement(agreement_pct: float = 0.9, model_reliability: float = 0.75) -> AgreementResult:
    return AgreementResult(
        symbol="NIFTY", snapshot_id="snap-1", as_of=BASE_TS,
        prediction_variance=0.01, agreement_pct=agreement_pct, confidence_spread=0.1,
        consensus_probability=0.8, model_reliability=model_reliability,
        agreement_level="high", proceed=True, per_model_reliability=[],
    )


def full_evidence(direction: str = "long") -> list[EvidenceContribution]:
    return build_evidence(
        calibrated=make_calibrated(), context=make_context(),
        similarity=make_similarity(), flow_result=make_intel(70.0),
        structure_result=make_intel(65.0), liquidity_result=make_intel(80.0),
        relative_result=make_intel(60.0), agreement=make_agreement(), direction=direction,
    )


# --- directional_score -------------------------------------------------------


def test_directional_score_is_unchanged_for_long() -> None:
    assert directional_score(70.0, "long") == 70.0


def test_directional_score_mirrors_for_short() -> None:
    assert directional_score(70.0, "short") == 30.0


def test_directional_score_treats_neutral_like_long() -> None:
    assert directional_score(70.0, "neutral") == 70.0


# --- build_evidence -----------------------------------------------------


def test_build_evidence_includes_all_eight_sources_when_analogs_exist() -> None:
    evidence = full_evidence()
    assert {e.name for e in evidence} == set(EVIDENCE_WEIGHTS)


def test_build_evidence_omits_historical_analog_with_zero_analogs() -> None:
    evidence = build_evidence(
        calibrated=make_calibrated(), context=make_context(),
        similarity=make_similarity(win_rate=None, mean_similarity=None),
        flow_result=make_intel(70.0), structure_result=make_intel(65.0),
        liquidity_result=make_intel(80.0), relative_result=make_intel(60.0),
        agreement=make_agreement(), direction="long",
    )
    assert "historical_analog" not in {e.name for e in evidence}
    assert len(evidence) == 7


def test_build_evidence_mirrors_directional_sources_for_short() -> None:
    long_evidence = {e.name: e.score for e in full_evidence("long")}
    short_evidence = {e.name: e.score for e in full_evidence("short")}
    for name in ("institutional_flow", "market_structure", "sector_strength"):
        assert short_evidence[name] == pytest.approx(100 - long_evidence[name])
    # Already direction-aware at the source -- untouched by mirroring.
    assert short_evidence["calibrated_probability"] == long_evidence["calibrated_probability"]
    assert short_evidence["liquidity"] == long_evidence["liquidity"]


# --- compute_conviction -------------------------------------------------


def test_compute_conviction_matches_the_docs_fixed_weights() -> None:
    evidence = full_evidence()
    score, mean_confidence, completeness = compute_conviction(evidence)
    manual = sum(EVIDENCE_WEIGHTS[e.name] * e.score for e in evidence) / sum(
        EVIDENCE_WEIGHTS[e.name] for e in evidence
    )
    assert score == pytest.approx(manual)
    assert completeness == pytest.approx(1.0)  # all 8 present


def test_compute_conviction_renormalizes_when_a_source_is_missing() -> None:
    evidence = [e for e in full_evidence() if e.name != "historical_analog"]
    score, _, completeness = compute_conviction(evidence)
    manual = sum(EVIDENCE_WEIGHTS[e.name] * e.score for e in evidence) / sum(
        EVIDENCE_WEIGHTS[e.name] for e in evidence
    )
    assert score == pytest.approx(manual)
    assert completeness == pytest.approx(7 / 8)


def test_compute_conviction_on_empty_evidence_is_an_honest_neutral() -> None:
    score, confidence, completeness = compute_conviction([])
    assert score == 50.0
    assert confidence == 0.0
    assert completeness == 0.0


# --- assess_conviction: trend / stability / grade -------------------------


def test_assess_conviction_grade_matches_score_thresholds() -> None:
    evidence = [EvidenceContribution(name="calibrated_probability", score=90.0, confidence=1.0)]
    result = assess_conviction("NIFTY", "long", "snap-1", BASE_TS, evidence)
    assert result.conviction_grade == "A"


def test_assess_conviction_trend_improving_when_score_rises_past_epsilon() -> None:
    evidence = [EvidenceContribution(name="calibrated_probability", score=80.0, confidence=1.0)]
    history = [60.0, 65.0, 70.0]  # rising well past TREND_EPSILON per step
    result = assess_conviction("NIFTY", "long", "snap-1", BASE_TS, evidence, history)
    assert result.trend_slope > TREND_EPSILON
    assert result.conviction_trend == "improving"


def test_assess_conviction_trend_declining_when_score_falls_past_epsilon() -> None:
    evidence = [EvidenceContribution(name="calibrated_probability", score=40.0, confidence=1.0)]
    history = [80.0, 70.0, 60.0]
    result = assess_conviction("NIFTY", "long", "snap-1", BASE_TS, evidence, history)
    assert result.conviction_trend == "declining"


def test_assess_conviction_trend_stable_with_no_history() -> None:
    evidence = [EvidenceContribution(name="calibrated_probability", score=70.0, confidence=1.0)]
    result = assess_conviction("NIFTY", "long", "snap-1", BASE_TS, evidence)
    assert result.trend_slope == 0.0
    assert result.conviction_trend == "stable"
    assert result.conviction_stability == 1.0  # a single point has zero variance, honestly


def test_assess_conviction_stability_is_low_for_an_oscillating_history() -> None:
    evidence = [EvidenceContribution(name="calibrated_probability", score=50.0, confidence=1.0)]
    oscillating = [20.0, 80.0, 20.0, 80.0]
    result = assess_conviction("NIFTY", "long", "snap-1", BASE_TS, evidence, oscillating)
    assert result.conviction_stability < 0.5


def test_assess_conviction_stability_is_high_for_a_smooth_climb() -> None:
    evidence = [EvidenceContribution(name="calibrated_probability", score=64.0, confidence=1.0)]
    smooth = [58.0, 60.0, 62.0]
    result = assess_conviction("NIFTY", "long", "snap-1", BASE_TS, evidence, smooth)
    assert result.conviction_stability > 0.9  # low variance, regardless of trend direction


def test_assess_conviction_confidence_improves_with_more_history() -> None:
    evidence = full_evidence()
    no_history = assess_conviction("NIFTY", "long", "snap-1", BASE_TS, evidence, [])
    full_history = assess_conviction(
        "NIFTY", "long", "snap-1", BASE_TS, evidence, [70.0] * HISTORY_TARGET
    )
    assert full_history.conviction_confidence > no_history.conviction_confidence


def test_to_dict_includes_all_evidence_entries() -> None:
    evidence = full_evidence()
    result = assess_conviction("NIFTY", "long", "snap-1", BASE_TS, evidence)
    payload = result.to_dict()
    assert len(payload["evidence"]) == len(evidence)
    assert payload["symbol"] == "NIFTY"
    assert payload["direction"] == "long"


# --- engine, no DB: honest degradation ---------------------------------------


async def test_evaluate_without_a_db_runs_cleanly() -> None:
    engine = ConvictionEngine(session_factory=None)
    result = await engine.evaluate("NIFTY")
    assert 0.0 <= result.conviction_score <= 100.0
    assert result.conviction_grade in ("A", "B", "C", "D", "F")


async def test_evaluate_top_candidates_runs_cleanly_without_a_db() -> None:
    engine = ConvictionEngine(session_factory=None)
    results = await engine.evaluate_top_candidates()
    assert results == []  # no DB -> no candidates generated -> nothing to evaluate


async def test_recent_returns_empty_list_without_a_session_factory() -> None:
    engine = ConvictionEngine(session_factory=None)
    assert await engine.recent("NIFTY") == []
