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
