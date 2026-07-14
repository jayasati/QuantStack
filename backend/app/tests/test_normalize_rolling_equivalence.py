"""Equivalence tests for the O(n) rewrites of normalize.py's rolling
z-score, min-max, and correlation functions (IRR-2026-07-11 finding #7
follow-up: found live via py-spy that the original O(n*window)
implementations -- re-scanning and re-reducing the trailing window at
every index -- were the dominant CPU cost in slow production requests,
independent of any database work).

Each naive_* function below is a byte-for-byte copy of the ORIGINAL
implementation (pre-rewrite) it stands in for, kept here specifically so
these tests compare the new fast implementation against genuine ground
truth rather than against a second copy of the same (possibly also
wrong) new logic. Do not "clean up" or share code between the naive_*
functions and the real ones in app/features/normalize.py -- that would
defeat the point.
"""

import random
from statistics import fmean, pstdev

import pytest

from app.features.normalize import rolling_correlation, rolling_minmax, rolling_zscore

Series = list[float | None]


# --- naive reference implementations (pre-rewrite, preserved verbatim) -----

def _naive_trailing(series: Series, i: int, window: int) -> list[float]:
    return [v for v in series[max(0, i - window + 1) : i + 1] if v is not None]


def naive_rolling_zscore(series: Series, window: int, min_obs: int) -> Series:
    n = len(series)
    out: Series = [None] * n
    for i in range(n):
        value = series[i]
        if value is None:
            continue
        trailing = _naive_trailing(series, i, window)
        if len(trailing) < min_obs:
            continue
        std = pstdev(trailing)
        if std > 0:
            out[i] = (value - fmean(trailing)) / std
    return out


def naive_rolling_minmax(series: Series, window: int, min_obs: int) -> Series:
    out: Series = [None] * len(series)
    for i, value in enumerate(series):
        if value is None:
            continue
        trailing = _naive_trailing(series, i, window)
        if len(trailing) < min_obs:
            continue
        low, high = min(trailing), max(trailing)
        if high > low:
            out[i] = (value - low) / (high - low)
    return out


def naive_rolling_correlation(a: Series, b: Series, window: int) -> Series:
    n = min(len(a), len(b))
    out: Series = [None] * n
    for i in range(window - 1, n):
        pairs = [
            (x, y)
            for x, y in zip(a[i - window + 1 : i + 1], b[i - window + 1 : i + 1], strict=True)
            if x is not None and y is not None
        ]
        if len(pairs) < window:
            continue
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        mean_x, mean_y = fmean(xs), fmean(ys)
        var_x = fmean([(x - mean_x) ** 2 for x in xs])
        var_y = fmean([(y - mean_y) ** 2 for y in ys])
        if var_x <= 0 or var_y <= 0:
            continue
        cov = fmean([(x - mean_x) * (y - mean_y) for x, y in pairs])
        out[i] = max(-1.0, min(1.0, cov / (var_x * var_y) ** 0.5))
    return out


# --- randomized series generation -------------------------------------------

def _random_series(rng: random.Random, n: int, none_probability: float,
                    value_scale: float = 10.0) -> Series:
    return [
        None if rng.random() < none_probability else round(rng.uniform(-value_scale, value_scale), 6)
        for _ in range(n)
    ]


def _assert_series_close(actual: Series, expected: Series) -> None:
    assert len(actual) == len(expected)
    for i, (a, e) in enumerate(zip(actual, expected)):
        if e is None:
            assert a is None, f"index {i}: expected None, got {a}"
        else:
            assert a is not None, f"index {i}: expected {e}, got None"
            assert a == pytest.approx(e, rel=1e-9, abs=1e-9), f"index {i}: {a} != {e}"


CASES = [
    # (n, window, min_obs, none_probability)
    (5, 10, 3, 0.0),      # shorter than window
    (50, 10, 3, 0.0),     # no gaps
    (50, 10, 3, 0.2),     # scattered gaps
    (50, 10, 3, 0.5),     # heavy gaps
    (200, 20, 5, 0.1),    # larger scale
    (200, 50, 10, 0.0),   # larger window, no gaps
    (30, 5, 2, 0.0),      # small window
    (1, 10, 3, 0.0),      # single element
    (0, 10, 3, 0.0),      # empty
]

