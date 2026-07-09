from app.intelligence.institutional_flow import assess_institutional_flow


def accumulation_features(**overrides) -> dict[str, float]:
    features = {
        "flow_fii_score": 0.6,
        "flow_dii_score": 0.4,
        "flow_etf_score": 0.3,
        "flow_deal_activity_score": 0.5,
        "flow_promoter_score": 0.2,
        "flow_insider_score": 0.1,
        "flow_sast_score": 0.1,
        "flow_participation_index": 78.0,
        "flow_fii_score_momentum_5": 0.02,
        "flow_fii_score_momentum_20": 0.015,
        "flow_dii_score_momentum_20": 0.01,
        "flow_participation_score_momentum_20": 0.02,
    }
    features.update(overrides)
    return features


def distribution_features(**overrides) -> dict[str, float]:
    features = {
        "flow_fii_score": -0.7,
        "flow_dii_score": -0.3,
        "flow_etf_score": -0.2,
        "flow_deal_activity_score": -0.4,
        "flow_promoter_score": -0.1,
        "flow_insider_score": -0.2,
        "flow_sast_score": 0.1,
        "flow_participation_index": 20.0,
        "flow_fii_score_momentum_20": -0.02,
    }
    features.update(overrides)
    return features


def mixed_features(**overrides) -> dict[str, float]:
    """FII selling while DII buys — offsetting, low-agreement flow."""
    features = {
        "flow_fii_score": -0.6,
        "flow_dii_score": 0.6,
        "flow_deal_activity_score": 0.1,
        "flow_promoter_score": -0.1,
        "flow_insider_score": 0.05,
    }
    features.update(overrides)
    return features


def test_participation_index_passthrough_used_as_score() -> None:
    result = assess_institutional_flow(accumulation_features())
    assert result.score == 78.0
    assert result.metrics["participation_index_source"] == "passthrough"


def test_missing_participation_index_recomputed_from_components() -> None:
    features = accumulation_features()
    del features["flow_participation_index"]
    result = assess_institutional_flow(features)
    assert result.metrics["participation_index_source"] == "recomputed"
    assert result.score > 50  # still net-buying components


def test_accumulation_dominant_when_broadly_buying() -> None:
    result = assess_institutional_flow(accumulation_features())
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "institutional_accumulation"
    assert result.metrics["accumulation_score"] > result.metrics["distribution_score"]


def test_distribution_dominant_when_broadly_selling() -> None:
    result = assess_institutional_flow(distribution_features())
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "institutional_distribution"
    assert result.metrics["distribution_score"] > result.metrics["accumulation_score"]
    assert result.score < 50


def test_accumulation_and_distribution_both_present_when_mixed() -> None:
    # FII selling hard, DII buying hard: both gross scores should be
    # substantial even though they net out.
    result = assess_institutional_flow(mixed_features())
    assert result.metrics["accumulation_score"] > 10
    assert result.metrics["distribution_score"] > 10


def test_mixed_flow_state_rises_with_disagreement() -> None:
    agreeing = assess_institutional_flow(accumulation_features())
    disagreeing = assess_institutional_flow(mixed_features())
    assert disagreeing.states["mixed_flow"] > agreeing.states["mixed_flow"]


def test_retail_driven_dominant_when_institutional_flows_are_quiet() -> None:
    result = assess_institutional_flow({
        "flow_fii_score": 0.02, "flow_dii_score": -0.01, "flow_deal_activity_score": 0.0,
    })
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "retail_driven"


def test_flow_momentum_averaged_across_bases_and_windows() -> None:
    result = assess_institutional_flow(accumulation_features())
    assert result.metrics["flow_momentum"] > 0


def test_high_sast_activity_lowers_confidence() -> None:
    calm = assess_institutional_flow(accumulation_features(flow_sast_score=0.1))
    busy = assess_institutional_flow(accumulation_features(flow_sast_score=0.9))
    assert busy.confidence < calm.confidence


def test_states_sum_to_one() -> None:
    result = assess_institutional_flow(accumulation_features())
    assert abs(sum(result.states.values()) - 1.0) < 1e-9


def test_no_data_defaults_to_neutral_with_low_confidence() -> None:
    result = assess_institutional_flow({})
    assert result.score == 50.0
    assert result.confidence < 0.3
    assert result.metrics["accumulation_score"] == 0.0
    assert result.metrics["distribution_score"] == 0.0


def test_more_complete_data_increases_confidence() -> None:
    sparse = assess_institutional_flow({"flow_fii_score": 0.5})
    rich = assess_institutional_flow(accumulation_features())
    assert rich.confidence > sparse.confidence
