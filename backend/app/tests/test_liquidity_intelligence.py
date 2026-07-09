from app.intelligence.liquidity import assess_liquidity


def liquid_features(**overrides) -> dict[str, float]:
    features = {
        "liquidity_score": 85.0,
        "liquidity_spread_pct": 0.08,
        "liquidity_market_impact_pct": 0.06,
        "liquidity_order_book_imbalance": 0.05,
        "liquidity_trend_5": 0.5,
        "liquidity_trend_20": 0.3,
        "liquidity_turnover": 5_000_000.0,
        "liquidity_delivery_pct": 55.0,
        "liquidity_turnover_z": 0.2,
        "liquidity_delivery_pct_z": 0.3,
    }
    features.update(overrides)
    return features


def illiquid_features(**overrides) -> dict[str, float]:
    features = {
        "liquidity_score": 12.0,
        "liquidity_spread_pct": 0.9,
        "liquidity_market_impact_pct": 0.45,
        "liquidity_order_book_imbalance": -0.7,
        "liquidity_trend_5": -4.0,
        "liquidity_trend_20": -3.5,
        "liquidity_turnover": 50_000.0,
        "liquidity_delivery_pct": 20.0,
        "liquidity_turnover_z": 2.0,
        "liquidity_delivery_pct_z": -1.5,
    }
    features.update(overrides)
    return features


def test_liquid_instrument_scores_high_and_reads_highly_liquid() -> None:
    result = assess_liquidity(liquid_features())
    assert result.score > 75
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant in {"healthy", "highly_liquid"}


def test_illiquid_instrument_scores_low() -> None:
    result = assess_liquidity(illiquid_features())
    assert result.score < 25
    assert result.metrics["liquidity_stress"] > 30


def test_execution_risk_reflects_spread_and_impact() -> None:
    liquid = assess_liquidity(liquid_features())
    illiquid = assess_liquidity(illiquid_features())
    assert illiquid.metrics["execution_risk"] > liquid.metrics["execution_risk"]


def test_thin_book_with_heavy_imbalance_favors_auction_driven_over_healthy() -> None:
    result = assess_liquidity(illiquid_features())
    # Severely one-sided book on an already-thin instrument.
    assert result.states["auction_driven"] > result.states["healthy"]


def test_churn_signal_flags_volume_without_delivery_conviction() -> None:
    genuine = assess_liquidity(
        liquid_features(liquidity_turnover_z=0.5, liquidity_delivery_pct_z=0.5)
    )
    churny = assess_liquidity(
        liquid_features(liquidity_turnover_z=3.0, liquidity_delivery_pct_z=-1.0)
    )
    assert churny.metrics["liquidity_stress"] > genuine.metrics["liquidity_stress"]


def test_states_sum_to_one() -> None:
    result = assess_liquidity(liquid_features())
    assert abs(sum(result.states.values()) - 1.0) < 1e-9


def test_missing_score_falls_back_to_neutral_level() -> None:
    features = liquid_features()
    del features["liquidity_score"]
    result = assess_liquidity(features)
    assert result.score == 50.0
    assert result.metrics["liquidity_level"] == 0.5


def test_no_data_defaults_to_neutral_with_low_confidence() -> None:
    result = assess_liquidity({})
    assert result.score == 50.0
    assert result.confidence < 0.3
    assert result.metrics["order_book_imbalance"] is None


def test_more_complete_data_increases_confidence() -> None:
    sparse = assess_liquidity({"liquidity_score": 60.0})
    rich = assess_liquidity(liquid_features())
    assert rich.confidence > sparse.confidence


def test_deteriorating_trend_lowers_confidence_and_raises_stress() -> None:
    stable = assess_liquidity(liquid_features())
    deteriorating = assess_liquidity(
        liquid_features(liquidity_trend_5=-3.0, liquidity_trend_20=-3.0)
    )
    assert deteriorating.confidence < stable.confidence
    assert deteriorating.metrics["liquidity_stress"] > stable.metrics["liquidity_stress"]
