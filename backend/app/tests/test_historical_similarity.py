"""Tests for Historical Similarity Prediction (Volume 5, Prompt 5.9)."""

from datetime import UTC, datetime

import pytest

from app.intelligence.base import IntelligenceResult
from app.prediction.historical_similarity import (
    HistoricalSimilarityEngine,
    evaluate_similarity,
    probability_distribution,
)

BASE_TS = datetime(2026, 7, 1, tzinfo=UTC)


def make_analog(subsequent_return: float, max_drawdown: float, max_runup: float) -> dict:
    return {
        "date": "2026-01-01", "similarity": 0.9,
        "subsequent_return": subsequent_return,
        "subsequent_volatility": 0.01,
        "max_drawdown": max_drawdown, "max_runup": max_runup,
    }


def make_analog_result(
    analogs: list[dict], mean_similarity: float = 0.8, method_agreement: float = 0.7
) -> IntelligenceResult:
    return IntelligenceResult(
        component="historical_analogs", score=60.0, confidence=0.6, as_of=BASE_TS,
        metrics={
            "analogs": analogs,
            "mean_similarity": mean_similarity,
            "method_agreement": method_agreement,
        },
    )


# --- probability_distribution ------------------------------------------------


def test_probability_distribution_matches_manual_percentiles() -> None:
    returns = [0.01, 0.02, 0.03, 0.04, 0.05, -0.01, -0.02, 0.0, 0.015, 0.025]
    dist = probability_distribution(returns)
    assert dist is not None
    assert dist.median == pytest.approx(sorted(returns)[len(returns) // 2], abs=0.01)
    assert dist.p10 <= dist.p25 <= dist.median <= dist.p75 <= dist.p90


def test_probability_distribution_on_empty_returns_is_none() -> None:
    assert probability_distribution([]) is None


# --- evaluate_similarity: direction handling ---------------------------------


def test_long_candidate_uses_raw_analog_stats() -> None:
    analogs = [
        make_analog(subsequent_return=0.05, max_drawdown=-0.02, max_runup=0.06),
        make_analog(subsequent_return=-0.03, max_drawdown=-0.05, max_runup=0.01),
    ]
    result_in = make_analog_result(analogs)
    result = evaluate_similarity("NIFTY", "long", BASE_TS, result_in)

    assert result.n_analogs == 2
    assert result.historical_win_rate == pytest.approx(0.5)
    assert result.average_return == pytest.approx((0.05 - 0.03) / 2)
    assert result.worst_drawdown == pytest.approx(-0.05)  # the most negative, real drawdown
    assert result.best_runup == pytest.approx(0.06)
    assert result.mean_similarity == 0.8
    assert result.method_agreement == 0.7


def test_short_candidate_flips_win_definition_and_average_return() -> None:
    """A falling price is the WIN for a short -- same sign convention as
    labeling.py's own triple-barrier walk-forward."""
    analogs = [
        make_analog(subsequent_return=0.05, max_drawdown=-0.02, max_runup=0.06),  # a loss, short
        make_analog(subsequent_return=-0.03, max_drawdown=-0.01, max_runup=0.02),  # a win, short
    ]
    result_in = make_analog_result(analogs)
    result = evaluate_similarity("NIFTY", "short", BASE_TS, result_in)

    assert result.historical_win_rate == pytest.approx(0.5)  # exactly one of the two wins short
    assert result.average_return == pytest.approx((-0.05 + 0.03) / 2)  # signs flipped


def test_short_candidate_swaps_drawdown_and_runup_magnitudes() -> None:
    """A rally against a short (the long path's own best run-up) is the
    short's worst excursion, and vice versa."""
    analogs = [make_analog(subsequent_return=0.01, max_drawdown=-0.10, max_runup=0.20)]
    result = evaluate_similarity("NIFTY", "short", BASE_TS, make_analog_result(analogs))
    assert result.worst_drawdown == pytest.approx(-0.20)  # negated best long run-up
    assert result.best_runup == pytest.approx(0.10)  # negated long drawdown, now positive


def test_neutral_direction_is_treated_like_long() -> None:
    analogs = [make_analog(subsequent_return=0.04, max_drawdown=-0.01, max_runup=0.05)]
    long_result = evaluate_similarity("NIFTY", "long", BASE_TS, make_analog_result(analogs))
    neutral_result = evaluate_similarity("NIFTY", "neutral", BASE_TS, make_analog_result(analogs))
    assert neutral_result.average_return == long_result.average_return
    assert neutral_result.worst_drawdown == long_result.worst_drawdown


def test_no_analogs_is_an_honest_none_not_a_fabricated_zero() -> None:
    result = evaluate_similarity("NIFTY", "long", BASE_TS, make_analog_result([]))
    assert result.n_analogs == 0
    assert result.historical_win_rate is None
    assert result.average_return is None
    assert result.worst_drawdown is None
    assert result.best_runup is None
    assert result.probability_distribution is None


def test_to_dict_serializes_the_probability_distribution() -> None:
    analogs = [make_analog(0.02, -0.01, 0.03), make_analog(0.03, -0.02, 0.04)]
    result = evaluate_similarity("NIFTY", "long", BASE_TS, make_analog_result(analogs))
    payload = result.to_dict()
    assert payload["symbol"] == "NIFTY"
    assert payload["n_analogs"] == 2
    assert set(payload["probability_distribution"].keys()) == {"p10", "p25", "median", "p75", "p90"}


# --- engine, no DB: honest degradation ---------------------------------------


async def test_evaluate_without_a_db_reports_zero_analogs() -> None:
    engine = HistoricalSimilarityEngine(session_factory=None)
    result = await engine.evaluate("NIFTY")
    assert result.n_analogs == 0
    assert result.historical_win_rate is None


async def test_evaluate_candidates_runs_one_per_candidate() -> None:
    from app.prediction.candidates import TradeCandidate

    engine = HistoricalSimilarityEngine(session_factory=None)
    candidates = [
        TradeCandidate(instrument="NIFTY", direction="long", reason="x",
                        priority=1, priority_score=1.0),
        TradeCandidate(instrument="BANKNIFTY", direction="short", reason="y",
                        priority=2, priority_score=0.9),
    ]
    results = await engine.evaluate_candidates(candidates)
    assert [r.symbol for r in results] == ["NIFTY", "BANKNIFTY"]
    assert [r.direction for r in results] == ["long", "short"]


async def test_evaluate_top_candidates_runs_cleanly_without_a_db() -> None:
    engine = HistoricalSimilarityEngine(session_factory=None)
    results = await engine.evaluate_top_candidates()
    assert results == []  # no DB -> no candidates generated -> nothing to evaluate


async def test_recent_returns_empty_list_without_a_session_factory() -> None:
    engine = HistoricalSimilarityEngine(session_factory=None)
    assert await engine.recent("NIFTY") == []
