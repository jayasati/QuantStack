from app.intelligence.momentum import assess_momentum


def bullish_building_features(**overrides) -> dict[str, float]:
    features = {
        "price_momentum_5": 8.0, "price_momentum_20": 15.0,
        "price_momentum_50": 20.0, "price_momentum_200": 25.0,
        "price_acceleration_5": 2.0, "price_acceleration_20": 3.0,
        "price_acceleration_50": 4.0, "price_acceleration_200": 5.0,
        "price_momentum_5_z": 1.0, "price_momentum_20_z": 1.2,
    }
    features.update(overrides)
    return features


def bearish_fading_features(**overrides) -> dict[str, float]:
    features = {
        "price_momentum_5": -8.0, "price_momentum_20": -15.0,
        "price_momentum_50": -20.0, "price_momentum_200": -25.0,
        "price_acceleration_5": -2.0, "price_acceleration_20": -3.0,
        "price_acceleration_50": -4.0, "price_acceleration_200": -5.0,
        "price_momentum_5_z": -1.0, "price_momentum_20_z": -1.2,
    }
    features.update(overrides)
    return features


def test_bullish_building_momentum_scores_high_and_reads_accelerating_bullish() -> None:
    result = assess_momentum(bullish_building_features())
    assert result.score > 65
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "accelerating_bullish"


def test_bearish_fading_momentum_scores_low_and_reads_accelerating_bearish() -> None:
    result = assess_momentum(bearish_fading_features())
    assert result.score < 35
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "accelerating_bearish"


def test_decelerating_momentum_reads_decelerating_not_accelerating() -> None:
    """Strong bullish level, but acceleration has flipped negative --
    momentum is still positive but losing steam."""
    result = assess_momentum(bullish_building_features(
        price_acceleration_5=-2.0, price_acceleration_20=-3.0,
        price_acceleration_50=-4.0, price_acceleration_200=-5.0,
    ))
    assert result.states["decelerating"] > result.states["accelerating_bullish"]


def test_extreme_z_score_reads_extreme_state() -> None:
    extreme = assess_momentum(bullish_building_features(
        price_momentum_5_z=3.5, price_momentum_20_z=3.2,
    ))
    normal = assess_momentum(bullish_building_features(
        price_momentum_5_z=0.2, price_momentum_20_z=0.1,
    ))
    assert extreme.metrics["is_extreme"] is True
    assert normal.metrics["is_extreme"] is False
    assert extreme.states["extreme"] > normal.states["extreme"]


def test_states_sum_to_one() -> None:
    result = assess_momentum(bullish_building_features())
    assert abs(sum(result.states.values()) - 1.0) < 1e-9


def test_no_data_defaults_to_neutral_with_low_confidence() -> None:
    result = assess_momentum({})
    assert result.score == 50.0
    assert result.confidence < 0.3


def test_more_complete_data_increases_confidence() -> None:
    sparse = assess_momentum({"price_momentum_5": 5.0})
    rich = assess_momentum(bullish_building_features())
    assert rich.confidence > sparse.confidence


def test_missing_windows_do_not_crash_and_still_produce_a_result() -> None:
    result = assess_momentum({"price_momentum_20": 10.0})
    assert 0.0 <= result.score <= 100.0


# --- Intraday overlay (DEBT-1/DEBT-2, 2026-07-16) -----------------------------


def test_omitted_intraday_matches_none_exactly() -> None:
    with_none = assess_momentum(bullish_building_features(), intraday_features=None)
    omitted = assess_momentum(bullish_building_features())
    assert with_none.score == omitted.score
    assert with_none.confidence == omitted.confidence
    assert with_none.states == omitted.states


def test_intraday_reversal_docks_confidence_on_bullish_read() -> None:
    calm = assess_momentum(
        bullish_building_features(),
        intraday_features={"intraday_move_from_open_pct": 0.3},
    )
    reversing = assess_momentum(
        bullish_building_features(),
        intraday_features={"intraday_move_from_open_pct": -4.0},
    )
    assert reversing.confidence < calm.confidence
    assert reversing.metrics["momentum_level"] < calm.metrics["momentum_level"]
