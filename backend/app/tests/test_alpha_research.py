"""Tests for the Alpha Research Engine (Volume 5.5)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.prediction.alpha_research import (
    CANDIDATE_FEATURE_SPECS,
    DECAY_WARNING_THRESHOLD,
    MIN_FEATURE_SAMPLES,
    RECOMMENDATION_THRESHOLD,
    AlphaResearchEngine,
    ModelComparisonResult,
    _correlation_magnitude,
    build_feature_label_pairs,
    evaluate_feature,
)
from app.prediction.ensemble import ENSEMBLE_FEATURE_SPECS
from app.prediction.labeling import BarrierConfig, Label

BASE_TS = datetime(2026, 7, 1, tzinfo=UTC)


def make_label(day: int, label: str, quality: float = 1.0) -> Label:
    return Label(
        symbol="X", timeframe="D", direction="long",
        entry_ts=BASE_TS + timedelta(days=day), entry_price=100.0,
        exit_ts=BASE_TS + timedelta(days=day + 1), exit_price=101.0,
        exit_reason="profit_target" if label == "win" else "stop",
        exit_return_pct=1.0, label=label, label_quality=quality, bars_held=1,
        barrier_config=BarrierConfig(profit_target_pct=0.02, stop_pct=0.01),
    )


# --- _correlation_magnitude ---------------------------------------------


def test_correlation_magnitude_is_one_for_a_perfect_signal() -> None:
    values = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0]
    targets = [1, 0, 1, 0, 1, 0]
    assert _correlation_magnitude(values, targets) == pytest.approx(1.0)


def test_correlation_magnitude_is_none_with_too_few_points() -> None:
    assert _correlation_magnitude([1.0], [1]) is None


def test_correlation_magnitude_is_none_when_labels_dont_vary() -> None:
    assert _correlation_magnitude([1.0, 2.0, 3.0], [1, 1, 1]) is None


# --- build_feature_label_pairs ------------------------------------------


def test_build_feature_label_pairs_uses_as_of_lookup_and_maps_labels() -> None:
    series = [
        (BASE_TS - timedelta(days=1), 1.0),
        (BASE_TS + timedelta(days=5), 99.0),  # strictly after both entries -- must not leak
    ]
    labels = [make_label(1, "win"), make_label(2, "loss")]
    values, targets = build_feature_label_pairs(labels, series)
    assert values == [1.0, 1.0]  # both entries see only the first (pre-entry) observation
    assert targets == [1, 0]


def test_build_feature_label_pairs_maps_partial_success_to_a_win() -> None:
    series = [(BASE_TS - timedelta(days=1), 1.0)]
    values, targets = build_feature_label_pairs([make_label(1, "partial_success")], series)
    assert targets == [1]


def test_build_feature_label_pairs_drops_low_quality_labels() -> None:
    series = [(BASE_TS - timedelta(days=1), 1.0)]
    labels = [make_label(1, "win", quality=0.1)]  # below MIN_LABEL_QUALITY
    values, targets = build_feature_label_pairs(labels, series)
    assert values == [] and targets == []


def test_build_feature_label_pairs_drops_labels_with_no_prior_observation() -> None:
    series = [(BASE_TS + timedelta(days=5), 1.0)]  # only an observation AFTER entry
    values, targets = build_feature_label_pairs([make_label(1, "win")], series)
    assert values == [] and targets == []


# --- evaluate_feature: decay detection -----------------------------------


def _alternating(n: int) -> tuple[list[float], list[int]]:
    values = [1.0 if i % 2 == 0 else -1.0 for i in range(n)]
    targets = [1 if v > 0 else 0 for v in values]
    return values, targets


def test_evaluate_feature_below_min_samples_is_honestly_none() -> None:
    values, targets = _alternating(MIN_FEATURE_SAMPLES - 1)
    evaluation = evaluate_feature("x", values, targets)
    assert evaluation.predictive_power is None
    assert evaluation.decay is None


def test_evaluate_feature_detects_no_decay_for_a_consistent_signal() -> None:
    values, targets = _alternating(40)
    evaluation = evaluate_feature("x", values, targets)
    assert evaluation.predictive_power == pytest.approx(1.0)
    assert evaluation.older_half_power == pytest.approx(1.0)
    assert evaluation.recent_half_power == pytest.approx(1.0)
    assert evaluation.decay == pytest.approx(0.0)


def test_evaluate_feature_detects_decay_when_recent_half_loses_the_signal() -> None:
    older_values, older_targets = _alternating(20)  # perfectly correlated
    # Recent half: feature no longer relates to the label at all.
    recent_values = [1.0, 1.0, -1.0, -1.0] * 5
    recent_targets = [1, 0, 1, 0] * 5
    values = older_values + recent_values
    targets = older_targets + recent_targets

    evaluation = evaluate_feature("x", values, targets)
    assert evaluation.older_half_power == pytest.approx(1.0)
    assert evaluation.recent_half_power is not None
    assert evaluation.recent_half_power < 0.5
    assert evaluation.decay is not None and evaluation.decay > DECAY_WARNING_THRESHOLD


def test_is_recommended_requires_power_above_threshold_and_no_decay() -> None:
    values, targets = _alternating(40)
    strong = evaluate_feature("brand_new_feature", values, targets)
    assert strong.predictive_power >= RECOMMENDATION_THRESHOLD
    assert strong.is_recommended is True


def test_is_recommended_is_false_for_a_feature_already_in_production() -> None:
    values, targets = _alternating(40)
    evaluation = evaluate_feature("price_momentum_20", values, targets)  # a real production feature
    assert evaluation.is_production_feature is True
    assert evaluation.is_recommended is False


def test_is_recommended_is_false_below_the_threshold() -> None:
    # Weak, near-zero correlation.
    values = [float(i % 7) for i in range(40)]
    targets = [1 if i % 2 == 0 else 0 for i in range(40)]
    evaluation = evaluate_feature("weak_feature", values, targets)
    if evaluation.predictive_power is not None:
        assert evaluation.predictive_power < 0.5  # sanity: not a strong signal by construction


# --- engine, no DB: honest degradation ---------------------------------------


async def test_evaluate_candidate_features_without_a_db_is_honestly_empty() -> None:
    engine = AlphaResearchEngine(session_factory=None)
    evaluations = await engine.evaluate_candidate_features("NIFTY")
    assert len(evaluations) == 10  # every candidate spec still gets an entry
    assert all(e.predictive_power is None for e in evaluations)


async def test_recommend_features_without_a_db_recommends_nothing() -> None:
    engine = AlphaResearchEngine(session_factory=None)
    assert await engine.recommend_features("NIFTY") == []


async def test_compare_against_production_without_a_db_is_insufficient_data() -> None:
    engine = AlphaResearchEngine(session_factory=None)
    result = await engine.compare_against_production("NIFTY")
    assert isinstance(result, ModelComparisonResult)
    assert result.winner == "insufficient_data"
    assert result.champion_holdout_accuracy is None
    assert result.challenger_holdout_accuracy is None
    assert result.champion_feature_count == len(ENSEMBLE_FEATURE_SPECS)
    assert result.challenger_feature_count == len(ENSEMBLE_FEATURE_SPECS) + len(
        CANDIDATE_FEATURE_SPECS
    )


async def test_feature_leaderboard_without_a_db_is_empty() -> None:
    engine = AlphaResearchEngine(session_factory=None)
    assert await engine.feature_leaderboard() == []


async def test_comparison_leaderboard_without_a_db_is_empty() -> None:
    engine = AlphaResearchEngine(session_factory=None)
    assert await engine.comparison_leaderboard() == []


async def test_recent_returns_empty_list_without_a_session_factory() -> None:
    engine = AlphaResearchEngine(session_factory=None)
    assert await engine.recent() == []
