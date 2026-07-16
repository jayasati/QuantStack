from app.intelligence.trend import assess_trend


def bull_features(**overrides) -> dict[str, float]:
    features = {
        "price_momentum_5": 2.0,
        "price_momentum_20": 5.0,
        "price_momentum_50": 9.0,
        "price_momentum_200": 18.0,
        "price_acceleration_20": 1.0,
        "price_dist_from_high_50": -0.5,
        "price_dist_from_low_50": 8.0,
        "ms_trend_direction": 1.0,
        "ms_structural_bias": 0.8,
        "ms_breakout_probability": 0.6,
        "ms_break_of_structure": 0.0,  # steady trend, no active break
    }
    features.update(overrides)
    return features


def test_active_break_of_structure_reads_as_breakout() -> None:
    result = assess_trend(
        bull_features(ms_break_of_structure=1.0, ms_breakout_probability=0.9),
        direction_history=[1.0] * 30,
    )
    assert max(result.states, key=lambda s: result.states[s]) == "breakout"


def test_strong_bull_reads_bullish() -> None:
    result = assess_trend(bull_features(), direction_history=[1.0] * 30)
    assert result.score > 75
    assert result.metrics["trend_direction"] > 0.5
    assert result.metrics["trend_strength"] > 0.5
    assert result.confidence > 0.7
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "strong_bull_trend"
    assert abs(sum(result.states.values()) - 1.0) < 1e-9


def test_strong_bear_mirror() -> None:
    features = {
        "price_momentum_5": -2.0, "price_momentum_20": -5.0,
        "price_momentum_50": -9.0, "price_momentum_200": -18.0,
        "price_acceleration_20": -1.0, "price_dist_from_low_50": 0.4,
        "ms_trend_direction": -1.0, "ms_structural_bias": -0.8,
    }
    result = assess_trend(features, direction_history=[-1.0] * 30)
    assert result.score < 25
    assert max(result.states, key=lambda s: result.states[s]) == "strong_bear_trend"


def test_flat_market_is_range_bound() -> None:
    features = {
        "price_momentum_5": 0.1, "price_momentum_20": -0.2,
        "price_momentum_50": 0.05, "price_momentum_200": 0.3,
        "ms_trend_direction": 0.0, "ms_structural_bias": 0.05,
    }
    result = assess_trend(features, direction_history=[0.0] * 20)
    assert 40 < result.score < 60
    assert max(result.states, key=lambda s: result.states[s]) == "range_bound"


def test_trend_age_and_stability_from_history() -> None:
    history = [-1.0] * 10 + [1.0] * 15
    result = assess_trend(bull_features(), direction_history=history)
    assert result.metrics["trend_age_bars"] == 15
    assert result.metrics["trend_stability"] == 15 / 20  # 15 of last 20 bars


def test_exhaustion_rises_when_old_trend_decelerates_at_highs() -> None:
    fresh = assess_trend(
        bull_features(price_acceleration_20=1.5), direction_history=[1.0] * 5
    )
    exhausted = assess_trend(
        bull_features(price_acceleration_20=-3.0, price_dist_from_high_50=-0.1),
        direction_history=[1.0] * 80,
    )
    assert exhausted.metrics["trend_exhaustion"] > fresh.metrics["trend_exhaustion"]
    assert exhausted.metrics["trend_exhaustion"] > 0.4


def test_volume_confirmation_optional_and_confidence_bounded() -> None:
    without_volume = assess_trend(bull_features(), direction_history=[1.0] * 30)
    assert without_volume.metrics["volume_confirmation"] is None
    assert any("volume" in r.lower() for r in without_volume.reasoning)

    with_volume = assess_trend(
        bull_features(volume_rvol_20=1.8, volume_obv_z=1.2),
        direction_history=[1.0] * 30,
    )
    assert with_volume.metrics["volume_confirmation"] is not None
    assert with_volume.confidence >= without_volume.confidence
    assert 0.0 <= with_volume.confidence <= 1.0


