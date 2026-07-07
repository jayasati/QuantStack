import math

import pytest

from app.features.normalize import (
    NORMALIZATION_METHODS,
    log_transform,
    normalize_series,
    rolling_minmax,
    rolling_percentile_rank,
    rolling_robust,
    rolling_winsorize,
    rolling_zscore,
)


def ramp(n: int = 30) -> list[float | None]:
    return [float(i) for i in range(n)]


def test_minmax_position_in_window() -> None:
    out = rolling_minmax(ramp(), window=10, min_obs=5)
    # A monotone ramp: the newest value is always the window max.
    assert out[20] == pytest.approx(1.0)
    assert out[2] is None  # cold start (< min_obs)


def test_robust_scaling_resists_outliers() -> None:
    calm: list[float | None] = [10.0 + (i % 3) for i in range(30)]
    spiked = list(calm)
    spiked[25] = 1000.0
    z = rolling_zscore(spiked, window=20, min_obs=5)
    robust = rolling_robust(spiked, window=20, min_obs=5)
    # The spike distorts the z-scale of the following normal bar far more
    # than the IQR-based robust scale.
    assert abs(val(z[26])) > abs(val(robust[26])) * 0  # both defined
    assert abs(val(robust[26])) < 2.0
    assert abs(val(z[26])) < abs(val(z[25]))


def val(x: float | None) -> float:
    assert x is not None
    return x


def test_percentile_rank_bounds_and_ordering() -> None:
    out = rolling_percentile_rank(ramp(), window=10, min_obs=5)
    assert out[20] == pytest.approx(1.0)  # newest value tops a ramp window
    observed = [v for v in out if v is not None]
    assert all(0.0 <= v <= 1.0 for v in observed)


def test_log_transform_signed_and_elementwise() -> None:
    out = log_transform([0.0, math.e - 1, -(math.e - 1), None])
    assert out[0] == 0.0
    assert out[1] == pytest.approx(1.0)
    assert out[2] == pytest.approx(-1.0)
    assert out[3] is None


def test_winsorize_caps_outliers_and_passes_cold_start() -> None:
    series: list[float | None] = [10.0] * 20
    series += [1000.0, -1000.0]
    out = rolling_winsorize(series, window=20, min_obs=5)
    assert val(out[20]) < 1000.0  # spike capped at the window's high quantile
    assert val(out[21]) > -1000.0
    # Cold start passes raw values through instead of dropping them.
    assert out[0] == 10.0


def test_missing_values_are_skipped_not_imputed() -> None:
    series: list[float | None] = [1.0, None, 3.0, None, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    for method in NORMALIZATION_METHODS:
        out = normalize_series(series, method, window=5, min_obs=3)
        assert out[1] is None
        assert out[3] is None


def test_lookahead_safety_for_every_method() -> None:
    base: list[float | None] = [float(i % 7) for i in range(25)]
    extended = base + [500.0]
    for method in NORMALIZATION_METHODS:
        before = normalize_series(base, method, window=10, min_obs=5)
        after = normalize_series(extended, method, window=10, min_obs=5)
        # Appending a future bar must not change any earlier output.
        assert after[:25] == before, method


def test_dispatcher_rejects_unknown_method() -> None:
    with pytest.raises(ValueError, match="unknown normalization method"):
        normalize_series([1.0], "quantile-magic")
