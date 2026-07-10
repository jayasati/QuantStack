"""Tests for the Trade Qualification Engine (Volume 5, Prompt 5.12)."""

from datetime import UTC, datetime

from app.intelligence.base import IntelligenceResult
from app.prediction.agreement import AgreementResult
from app.prediction.historical_similarity import HistoricalSimilarityResult
from app.prediction.qualification import (
    MAX_EVENT_RISK_SCORE,
    MAX_SPREAD_PCT,
    MIN_ANALOG_SIMILARITY,
    MIN_FEATURE_QUALITY,
    MIN_LIQUIDITY_SCORE,
    MIN_MARKET_CONFIDENCE_SCORE,
    TradeQualificationEngine,
    check_event_risk,
    check_feature_quality,
    check_historical_analog_reliability,
    check_liquidity,
    check_market_confidence,
    check_model_agreement,
    check_spread,
    qualify_trade,
)

BASE_TS = datetime(2026, 7, 1, tzinfo=UTC)


def make_intel(
    score: float, confidence: float = 0.7, metrics: dict | None = None
) -> IntelligenceResult:
    return IntelligenceResult(
        component="x", score=score, confidence=confidence, metrics=metrics or {}
    )


def make_agreement(agreement_pct: float = 0.9, proceed: bool = True) -> AgreementResult:
    return AgreementResult(
        symbol="NIFTY", snapshot_id="snap-1", as_of=BASE_TS,
        prediction_variance=0.01, agreement_pct=agreement_pct, confidence_spread=0.1,
        consensus_probability=0.8, model_reliability=0.75,
        agreement_level="high" if proceed else "low", proceed=proceed, per_model_reliability=[],
    )


def make_similarity(mean_similarity: float | None) -> HistoricalSimilarityResult:
    return HistoricalSimilarityResult(
        symbol="NIFTY", direction="long", as_of=BASE_TS,
        n_analogs=20 if mean_similarity is not None else 0,
        historical_win_rate=0.6, average_return=0.02, worst_drawdown=-0.03, best_runup=0.05,
        probability_distribution=None, mean_similarity=mean_similarity, method_agreement=0.6,
    )


PASSING = {
    "liquidity_result": make_intel(80.0),
    "spread_pct": 0.1,
    "event_result": make_intel(10.0),
    "agreement": make_agreement(),
    "market_confidence": make_intel(70.0, metrics={"feature_quality": 0.9}),
    "similarity": make_similarity(0.8),
}


# --- individual checks: passing cases ---------------------------------------


def test_check_liquidity_passes_above_the_floor() -> None:
    assert check_liquidity(make_intel(MIN_LIQUIDITY_SCORE + 1)) is None


def test_check_spread_passes_below_the_ceiling() -> None:
    assert check_spread(MAX_SPREAD_PCT - 0.01) is None


def test_check_event_risk_passes_below_ceiling_and_no_freeze() -> None:
    assert check_event_risk(make_intel(MAX_EVENT_RISK_SCORE - 1)) is None


def test_check_model_agreement_passes_when_models_agree() -> None:
    assert check_model_agreement(make_agreement(proceed=True)) is None


def test_check_feature_quality_passes_above_the_floor() -> None:
    result = make_intel(50.0, metrics={"feature_quality": MIN_FEATURE_QUALITY + 0.1})
    assert check_feature_quality(result) is None


def test_check_market_confidence_passes_above_the_floor() -> None:
    assert check_market_confidence(make_intel(MIN_MARKET_CONFIDENCE_SCORE + 1)) is None


def test_check_historical_analog_reliability_passes_above_the_floor() -> None:
    assert check_historical_analog_reliability(make_similarity(MIN_ANALOG_SIMILARITY + 0.1)) is None


# --- individual checks: rejecting cases --------------------------------------


def test_check_liquidity_rejects_below_the_floor() -> None:
    reason = check_liquidity(make_intel(MIN_LIQUIDITY_SCORE - 1))
    assert reason is not None
    assert "Liquidity too low" in reason


