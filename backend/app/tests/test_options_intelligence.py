from app.intelligence.options import assess_options


def bullish_features(**overrides) -> dict[str, float]:
    """Decisively (not just moderately) bullish -- a moderate lean is
    expected to read as "mixed" (the same honest-uncertainty design
    breadth.py already uses), so these values sit near the extremes of
    each feature's documented expected_range."""
    features = {
        "options_dealer_positioning": 0.9,
        "options_pcr": 0.3,
        "options_max_pain_distance_pct": 15.0,
        "options_atm_iv": 18.0,
        "options_iv_rank": 50.0,
        "options_call_writing_score": 0.2,
        "options_put_writing_score": 0.5,
    }
    features.update(overrides)
    return features


def bearish_features(**overrides) -> dict[str, float]:
    features = {
        "options_dealer_positioning": -0.9,
        "options_pcr": 1.7,
        "options_max_pain_distance_pct": -15.0,
        "options_atm_iv": 22.0,
        "options_iv_rank": 50.0,
        "options_call_writing_score": 0.5,
        "options_put_writing_score": 0.2,
    }
    features.update(overrides)
    return features


def test_bullish_positioning_scores_high_and_reads_bullish() -> None:
    result = assess_options(bullish_features())
    assert result.score > 80
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "bullish_positioning"


def test_bearish_positioning_scores_low_and_reads_bearish() -> None:
    result = assess_options(bearish_features())
    assert result.score < 20
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "bearish_positioning"


def test_high_pcr_alone_leans_bearish() -> None:
    neutral = assess_options({"options_pcr": 1.0})
    put_heavy = assess_options({"options_pcr": 2.0})
    assert put_heavy.score < neutral.score


def test_positive_max_pain_distance_alone_leans_bullish() -> None:
    neutral = assess_options({"options_max_pain_distance_pct": 0.0})
    pulled_up = assess_options({"options_max_pain_distance_pct": 8.0})
    assert pulled_up.score > neutral.score


def test_states_sum_to_one() -> None:
    result = assess_options(bullish_features())
    assert abs(sum(result.states.values()) - 1.0) < 1e-9


def test_no_data_defaults_to_neutral_with_low_confidence() -> None:
    result = assess_options({})
    assert result.score == 50.0
    assert result.confidence < 0.4


def test_more_complete_data_increases_confidence() -> None:
    sparse = assess_options({"options_dealer_positioning": 0.3})
    rich = assess_options(bullish_features())
    assert rich.confidence > sparse.confidence


def test_elevated_iv_rank_reads_elevated_iv_state() -> None:
    result = assess_options({"options_dealer_positioning": 0.0, "options_iv_rank": 90.0})
    assert result.states["elevated_iv"] > result.states["compressed_iv"]


def test_compressed_iv_rank_reads_compressed_iv_state() -> None:
    result = assess_options({"options_dealer_positioning": 0.0, "options_iv_rank": 10.0})
    assert result.states["compressed_iv"] > result.states["elevated_iv"]


def test_gamma_exposure_does_not_affect_directional_level() -> None:
    without_gamma = assess_options(bullish_features())
    with_gamma = assess_options(bullish_features(options_gamma_exposure=1_000_000.0))
    assert without_gamma.score == with_gamma.score


def test_call_put_writing_alone_is_not_double_counted_with_dealer_positioning() -> None:
    """options_dealer_positioning is derived from call/put writing scores
    (features/options.py:186-188) -- adding writing scores on top of an
    already-present dealer_positioning must not shift the directional level,
    only add explanatory contributions."""
    with_writing = assess_options(bullish_features())
    without_writing = assess_options({
        k: v for k, v in bullish_features().items()
        if k not in ("options_call_writing_score", "options_put_writing_score")
    })
    assert with_writing.score == without_writing.score
