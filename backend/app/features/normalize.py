"""Feature Normalization Engine (Volume 3, Prompt 3.13).

Every method operates on a trailing window ending at the current bar, so no
value ever sees the future (look-ahead bias prevention); missing values are
skipped rather than imputed; and bars with fewer than `min_obs` trailing
observations stay None (cold start). Raw series are always stored alongside
their normalized companions — the engines emit both.

Methods: rolling z-score, min-max scaling, robust scaling (median/IQR),
percentile rank, signed log transform, and winsorization — all reachable
through :func:`normalize_series`.
"""

import math
from collections import deque
from dataclasses import replace
from statistics import fmean, median, pstdev

from app.features.schema import FeatureDefinition, Series

NORMALIZED_SUFFIX = "_z"

NORMALIZATION_METHODS = (
    "zscore", "minmax", "robust", "percentile", "log", "winsorize",
)


def _default_min_obs(window: int) -> int:
    return max(10, window // 10)


def _trailing(series: Series, i: int, window: int) -> list[float]:
    return [v for v in series[max(0, i - window + 1) : i + 1] if v is not None]


def rolling_zscore(series: Series, window: int, min_obs: int | None = None) -> Series:
    """Z-score of each value against the trailing `window` bars (inclusive).

    Bars with fewer than `min_obs` non-None trailing values, or zero variance,
    stay None (cold start / degenerate distribution).

    O(n) via a sliding sum/sum-of-squares (add the entering bar, subtract the
    one that just fell out of the window) rather than re-scanning and
    re-reducing the trailing slice at every index -- the naive version this
    replaced was O(n*window), which showed up live (2026-07-14, found via
    py-spy on the production process) as the single largest CPU cost in a
    "slow" request, independent of any database work. Matches the original's
    output within ordinary floating-point tolerance -- see
    test_normalize_rolling_equivalence.py, which compares this against a
    preserved copy of the original O(n*window) implementation across many
    randomized series (including None gaps, constant runs, and extreme
    values) rather than trusting the derivation alone.
    """
    if min_obs is None:
        min_obs = _default_min_obs(window)
    n = len(series)
    out: Series = [None] * n
    count = 0
    total = 0.0
    total_sq = 0.0
    for i in range(n):
        entering = series[i]
        if entering is not None:
            count += 1
            total += entering
            total_sq += entering * entering
        left = i - window
        if left >= 0:
            leaving = series[left]
            if leaving is not None:
                count -= 1
                total -= leaving
                total_sq -= leaving * leaving

        value = series[i]
        if value is None or count < min_obs:
            continue
        mean = total / count
        variance = total_sq / count - mean * mean
        if variance <= 0:
            continue  # degenerate (zero, or a hair negative from float error)
        std = math.sqrt(variance)
        out[i] = (value - mean) / std
    return out


def rolling_minmax(series: Series, window: int, min_obs: int | None = None) -> Series:
    """Position of each value between the trailing window's min and max, 0..1.

    O(n) amortized via two monotonic deques (classic sliding-window min/max)
    instead of calling min()/max() over the trailing slice at every index
    (O(n*window)) -- see rolling_zscore's docstring for why this class of
    fix matters. Each deque holds (index, value) pairs in an order that
    keeps the current window's min (resp. max) at the front; a value being
    pushed pops any deque tail that could never win against it, and a
    front is dropped once its index falls outside the window. Every index
    is pushed once and popped at most once, so total work is O(n) despite
    the window potentially containing many entries.
    """
    if min_obs is None:
        min_obs = _default_min_obs(window)
    n = len(series)
    out: Series = [None] * n
    count = 0
    min_deque: deque[tuple[int, float]] = deque()
    max_deque: deque[tuple[int, float]] = deque()

    for i in range(n):
        entering = series[i]
        if entering is not None:
            count += 1
            while min_deque and min_deque[-1][1] >= entering:
                min_deque.pop()
            min_deque.append((i, entering))
            while max_deque and max_deque[-1][1] <= entering:
                max_deque.pop()
            max_deque.append((i, entering))

        left = i - window
        if left >= 0:
            leaving = series[left]
            if leaving is not None:
                count -= 1
            if min_deque and min_deque[0][0] == left:
                min_deque.popleft()
            if max_deque and max_deque[0][0] == left:
                max_deque.popleft()

        value = series[i]
        if value is None or count < min_obs:
            continue
        low, high = min_deque[0][1], max_deque[0][1]
        if high > low:
            out[i] = (value - low) / (high - low)
    return out


def _quartiles(values: list[float]) -> tuple[float, float]:
    ordered = sorted(values)
    n = len(ordered)

    def quantile(q: float) -> float:
        position = q * (n - 1)
        lower = int(position)
        upper = min(lower + 1, n - 1)
        fraction = position - lower
        return ordered[lower] * (1 - fraction) + ordered[upper] * fraction

    return quantile(0.25), quantile(0.75)


def rolling_robust(series: Series, window: int, min_obs: int | None = None) -> Series:
    """Robust scaling: (value - median) / IQR over the trailing window.

    Outlier-resistant — a handful of extreme bars cannot distort the scale
    the way they distort a z-score.
    """
    if min_obs is None:
        min_obs = _default_min_obs(window)
    out: Series = [None] * len(series)
    for i, value in enumerate(series):
        if value is None:
            continue
        trailing = _trailing(series, i, window)
        if len(trailing) < min_obs:
            continue
        q1, q3 = _quartiles(trailing)
        iqr = q3 - q1
        if iqr > 0:
            out[i] = (value - median(trailing)) / iqr
    return out


def rolling_percentile_rank(
    series: Series, window: int, min_obs: int | None = None
) -> Series:
    """Percentile (0..1) of each value within its trailing window."""
    if min_obs is None:
        min_obs = _default_min_obs(window)
    out: Series = [None] * len(series)
    for i, value in enumerate(series):
        if value is None:
            continue
        trailing = _trailing(series, i, window)
        if len(trailing) < min_obs:
            continue
        out[i] = sum(1 for v in trailing if v <= value) / len(trailing)
    return out


def log_transform(series: Series) -> Series:
    """Signed log1p: sign(x) * ln(1 + |x|). Compresses heavy tails while
    preserving sign and zero; needs no window (element-wise)."""
    return [
        math.copysign(math.log1p(abs(v)), v) if v is not None else None
        for v in series
    ]


def rolling_winsorize(
    series: Series,
    window: int,
    limits: tuple[float, float] = (0.05, 0.95),
    min_obs: int | None = None,
) -> Series:
    """Clamp each value to the [low, high] quantiles of the *prior* window —
    outliers are capped, never dropped. The current bar is excluded from the
    quantile estimate so a spike cannot set its own cap."""
    if min_obs is None:
        min_obs = _default_min_obs(window)
    low_q, high_q = limits
    out: Series = [None] * len(series)
    for i, value in enumerate(series):
        if value is None:
            continue
        history = [v for v in series[max(0, i - window) : i] if v is not None]
        if len(history) < min_obs:
            out[i] = value  # cold start: pass through unclamped, never None
            continue
        ordered = sorted(history)
        n = len(ordered)
        low = ordered[max(0, min(n - 1, int(low_q * (n - 1))))]
        high = ordered[max(0, min(n - 1, int(math.ceil(high_q * (n - 1)))))]
        out[i] = min(max(value, low), high)
    return out


def normalize_series(
    series: Series,
    method: str,
    window: int = 100,
    min_obs: int | None = None,
) -> Series:
    """Dispatch to a normalization method by name (see NORMALIZATION_METHODS)."""
    if method == "zscore":
        return rolling_zscore(series, window, min_obs)
    if method == "minmax":
        return rolling_minmax(series, window, min_obs)
    if method == "robust":
        return rolling_robust(series, window, min_obs)
    if method == "percentile":
        return rolling_percentile_rank(series, window, min_obs)
    if method == "log":
        return log_transform(series)
    if method == "winsorize":
        return rolling_winsorize(series, window, min_obs=min_obs)
    raise ValueError(f"unknown normalization method: {method!r}")


def rolling_slope(series: Series, window: int) -> Series:
    """Least-squares slope over the trailing window, per step.

    None until the window is fully populated (no partial-window slopes).
    """
    n = len(series)
    out: Series = [None] * n
    if window < 2:
        return out
    mean_t = (window - 1) / 2
    var_t = fmean([(t - mean_t) ** 2 for t in range(window)])
    for i in range(window - 1, n):
        window_values = series[i - window + 1 : i + 1]
        values = [v for v in window_values if v is not None]
        if len(values) < window:
            continue
        mean_v = fmean(values)
        out[i] = fmean(
            [(t - mean_t) * (v - mean_v) for t, v in enumerate(values)]
        ) / var_t
    return out


def rolling_correlation(a: Series, b: Series, window: int) -> Series:
    """Pearson correlation of two series over the trailing window, per step.

    None until the window is fully populated in both series, or when either
    side has zero variance.

    O(n) via sliding sums (Sx, Sy, Sxx, Syy, Sxy) over the current window's
    paired-non-None entries, plus a running count of "gap" positions (where
    either side is None) -- the original's `len(pairs) < window` check
    requires a COMPLETELY clean window (unlike rolling_zscore's min_obs,
    which tolerates partial gaps), so a window is usable exactly when the
    gap count is 0. See rolling_zscore's docstring for why this class of
    fix matters, and test_normalize_rolling_equivalence.py for the
    randomized-input equivalence check against the original O(n*window)
    implementation this replaced.
    """
    n = min(len(a), len(b))
    out: Series = [None] * n
    gaps = 0
    sx = sy = sxx = syy = sxy = 0.0

    def _add(idx: int) -> None:
        nonlocal gaps, sx, sy, sxx, syy, sxy
        x, y = a[idx], b[idx]
        if x is None or y is None:
            gaps += 1
        else:
            sx += x
            sy += y
            sxx += x * x
            syy += y * y
            sxy += x * y

    def _remove(idx: int) -> None:
        nonlocal gaps, sx, sy, sxx, syy, sxy
        x, y = a[idx], b[idx]
        if x is None or y is None:
            gaps -= 1
        else:
            sx -= x
            sy -= y
            sxx -= x * x
            syy -= y * y
            sxy -= x * y

    for i in range(n):
        _add(i)
        left = i - window
        if left >= 0:
            _remove(left)

        if i < window - 1 or gaps > 0:
            continue
        mean_x, mean_y = sx / window, sy / window
        var_x = sxx / window - mean_x * mean_x
        var_y = syy / window - mean_y * mean_y
        if var_x <= 0 or var_y <= 0:
            continue
        cov = sxy / window - mean_x * mean_y
        # Pearson is mathematically bounded; clamp float overshoot at +/-1.
        out[i] = max(-1.0, min(1.0, cov / (var_x * var_y) ** 0.5))
    return out


def trailing_percentile(series: Series, i: int, window: int, min_obs: int) -> float | None:
    """Percentile (0..1) of series[i] among the trailing `window` values, no look-ahead."""
    current = series[i]
    if current is None:
        return None
    trailing = [v for v in series[max(0, i - window + 1) : i + 1] if v is not None]
    if len(trailing) < min_obs:
        return None
    return sum(1 for v in trailing if v <= current) / len(trailing)


def normalized_definition(definition: FeatureDefinition, window: int) -> FeatureDefinition:
    """Registry metadata for the z-score companion of a raw feature."""
    return replace(
        definition,
        feature_name=definition.feature_name + NORMALIZED_SUFFIX,
        description=(
            f"Rolling z-score of {definition.feature_name} over {window} bars "
            "(look-ahead safe)."
        ),
        dependencies=(definition.feature_name,),
        unit="zscore",
        expected_range=(-10.0, 10.0),
    )


def add_normalized_series(series: dict[str, Series], window: int) -> dict[str, Series]:
    """Extend a feature map with a z-score companion for every raw series."""
    normalized = {
        name + NORMALIZED_SUFFIX: rolling_zscore(values, window)
        for name, values in series.items()
    }
    return {**series, **normalized}
