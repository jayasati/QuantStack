"""Feature normalization helpers (Volume 3).

Prompt 3.2 requires every volume feature to ship normalized; the full
Normalization Engine arrives with Prompt 3.13. The rolling z-score here is
look-ahead safe: the window for bar i uses only bars <= i.
"""

from dataclasses import replace
from statistics import fmean, pstdev

from app.features.schema import FeatureDefinition, Series

NORMALIZED_SUFFIX = "_z"


def rolling_zscore(series: Series, window: int, min_obs: int | None = None) -> Series:
    """Z-score of each value against the trailing `window` bars (inclusive).

    Bars with fewer than `min_obs` non-None trailing values, or zero variance,
    stay None (cold start / degenerate distribution).
    """
    if min_obs is None:
        min_obs = max(10, window // 10)
    n = len(series)
    out: Series = [None] * n
    for i in range(n):
        value = series[i]
        if value is None:
            continue
        trailing = [v for v in series[max(0, i - window + 1) : i + 1] if v is not None]
        if len(trailing) < min_obs:
            continue
        std = pstdev(trailing)
        if std > 0:
            out[i] = (value - fmean(trailing)) / std
    return out


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
    """
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
