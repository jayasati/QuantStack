from app.intelligence.macro import FACTOR_UNIVERSE, assess_macro


def test_strong_risk_on_scores_above_50_with_risk_on_dominant() -> None:
    result = assess_macro({f: 0.4 for f in FACTOR_UNIVERSE})
    assert result.score == 70.0
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "risk_on"
    assert result.metrics["consistency"] == 1.0


def test_strong_risk_off_scores_below_50_with_risk_off_dominant() -> None:
    result = assess_macro({f: -0.4 for f in FACTOR_UNIVERSE})
    assert result.score == 30.0
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "risk_off"


def test_quiet_factors_read_mixed() -> None:
    result = assess_macro({f: 0.01 for f in FACTOR_UNIVERSE})
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "mixed"


def test_partial_data_reduces_data_completeness() -> None:
    half = dict.fromkeys(FACTOR_UNIVERSE[: len(FACTOR_UNIVERSE) // 2], 0.4)
    result = assess_macro(half)
    assert result.metrics["factors_present"] == len(FACTOR_UNIVERSE) // 2


def test_no_data_defaults_to_neutral_with_zero_confidence() -> None:
    result = assess_macro(dict.fromkeys(FACTOR_UNIVERSE))
    assert result.score == 50.0
    assert result.confidence == 0.0
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "mixed"


def test_disagreeing_factors_lower_consistency() -> None:
    agreeing = assess_macro({f: 0.4 for f in FACTOR_UNIVERSE})
    disagreeing = dict.fromkeys(FACTOR_UNIVERSE, 0.4)
    keys = list(FACTOR_UNIVERSE)
    for k in keys[: len(keys) // 2]:
        disagreeing[k] = -0.4
    result = assess_macro(disagreeing)
    assert result.metrics["consistency"] < agreeing.metrics["consistency"]


def test_states_sum_to_one() -> None:
    result = assess_macro({f: 0.4 for f in FACTOR_UNIVERSE})
    assert abs(sum(result.states.values()) - 1.0) < 1e-9