SEEDS = [1, 2, 3, 4, 5]


@pytest.mark.parametrize("n,window,min_obs,none_prob", CASES)
@pytest.mark.parametrize("seed", SEEDS)
def test_rolling_zscore_matches_naive_reference(seed, n, window, min_obs, none_prob) -> None:
    series = _random_series(random.Random(seed * 1000 + n), n, none_prob)
    _assert_series_close(
        rolling_zscore(series, window, min_obs),
        naive_rolling_zscore(series, window, min_obs),
    )


@pytest.mark.parametrize("n,window,min_obs,none_prob", CASES)
@pytest.mark.parametrize("seed", SEEDS)
def test_rolling_minmax_matches_naive_reference(seed, n, window, min_obs, none_prob) -> None:
    series = _random_series(random.Random(seed * 2000 + n), n, none_prob)
    _assert_series_close(
        rolling_minmax(series, window, min_obs),
        naive_rolling_minmax(series, window, min_obs),
    )


@pytest.mark.parametrize("n,window,min_obs,none_prob", CASES)
@pytest.mark.parametrize("seed", SEEDS)
def test_rolling_correlation_matches_naive_reference(seed, n, window, min_obs, none_prob) -> None:
    rng = random.Random(seed * 3000 + n)
    a = _random_series(rng, n, none_prob)
    b = _random_series(rng, n, none_prob)
    _assert_series_close(
        rolling_correlation(a, b, window),
        naive_rolling_correlation(a, b, window),
    )


# --- specific edge cases worth naming, not just leaving to random luck -----

def test_rolling_zscore_constant_series_is_all_none_zero_variance() -> None:
    series = [5.0] * 30
    out = rolling_zscore(series, window=10, min_obs=3)
    assert all(v is None for v in out)


def test_rolling_minmax_constant_series_is_all_none_zero_range() -> None:
    series = [5.0] * 30
    out = rolling_minmax(series, window=10, min_obs=3)
    assert all(v is None for v in out)


def test_rolling_correlation_constant_series_is_all_none_zero_variance() -> None:
    a = [5.0] * 30
    b = [float(i) for i in range(30)]
    out = rolling_correlation(a, b, window=10)
    assert all(v is None for v in out)


def test_rolling_correlation_perfect_positive_correlation() -> None:
    a = [float(i) for i in range(30)]
    b = [2.0 * i + 1.0 for i in range(30)]
    out = rolling_correlation(a, b, window=10)
    assert out[15] == pytest.approx(1.0)


def test_rolling_correlation_perfect_negative_correlation() -> None:
    a = [float(i) for i in range(30)]
    b = [-2.0 * i + 1.0 for i in range(30)]
    out = rolling_correlation(a, b, window=10)
    assert out[15] == pytest.approx(-1.0)


def test_rolling_correlation_single_gap_invalidates_the_whole_window() -> None:
    a: Series = [float(i) for i in range(30)]
    b: Series = [float(i) for i in range(30)]
    b[12] = None  # gap at index 12
    out = rolling_correlation(a, b, window=10)
    assert out[15] is None  # window [6..15] contains the gap at 12
    assert out[21] is None  # window [12..21] still contains it (inclusive)
    assert out[22] is not None  # window [13..22] is the first to clear it


def test_rolling_minmax_min_obs_cold_start() -> None:
    series = [float(i) for i in range(30)]
    out = rolling_minmax(series, window=10, min_obs=5)
    assert out[2] is None  # fewer than min_obs trailing values
    assert out[20] == pytest.approx(1.0)  # newest value tops a rising ramp


def test_rolling_zscore_sliding_correctness_across_a_long_series() -> None:
    """Specifically exercises many successive add/remove transitions --
    the part a single random case might not stress enough."""
    rng = random.Random(42)
    series: Series = [round(rng.gauss(0, 1), 4) for _ in range(500)]
    for idx in rng.sample(range(50, 450), 20):
        series[idx] = None
    _assert_series_close(
        rolling_zscore(series, window=30, min_obs=8),
        naive_rolling_zscore(series, window=30, min_obs=8),
    )
