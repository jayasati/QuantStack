from app.intelligence.volatility import assess_volatility


def low_vol_features(**overrides) -> dict[str, float]:
    features = {
        "volatility_regime_5": 0.0,
        "volatility_regime_20": 0.0,
        "volatility_regime_50": 0.0,
        "volatility_regime_100": 0.0,
        "volatility_hist_5": 8.0,
        "volatility_hist_20": 9.0,
        "volatility_hist_50": 10.0,
        "volatility_hist_100": 10.5,
        "volatility_of_volatility_20": 0.5,
        "volatility_compression_20": 0.8,
        "volatility_expansion_prob_20": 0.74,
        "volatility_expected_move_5": 120.0,
        "volatility_vix_distance_20": 0.5,
    }
    features.update(overrides)
    return features


def high_vol_features(**overrides) -> dict[str, float]:
    features = {
        "volatility_regime_5": 2.0,
        "volatility_regime_20": 2.0,
        "volatility_regime_50": 2.0,
        "volatility_regime_100": 2.0,
        "volatility_hist_5": 45.0,
        "volatility_hist_20": 40.0,
        "volatility_hist_50": 35.0,
        "volatility_hist_100": 32.0,
        "volatility_of_volatility_20": 8.0,
        "volatility_compression_20": 0.05,
        "volatility_expansion_prob_20": 0.14,
        "volatility_expected_move_5": 900.0,
        "volatility_vix_distance_20": -8.0,
    }
    features.update(overrides)
    return features


def test_low_vol_reads_low_with_high_score_below_50() -> None:
    result = assess_volatility(low_vol_features())
    assert result.score < 35
    assert result.metrics["volatility_level"] < 0.35
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant in {"extremely_low", "low"}


def test_high_vol_reads_high_with_score_above_50() -> None:
    result = assess_volatility(high_vol_features())
    assert result.score > 70
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant in {"high", "extreme"}
    assert abs(sum(result.states.values()) - 1.0) < 1e-9


def test_compression_and_expansion_probability_are_averaged_across_windows() -> None:
    result = assess_volatility(low_vol_features(
        volatility_compression_5=0.9, volatility_compression_20=0.7,
        volatility_expansion_prob_5=0.82, volatility_expansion_prob_20=0.66,
    ))
    assert result.metrics["compression_probability"] == (0.9 + 0.7) / 2
    assert result.metrics["expansion_probability"] == (0.82 + 0.66) / 2


def test_negative_vix_distance_tilts_level_up() -> None:
    calm = assess_volatility(low_vol_features(volatility_vix_distance_20=0.0))
    implied_hot = assess_volatility(low_vol_features(volatility_vix_distance_20=-9.0))
    assert implied_hot.metrics["volatility_level"] > calm.metrics["volatility_level"]
    assert implied_hot.metrics["vix_realized_minus_implied"] == -9.0


def test_missing_vix_data_degrades_gracefully() -> None:
    features = low_vol_features()
    del features["volatility_vix_distance_20"]
    result = assess_volatility(features)
    assert result.metrics["vix_realized_minus_implied"] is None
    # Confidence should still be computable and bounded.
    assert 0.0 <= result.confidence <= 1.0


def test_disagreement_across_windows_lowers_confidence() -> None:
    agreeing = assess_volatility(low_vol_features())
    disagreeing = assess_volatility(low_vol_features(
        volatility_regime_5=2.0, volatility_regime_100=2.0,
    ))
    assert disagreeing.confidence < agreeing.confidence


def test_high_vol_of_vol_lowers_confidence() -> None:
    stable = assess_volatility(high_vol_features(volatility_of_volatility_20=1.0))
    unstable = assess_volatility(high_vol_features(volatility_of_volatility_20=40.0))
    assert unstable.confidence < stable.confidence
    assert unstable.metrics["vol_of_vol_instability"] > stable.metrics["vol_of_vol_instability"]


def test_no_data_defaults_to_neutral_with_low_confidence() -> None:
    result = assess_volatility({})
    assert result.score == 50.0
    assert result.confidence < 0.4
    assert result.metrics["expected_volatility_pct"] is None


def test_expected_move_prefers_shortest_available_window() -> None:
    result = assess_volatility(low_vol_features(
        volatility_expected_move_5=100.0, volatility_expected_move_20=250.0,
    ))
    assert result.metrics["expected_move_price"] == 100.0
