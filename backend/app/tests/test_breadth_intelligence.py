from app.intelligence.breadth import assess_breadth


def broad_bull_features(**overrides) -> dict[str, float]:
    features = {
        "breadth_strength": 0.6,
        "breadth_participation_pct": 78.0,
        "breadth_trend_pct": 72.0,
        "breadth_divergence": 0.5,
        "breadth_health_score": 82.0,
        "breadth_momentum_5": 0.02,
        "breadth_momentum_20": 0.015,
        "breadth_new_high_momentum_20": 3.0,
        "breadth_new_low_momentum_20": 0.2,
    }
    features.update(overrides)
    return features


def narrow_bull_features(**overrides) -> dict[str, float]:
    """Index looks strong, but only a few large caps are actually up."""
    features = {
        "breadth_strength": 0.5,
        "breadth_participation_pct": 38.0,
        "breadth_trend_pct": 35.0,
        "breadth_divergence": -6.0,
        "breadth_health_score": 45.0,
        "breadth_momentum_5": 0.0,
        "breadth_momentum_20": -0.01,
        "breadth_new_high_momentum_20": 0.5,
        "breadth_new_low_momentum_20": 1.5,
    }
    features.update(overrides)
    return features


def broad_bear_features(**overrides) -> dict[str, float]:
    features = {
        "breadth_strength": -0.6,
        "breadth_participation_pct": 20.0,
        "breadth_trend_pct": 18.0,
        "breadth_divergence": 0.2,
        "breadth_health_score": 15.0,
        "breadth_momentum_5": -0.02,
        "breadth_momentum_20": -0.018,
        "breadth_new_high_momentum_20": 0.1,
        "breadth_new_low_momentum_20": 4.0,
    }
    features.update(overrides)
    return features


def test_broad_bull_scores_above_50_with_broad_participation_dominant() -> None:
    result = assess_breadth(broad_bull_features())
    assert result.score > 65
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "broad_participation"
    assert result.metrics["participation_quality"] > 0.6


def test_broad_bear_scores_below_50() -> None:
    result = assess_breadth(broad_bear_features())
    assert result.score < 35
    assert result.metrics["breadth_health"] < 50


def test_narrow_bull_flags_narrow_participation_despite_index_strength() -> None:
    result = assess_breadth(narrow_bull_features())
    # Index-level strength is positive but breadth quality is poor.
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "narrow_participation"
    assert result.metrics["participation_quality"] < 0.4


def test_negative_divergence_lowers_participation_quality() -> None:
    confirmed = assess_breadth(broad_bull_features(breadth_divergence=1.0))
    narrow = assess_breadth(broad_bull_features(breadth_divergence=-8.0))
    assert narrow.metrics["participation_quality"] < confirmed.metrics["participation_quality"]


def test_missing_health_score_falls_back_to_derived_proxy() -> None:
    features = broad_bull_features()
    del features["breadth_health_score"]
    result = assess_breadth(features)
    assert result.metrics["breadth_health"] is not None
    assert result.metrics["breadth_health"] > 50  # still broad and bullish


def test_improving_vs_deteriorating_breadth_states() -> None:
    improving = assess_breadth(broad_bull_features(
        breadth_momentum_5=0.03, breadth_momentum_20=0.03,
    ))
    deteriorating = assess_breadth(broad_bull_features(
        breadth_momentum_5=-0.03, breadth_momentum_20=-0.03,
    ))
    assert improving.states["improving_breadth"] > improving.states["deteriorating_breadth"]
    assert deteriorating.states["deteriorating_breadth"] > deteriorating.states["improving_breadth"]


def test_states_sum_to_one() -> None:
    result = assess_breadth(broad_bull_features())
    assert abs(sum(result.states.values()) - 1.0) < 1e-9


def test_no_data_defaults_to_neutral_with_low_confidence() -> None:
    result = assess_breadth({})
    assert result.score == 50.0
    assert result.confidence < 0.4
    assert result.metrics["breadth_divergence"] is None


def test_more_complete_data_increases_confidence() -> None:
    sparse = assess_breadth({"breadth_strength": 0.5})
    rich = assess_breadth(broad_bull_features())
    assert rich.confidence > sparse.confidence
