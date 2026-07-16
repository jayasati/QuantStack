import math
import random
import time

import pytest

from app.features.selection import (
    MODEL_CANDIDATES,
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


def _make_large_dataset(n: int = 250, columns: int = 800):
    """Production-scale fixture: live HDFCBANK/D had 781 distinct stored
    features and 250 bars (Volume 3 DEBT-9 build, 2026-07-16) -- the unit
    tests above only ever used a handful of columns, which hid an O(columns^2)
    cost in the pairwise-correlation redundancy scan (measured live: 12.5s/
    symbol) until this was scheduled to run periodically."""
    rng = random.Random(11)
    driver = [rng.gauss(0, 1) for _ in range(n)]
    target = [0.9 * driver[i] + 0.1 * rng.gauss(0, 1) for i in range(n)]
    matrix = {"driver": driver}
    # Every one of these is a near-duplicate of `driver` -- the adversarial
    # case for the early-exit: MI-sorted order puts them all right behind
    # `driver`, so the redundancy scan must actually walk past all of them
    # before finding MODEL_CANDIDATES genuine survivors.
    for i in range(columns // 2):
        matrix[f"dup_{i}"] = [v * 1.0000001 + 1e-9 for v in driver]
    for i in range(columns - columns // 2 - 1):
        matrix[f"noise_{i}"] = [rng.gauss(0, 1) for _ in range(n)]
    return matrix, target


def test_select_features_scales_to_live_column_counts() -> None:
    matrix, target = _make_large_dataset()
    start = time.perf_counter()
    outcome = select_features(matrix, target, max_features=10)
    elapsed = time.perf_counter() - start
    # Locally measured ~0.7s post-fix at this scale (was multi-second
    # unbounded); generous bound so this catches a real regression without
    # being flaky on a slower CI box.
    assert elapsed < 5.0
    assert outcome["recommended"][0] == "driver"
    assert len(outcome["ranking"]) <= MODEL_CANDIDATES


def test_early_exit_is_correctness_preserving_past_the_cutoff() -> None:
    """A near-duplicate pair with no relationship to the target, placed
    after enough real signal to fill the survivor quota, must still be
    excluded correctly even though the redundancy scan stops once
    MODEL_CANDIDATES survivors are found -- it was never a candidate for
    `recommended` either way, so cutting the scan short must not change
    `recommended`/`ranking` (only `redundant`/`correlated_pairs` may shrink,
    since pairs entirely past the cutoff go unreported by design)."""
    rng = random.Random(3)
    n = 250
    base = [rng.gauss(0, 1) for _ in range(n)]
    target = base
    matrix: dict[str, list[float]] = {}
    # More than MODEL_CANDIDATES features directly correlated with the
    # target -- these fill the survivor quota before the MI-sorted scan
    # ever reaches the independent-noise tail below.
    for i in range(MODEL_CANDIDATES + 5):
        noise = [rng.gauss(0, 0.3) for _ in range(n)]
        matrix[f"driver_{i}"] = [b + e for b, e in zip(base, noise, strict=True)]
    # Independent of target and of every driver above -- lowest MI, and a
    # near-duplicate of each other.
    weak = [rng.gauss(0, 1) for _ in range(n)]
    matrix["weak_a"] = weak
    matrix["weak_b"] = [v * 1.0000001 + 1e-9 for v in weak]

    outcome = select_features(matrix, target, max_features=MODEL_CANDIDATES)
    assert "weak_a" not in outcome["recommended"]
    assert "weak_b" not in outcome["recommended"]
    assert len(outcome["ranking"]) == MODEL_CANDIDATES