def test_check_spread_rejects_above_the_ceiling() -> None:
    reason = check_spread(MAX_SPREAD_PCT + 0.5)
    assert reason is not None
    assert "Spread too large" in reason


def test_check_spread_fails_closed_when_unavailable() -> None:
    """Unlike the scoring engines built earlier this volume, missing data
    here means reject, not honestly-neutral."""
    reason = check_spread(None)
    assert reason is not None
    assert "unavailable" in reason


def test_check_event_risk_rejects_above_the_ceiling() -> None:
    reason = check_event_risk(make_intel(MAX_EVENT_RISK_SCORE + 1))
    assert reason is not None
    assert "Event Risk too high" in reason


def test_check_event_risk_rejects_on_trading_freeze_regardless_of_score() -> None:
    result = make_intel(5.0, metrics={"trading_freeze_recommended": True})
    reason = check_event_risk(result)
    assert reason is not None
    assert "trading freeze recommended" in reason


def test_check_model_agreement_rejects_when_models_disagree() -> None:
    reason = check_model_agreement(make_agreement(agreement_pct=0.2, proceed=False))
    assert reason is not None
    assert "Model disagreement high" in reason


def test_check_feature_quality_rejects_below_the_floor() -> None:
    result = make_intel(50.0, metrics={"feature_quality": MIN_FEATURE_QUALITY - 0.1})
    reason = check_feature_quality(result)
    assert reason is not None
    assert "Feature Quality poor" in reason


def test_check_feature_quality_fails_closed_when_unavailable() -> None:
    reason = check_feature_quality(make_intel(50.0, metrics={}))
    assert reason is not None
    assert "unavailable" in reason


def test_check_market_confidence_rejects_below_the_floor() -> None:
    reason = check_market_confidence(make_intel(MIN_MARKET_CONFIDENCE_SCORE - 1))
    assert reason is not None
    assert "Market Confidence poor" in reason


def test_check_historical_analog_reliability_rejects_below_the_floor() -> None:
    reason = check_historical_analog_reliability(make_similarity(MIN_ANALOG_SIMILARITY - 0.1))
    assert reason is not None
    assert "Historical analog reliability poor" in reason


def test_check_historical_analog_reliability_fails_closed_with_no_analogs() -> None:
    reason = check_historical_analog_reliability(make_similarity(None))
    assert reason is not None
    assert "no analogs found" in reason


# --- qualify_trade: full gate -------------------------------------------


def test_qualify_trade_passes_when_every_check_passes() -> None:
    qualified, reasons = qualify_trade(**PASSING)
    assert qualified is True
    assert reasons == []


def test_qualify_trade_rejects_and_lists_every_failing_reason() -> None:
    failing = {**PASSING, "liquidity_result": make_intel(10.0), "spread_pct": None}
    qualified, reasons = qualify_trade(**failing)
    assert qualified is False
    assert len(reasons) == 2
    assert any("Liquidity too low" in r for r in reasons)
    assert any("Spread too large" in r for r in reasons)


def test_qualify_trade_rejects_on_a_single_failing_check() -> None:
    failing = {**PASSING, "agreement": make_agreement(agreement_pct=0.1, proceed=False)}
    qualified, reasons = qualify_trade(**failing)
    assert qualified is False
    assert len(reasons) == 1


# --- engine, no DB: fails closed honestly -----------------------------------


async def test_evaluate_without_a_db_fails_closed() -> None:
    """No DB means no real spread/feature-quality/agreement/analog data --
    the trade should NOT be qualified, with explicit reasons naming which
    checks couldn't be confirmed."""
    engine = TradeQualificationEngine(session_factory=None)
    result = await engine.evaluate("NIFTY")
    assert result.qualified is False
    assert len(result.rejection_reasons) > 0


async def test_evaluate_top_candidates_runs_cleanly_without_a_db() -> None:
    engine = TradeQualificationEngine(session_factory=None)
    results = await engine.evaluate_top_candidates()
    assert results == []  # no DB -> no candidates generated -> nothing to evaluate


async def test_recent_returns_empty_list_without_a_session_factory() -> None:
    engine = TradeQualificationEngine(session_factory=None)
    assert await engine.recent("NIFTY") == []
