import math
import random

import pytest

from app.features.selection import (
    linear_shap_importance,
    mutual_information,
    permutation_importance,
    rfe_ranks,
    ridge_fit,
    select_features,
)


def make_dataset(n: int = 200):
    rng = random.Random(9)
    driver = [rng.gauss(0, 1) for _ in range(n)]
    noise = [rng.gauss(0, 1) for _ in range(n)]
    weak = [rng.gauss(0, 1) for _ in range(n)]
    target = [0.9 * driver[i] + 0.15 * weak[i] + 0.1 * rng.gauss(0, 1) for i in range(n)]
    matrix = {
        "driver": driver,
        "duplicate": [v * 1.0000001 + 1e-9 for v in driver],  # redundant copy
        "weak": weak,
        "noise": noise,
        "constant": [7.0] * n,
    }
    return matrix, target


def test_mutual_information_orders_signal_over_noise() -> None:
    matrix, target = make_dataset()
    mi_driver = mutual_information(matrix["driver"], target)
    mi_noise = mutual_information(matrix["noise"], target)
    assert mi_driver > mi_noise
    assert mi_driver > 0.1


def test_ridge_recovers_linear_coefficients() -> None:
    rng = random.Random(4)
    x1 = [rng.gauss(0, 1) for _ in range(300)]
    x2 = [rng.gauss(0, 1) for _ in range(300)]
    y = [2.0 * a - 1.0 * b for a, b in zip(x1, x2, strict=True)]
    coefs = ridge_fit([x1, x2], y, lam=0.01)
    assert coefs[0] == pytest.approx(2.0, abs=0.05)
    assert coefs[1] == pytest.approx(-1.0, abs=0.05)


def test_permutation_and_shap_prefer_the_driver() -> None:
    matrix, target = make_dataset()
    columns = [matrix["driver"], matrix["noise"]]
    coefs = ridge_fit(columns, target)
    permutation = permutation_importance(columns, coefs, target)
    shap = linear_shap_importance(columns, coefs)
    assert permutation[0] > permutation[1]
    assert shap[0] > shap[1]


def test_rfe_keeps_the_driver_longest() -> None:
    matrix, target = make_dataset()
    columns = [matrix["driver"], matrix["weak"], matrix["noise"]]
    ranks = rfe_ranks(columns, target)
    assert ranks[0] == 1  # driver survives to the end
    assert ranks[2] == 3  # noise eliminated first


def test_select_features_full_pipeline() -> None:
    matrix, target = make_dataset()
    outcome = select_features(matrix, target, max_features=3)
    assert outcome["dropped_low_variance"] == ["constant"]
    assert outcome["redundant"] == ["duplicate"]
    assert outcome["correlated_pairs"][0]["keep"] == "driver"
    assert outcome["correlated_pairs"][0]["correlation"] == pytest.approx(1.0)
    assert outcome["recommended"][0] == "driver"
    assert "duplicate" not in outcome["recommended"]
    ranked_names = [entry["feature_name"] for entry in outcome["ranking"]]
    assert ranked_names.index("driver") < ranked_names.index("noise")
    for entry in outcome["ranking"]:
        assert entry["mutual_information"] >= 0
        assert entry["rfe_rank"] >= 1


def test_selection_is_deterministic() -> None:
    matrix, target = make_dataset()
    first = select_features(matrix, target)
    second = select_features(matrix, target)
    assert first == second


def test_mi_handles_degenerate_input() -> None:
    values = [1.0] * 50
    target = [math.sin(i) for i in range(50)]
    assert mutual_information(values, target) >= 0.0
