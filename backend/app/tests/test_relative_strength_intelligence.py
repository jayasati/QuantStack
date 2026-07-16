from app.intelligence.relative import assess_relative_strength


def leading_features(**overrides) -> dict[str, float]:
    features = {}
    for ref in ("nifty", "sensex", "sector", "industry", "peers"):
        for w in (5, 20, 50, 100):
            features[f"rs_{ref}_strength_{w}"] = 4.0
            features[f"rs_{ref}_momentum_{w}"] = 0.3
    for w in (5, 20, 50, 100):
        features[f"rs_outperformance_{w}"] = 82.0
        features[f"rs_percentile_rank_{w}"] = 90.0
    features.update(overrides)
    return features


def lagging_features(**overrides) -> dict[str, float]:
    features = {}
    for ref in ("nifty", "sensex", "sector", "industry", "peers"):
        for w in (5, 20, 50, 100):
            features[f"rs_{ref}_strength_{w}"] = -4.0
            features[f"rs_{ref}_momentum_{w}"] = -0.3
    for w in (5, 20, 50, 100):
        features[f"rs_outperformance_{w}"] = 15.0
        features[f"rs_percentile_rank_{w}"] = 10.0
    features.update(overrides)
    return features


def rotating_features(**overrides) -> dict[str, float]:
    """Beats Nifty/Sensex but lags its own sector/industry/peers."""
    features = {}
    for ref in ("nifty", "sensex"):
        for w in (5, 20, 50, 100):
            features[f"rs_{ref}_strength_{w}"] = 4.0
    for ref in ("sector", "industry", "peers"):
        for w in (5, 20, 50, 100):
            features[f"rs_{ref}_strength_{w}"] = -1.0
    features.update(overrides)
    return features


def test_leading_stock_scores_high_with_leading_dominant() -> None:
    result = assess_relative_strength(leading_features())
    assert result.score > 75
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "leading"


def test_lagging_stock_scores_low_with_lagging_dominant() -> None:
    result = assess_relative_strength(lagging_features())
    assert result.score < 25
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "lagging"


def test_disagreeing_references_favor_rotating_state() -> None:
    result = assess_relative_strength(rotating_features())
    assert result.states["rotating"] > result.states["leading"]
    assert result.states["rotating"] > result.states["lagging"]


def test_leadership_ranking_passthrough() -> None:
    result = assess_relative_strength(leading_features())
    assert result.metrics["leadership_ranking"] == 90.0


def test_relative_momentum_sign_matches_direction() -> None:
    leading = assess_relative_strength(leading_features())
    lagging = assess_relative_strength(lagging_features())
    assert leading.metrics["relative_momentum"] > 0
    assert lagging.metrics["relative_momentum"] < 0


def test_reference_agreement_high_when_all_refs_agree() -> None:
    result = assess_relative_strength(leading_features())
    assert result.metrics["reference_agreement"] == 1.0


def test_reference_agreement_lower_when_refs_disagree() -> None:
    agreeing = assess_relative_strength(leading_features())
    disagreeing = assess_relative_strength(rotating_features())
    assert disagreeing.metrics["reference_agreement"] < agreeing.metrics["reference_agreement"]


def test_states_sum_to_one() -> None:
    result = assess_relative_strength(leading_features())
    assert abs(sum(result.states.values()) - 1.0) < 1e-9


def test_no_data_defaults_to_neutral_with_low_confidence() -> None:
    result = assess_relative_strength({})
    assert result.score == 50.0
    assert result.confidence < 0.3
    assert result.metrics["leadership_ranking"] is None
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "in_line"


def test_more_complete_data_increases_confidence() -> None:
    sparse = assess_relative_strength({"rs_nifty_strength_20": 2.0})
    rich = assess_relative_strength(leading_features())
    assert rich.confidence > sparse.confidence


# --- Intraday overlay (DEBT-1, 2026-07-16) ------------------------------------


def test_omitted_intraday_matches_none_exactly() -> None:
    with_none = assess_relative_strength(
        leading_features(), intraday_features=None, intraday_benchmarks=None
    )
    omitted = assess_relative_strength(leading_features())
    assert with_none.score == omitted.score
    assert with_none.confidence == omitted.confidence
    assert with_none.states == omitted.states


def test_intraday_outperformance_vs_covered_benchmarks_reinforces_leading() -> None:
    result = assess_relative_strength(
        leading_features(),
        intraday_features={"intraday_move_from_open_pct": 2.0},
        intraday_benchmarks={
            "nifty": {"intraday_move_from_open_pct": 0.3},
            "sensex": {"intraday_move_from_open_pct": 0.2},
        },
    )
    assert result.metrics["intraday_relative_references"] == ["nifty", "sensex"]
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "leading"


def test_intraday_underperformance_docks_confidence_on_a_leading_read() -> None:
    calm = assess_relative_strength(
        leading_features(),
        intraday_features={"intraday_move_from_open_pct": 0.5},
        intraday_benchmarks={
            "nifty": {"intraday_move_from_open_pct": 0.3},
            "sensex": {"intraday_move_from_open_pct": 0.3},
        },
    )
    reversing = assess_relative_strength(
        leading_features(),
        intraday_features={"intraday_move_from_open_pct": -3.0},
        intraday_benchmarks={
            "nifty": {"intraday_move_from_open_pct": 0.5},
            "sensex": {"intraday_move_from_open_pct": 0.4},
        },
    )
    assert reversing.confidence < calm.confidence
    assert reversing.metrics["relative_trend"] < calm.metrics["relative_trend"]


def test_only_nifty_covered_still_applies_a_partial_overlay() -> None:
    """sensex missing (e.g. cold start) -- nifty alone should still apply,
    not silently skip the whole overlay."""
    result = assess_relative_strength(
        leading_features(),
        intraday_features={"intraday_move_from_open_pct": 2.0},
        intraday_benchmarks={"nifty": {"intraday_move_from_open_pct": 0.2}},
    )
    assert result.metrics["intraday_relative_references"] == ["nifty"]


def test_no_benchmark_coverage_degrades_to_no_overlay() -> None:
    result = assess_relative_strength(
        leading_features(),
        intraday_features={"intraday_move_from_open_pct": 2.0},
        intraday_benchmarks={"sector": {"intraday_move_from_open_pct": 0.1}},
    )
    assert result.metrics["intraday_relative_references"] is None
