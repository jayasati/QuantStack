import pytest

from app.intelligence.analogs import (
    Analog,
    assess_historical_analogs,
    cosine_similarity,
    euclidean_distance,
    path_outcomes,
)


def test_cosine_similarity_identical_vectors_is_one() -> None:
    assert cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_similarity_opposite_vectors_is_negative_one() -> None:
    assert cosine_similarity([1.0, 2.0, 3.0], [-1.0, -2.0, -3.0]) == pytest.approx(-1.0)


def test_cosine_similarity_orthogonal_vectors_is_zero() -> None:
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_zero_vector_is_none() -> None:
    assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) is None


def test_euclidean_distance_basic() -> None:
    assert euclidean_distance([0.0, 0.0], [3.0, 4.0]) == pytest.approx(5.0)


def test_path_outcomes_matches_hand_computed_values() -> None:
    # path: 1.0 -> 1.10 -> 1.045 (1.10*0.95) -> 1.0659 (1.045*1.02)
    outcome = path_outcomes([0.10, -0.05, 0.02])
    assert outcome is not None
    assert outcome["subsequent_return"] == pytest.approx(0.0659, abs=1e-4)
    assert outcome["max_drawdown"] == pytest.approx(-0.05, abs=1e-4)  # 1.045/1.10 - 1
    assert outcome["max_runup"] == pytest.approx(0.10, abs=1e-4)  # 1.10/1.0 - 1
    assert outcome["subsequent_volatility"] > 0


def test_path_outcomes_empty_returns_none() -> None:
    assert path_outcomes([]) is None


def _pool(n_similar: int = 20, n_dissimilar: int = 5) -> tuple[list, dict]:
    historical = []
    outcomes = {}
    for i in range(n_similar):
        date = f"sim-{i}"
        vector = [1.0 + 0.01 * i, 1.0 - 0.01 * i, 1.0 + 0.005 * i]
        historical.append((date, vector))
        outcomes[date] = {
            "subsequent_return": 0.03, "subsequent_volatility": 0.01,
            "max_drawdown": -0.01, "max_runup": 0.04,
        }
    for i in range(n_dissimilar):
        date = f"dis-{i}"
        vector = [-1.0, -1.0, -1.0]
        historical.append((date, vector))
        outcomes[date] = {
            "subsequent_return": -0.05, "subsequent_volatility": 0.02,
            "max_drawdown": -0.06, "max_runup": 0.0,
        }
    return historical, outcomes


def test_similar_analogs_with_positive_outcomes_read_bullish() -> None:
    historical, outcomes = _pool()
    result = assess_historical_analogs([1.0, 1.0, 1.0], historical, outcomes)
    assert len(result.metrics["analogs"]) == 20
    assert all(a["date"].startswith("sim-") for a in result.metrics["analogs"])
    assert result.metrics["win_rate"] == 1.0
    assert result.score > 50
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "bullish_precedent"


def test_top_k_truncates_to_20_even_with_more_similar_candidates() -> None:
    historical, outcomes = _pool(n_similar=30, n_dissimilar=5)
    result = assess_historical_analogs([1.0, 1.0, 1.0], historical, outcomes)
    assert len(result.metrics["analogs"]) == 20


def test_bearish_pool_reads_bearish() -> None:
    # Enough bearish-direction candidates to fill the top-20 on their own —
    # with fewer than 20 (e.g. the default 5), the remaining slots would be
    # filled by the next-least-dissimilar "sim" candidates, diluting the
    # win rate; that's correct behavior for a thin pool, not what this test
    # is checking.
    historical, outcomes = _pool(n_similar=5, n_dissimilar=20)
    result = assess_historical_analogs([-1.0, -1.0, -1.0], historical, outcomes)
    assert result.metrics["win_rate"] == 0.0
    assert result.score < 50
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "bearish_precedent"


def test_no_historical_data_returns_empty_analogs_with_mixed_precedent() -> None:
    result = assess_historical_analogs([1.0, 1.0, 1.0], [], {})
    assert result.metrics["analogs"] == []
    assert result.confidence < 0.2
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "mixed_precedent"


def test_method_disagreement_lowers_confidence() -> None:
    # Same direction as current (cosine ~1) but wildly different magnitude:
    # cosine ranks these first, euclidean ranks them last, since euclidean
    # distance grows with magnitude even when direction is identical.
    historical = []
    outcomes = {}
    for i in range(20):
        date = f"far-{i}"
        historical.append((date, [100.0 + i, 100.0 + i, 100.0 + i]))
        outcomes[date] = {
            "subsequent_return": 0.02, "subsequent_volatility": 0.01,
            "max_drawdown": -0.01, "max_runup": 0.02,
        }
    # A handful of genuinely close-by-both-metrics candidates that fall
    # outside the cosine top-20 but would win on euclidean distance alone.
    for i in range(20):
        date = f"near-{i}"
        historical.append((date, [-1.0, -1.0, -1.0]))
        outcomes[date] = {
            "subsequent_return": -0.02, "subsequent_volatility": 0.01,
            "max_drawdown": -0.03, "max_runup": 0.0,
        }
    result = assess_historical_analogs([1.0, 1.0, 1.0], historical, outcomes)
    assert result.metrics["method_agreement"] < 0.5


def test_states_sum_to_one() -> None:
    historical, outcomes = _pool()
    result = assess_historical_analogs([1.0, 1.0, 1.0], historical, outcomes)
    assert abs(sum(result.states.values()) - 1.0) < 1e-9


def test_analog_dataclass_fields() -> None:
    analog = Analog(
        date="2026-01-01", similarity=0.9, subsequent_return=0.02,
        subsequent_volatility=0.01, max_drawdown=-0.01, max_runup=0.03,
    )
    assert analog.date == "2026-01-01"
    assert analog.similarity == 0.9
