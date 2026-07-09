from app.intelligence.structure import assess_market_structure


def markup_features(**overrides) -> dict[str, float]:
    features = {
        "ms_structural_bias": 0.6, "ms_trend_direction": 1.0,
        "ms_breakout_probability": 0.7, "ms_sweep_probability": 0.1,
    }
    features.update(overrides)
    return features


def test_confirmed_uptrend_reads_markup() -> None:
    result = assess_market_structure(markup_features())
    assert result.score == 80.0
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "markup"


def test_confirmed_downtrend_reads_markdown() -> None:
    result = assess_market_structure(
        markup_features(ms_structural_bias=-0.6, ms_trend_direction=-1.0)
    )
    assert result.score == 20.0
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "markdown"


def test_bullish_bias_without_confirmed_trend_reads_accumulation_leaning() -> None:
    result = assess_market_structure(
        markup_features(ms_structural_bias=0.5, ms_trend_direction=0.0)
    )
    assert result.states["accumulation"] > result.states["markup"]
    assert result.states["accumulation"] > 0
    assert result.states["markdown"] == 0
    assert result.states["distribution"] == 0


def test_bearish_bias_without_confirmed_trend_reads_distribution_leaning() -> None:
    result = assess_market_structure(
        markup_features(ms_structural_bias=-0.5, ms_trend_direction=0.0)
    )
    assert result.states["distribution"] > 0
    assert result.states["accumulation"] == 0


def test_flat_bias_no_trend_reads_consolidation() -> None:
    result = assess_market_structure(
        markup_features(ms_structural_bias=0.02, ms_trend_direction=0.0)
    )
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "consolidation"


def test_higher_sweep_probability_increases_liquidity_sweep_share() -> None:
    calm = assess_market_structure(markup_features(ms_sweep_probability=0.1))
    elevated = assess_market_structure(markup_features(ms_sweep_probability=0.9))
    assert elevated.states["liquidity_sweep"] > calm.states["liquidity_sweep"]


def test_change_of_character_docks_confidence_not_level() -> None:
    baseline = assess_market_structure(markup_features())
    with_choc = assess_market_structure(markup_features(ms_change_of_character=-1.0))
    assert with_choc.confidence < baseline.confidence
    assert with_choc.score == baseline.score  # level unaffected, only confidence


def test_states_sum_to_one() -> None:
    result = assess_market_structure(markup_features())
    assert abs(sum(result.states.values()) - 1.0) < 1e-9


def test_no_data_defaults_to_neutral_consolidation_low_confidence() -> None:
    result = assess_market_structure({})
    assert result.score == 50.0
    assert result.confidence < 0.3
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "consolidation"
    assert result.metrics["structural_bias"] is None