def test_empty_features_degrade_gracefully() -> None:
    result = assess_trend({}, direction_history=[])
    assert result.score == 50
    assert result.confidence < 0.5
    assert result.metrics["trend_age_bars"] == 0


def test_explainability_payload_present() -> None:
    result = assess_trend(bull_features(), direction_history=[1.0] * 30)
    assert result.contributions
    features_cited = {c.feature for c in result.contributions}
    assert "price_momentum_20" in features_cited
    assert "ms_trend_direction" in features_cited
    assert result.reasoning
    payload = result.to_dict()
    assert payload["component"] == "trend"
    assert isinstance(payload["contributions"], list)


# --- Intraday overlay (DEBT-1/DEBT-2, 2026-07-16) -----------------------------


def test_no_intraday_features_is_byte_identical_to_omitting_the_param() -> None:
    """Explicit None must match the default -- callers can't accidentally
    change behavior by passing None instead of omitting the argument."""
    with_none = assess_trend(bull_features(), direction_history=[1.0] * 30, intraday_features=None)
    without_arg = assess_trend(bull_features(), direction_history=[1.0] * 30)
    assert with_none.score == without_arg.score
    assert with_none.confidence == without_arg.confidence
    assert with_none.states == without_arg.states
    assert with_none.metrics == without_arg.metrics


def test_intraday_move_agreeing_with_trend_keeps_bullish_read() -> None:
    result = assess_trend(
        bull_features(), direction_history=[1.0] * 30,
        intraday_features={
            "intraday_move_from_open_pct": 1.5, "intraday_current_drawdown_pct": 0.1,
        },
    )
    assert result.metrics["trend_direction"] > 0.5
    assert result.metrics["intraday_direction"] > 0
    assert max(result.states, key=lambda s: result.states[s]) == "strong_bull_trend"


def test_intraday_collapse_docks_confidence_on_an_otherwise_bullish_read() -> None:
    """The HDFCBANK 2026-07-15 scenario: D-based evidence says bullish, but
    today's actual session move is a real decline -- confidence must drop
    visibly rather than sit unchanged like the original incident."""
    calm = assess_trend(
        bull_features(), direction_history=[1.0] * 30,
        intraday_features={
            "intraday_move_from_open_pct": 0.1, "intraday_current_drawdown_pct": 0.2,
        },
    )
    collapsing = assess_trend(
        bull_features(), direction_history=[1.0] * 30,
        intraday_features={
            "intraday_move_from_open_pct": -3.0, "intraday_current_drawdown_pct": 3.2,
        },
    )
    assert collapsing.confidence < calm.confidence
    assert collapsing.metrics["trend_exhaustion"] > calm.metrics["trend_exhaustion"]
    assert any("opposes" in r.lower() for r in collapsing.reasoning)


def test_large_enough_intraday_move_can_flip_direction() -> None:
    """A strong enough same-day move against a weak D-based read should be
    able to flip the blended direction, not just dampen confidence."""
    weak_bull = {
        "price_momentum_5": 0.3, "price_momentum_20": 0.5,
        "ms_trend_direction": 0.2, "ms_structural_bias": 0.1,
    }
    without_intraday = assess_trend(weak_bull, direction_history=[1.0] * 5)
    with_selloff = assess_trend(
        weak_bull, direction_history=[1.0] * 5,
        intraday_features={"intraday_move_from_open_pct": -4.0},
    )
    assert without_intraday.metrics["trend_direction"] > 0
    assert with_selloff.metrics["trend_direction"] < without_intraday.metrics["trend_direction"]


def test_missing_intraday_move_from_open_degrades_to_no_overlay() -> None:
    """Present but incomplete intraday data (e.g. cold start, no move-from-
    open computed yet) must not crash or silently fabricate a signal."""
    result = assess_trend(
        bull_features(), direction_history=[1.0] * 30,
        intraday_features={"intraday_realized_vol_pct": 12.0},  # no move_from_open key
    )
    assert result.metrics["intraday_direction"] is None
