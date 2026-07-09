from app.intelligence.events import assess_event_risk


def imminent_high_impact(**overrides) -> dict[str, float]:
    features = {
        "event_market_sensitivity": 0.75,
        "event_hours_until_next": 1.5,
        "event_expected_volatility": 3.5,
        "event_category_impact": 1.0,
        "event_confidence_reduction": 0.5,
        "event_trading_freeze": 1.0,
        "event_historical_similarity": 0.8,
    }
    features.update(overrides)
    return features


def distant_low_impact(**overrides) -> dict[str, float]:
    features = {
        "event_market_sensitivity": 0.05,
        "event_hours_until_next": 40.0,
        "event_expected_volatility": 1.1,
        "event_category_impact": 0.0,
        "event_confidence_reduction": 0.0,
        "event_trading_freeze": 0.0,
        "event_historical_similarity": 0.9,
    }
    features.update(overrides)
    return features


def test_imminent_high_impact_event_scores_high() -> None:
    result = assess_event_risk(imminent_high_impact())
    assert result.score > 70


def test_distant_low_impact_event_scores_low() -> None:
    result = assess_event_risk(distant_low_impact())
    assert result.score < 30


def test_trading_freeze_flag_forces_freeze_state_dominant() -> None:
    result = assess_event_risk(imminent_high_impact())
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "freeze_recommended"


def test_freeze_flag_floors_state_even_if_level_alone_would_not() -> None:
    # Moderate level, but the collector explicitly recommends a freeze.
    features = distant_low_impact(
        event_trading_freeze=1.0, event_market_sensitivity=0.3, event_hours_until_next=20.0,
    )
    result = assess_event_risk(features)
    assert result.states["freeze_recommended"] > result.states["clear"]


def test_confidence_reduction_directly_lowers_confidence() -> None:
    calm = assess_event_risk(distant_low_impact(event_confidence_reduction=0.0))
    reduced = assess_event_risk(distant_low_impact(event_confidence_reduction=0.6))
    assert reduced.confidence < calm.confidence


def test_novel_event_mix_lowers_confidence_further() -> None:
    familiar = assess_event_risk(imminent_high_impact(event_historical_similarity=0.9))
    novel = assess_event_risk(imminent_high_impact(event_historical_similarity=0.0))
    assert novel.confidence < familiar.confidence


def test_expected_impact_score_from_category_impact() -> None:
    result = assess_event_risk(imminent_high_impact())
    assert result.metrics["expected_impact_score"] == 100.0


def test_metrics_passthrough() -> None:
    result = assess_event_risk(imminent_high_impact())
    assert result.metrics["hours_until_event"] == 1.5
    assert result.metrics["confidence_reduction"] == 0.5
    assert result.metrics["historical_similarity"] == 0.8
    assert result.metrics["trading_freeze_recommended"] is True


def test_states_sum_to_one() -> None:
    result = assess_event_risk(imminent_high_impact())
    assert abs(sum(result.states.values()) - 1.0) < 1e-9


def test_no_calendar_data_defaults_to_zero_risk_not_neutral() -> None:
    result = assess_event_risk({})
    assert result.score == 0.0
    assert result.metrics["trading_freeze_recommended"] is False
    assert result.confidence <= 0.6  # docked for missing reduction + similarity signals


def test_no_data_confidence_lower_than_full_data_with_no_risk() -> None:
    no_data = assess_event_risk({})
    known_clear = assess_event_risk(distant_low_impact())
    assert known_clear.confidence > no_data.confidence
