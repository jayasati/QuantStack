"""Distribution-comparison statistics shared by the feature quality and
drift engines (Volume 3, Prompts 3.14 / 3.15).

All functions compare a reference sample against a recent sample and are
pure — no I/O, deterministic, None-free inputs.
"""

import math
from collections.abc import Sequence
from statistics import fmean, pstdev

_EPSILON = 1e-6


def ks_statistic(reference: Sequence[float], recent: Sequence[float]) -> float | None:
    """Kolmogorov-Smirnov statistic: max ECDF distance, 0..1."""
    if not reference or not recent:
        return None
    ref_sorted = sorted(reference)
    rec_sorted = sorted(recent)
    support = sorted(set(ref_sorted) | set(rec_sorted))

    def ecdf(values: list[float], x: float) -> float:
        low, high = 0, len(values)
        while low < high:
            mid = (low + high) // 2
            if values[mid] <= x:
                low = mid + 1
            else:
                high = mid
        return low / len(values)

    return max(abs(ecdf(ref_sorted, x) - ecdf(rec_sorted, x)) for x in support)


def _histogram(
    values: Sequence[float], edges: Sequence[float]
) -> list[float]:
    counts = [0] * (len(edges) + 1)
    for v in values:
        placed = False
        for b, edge in enumerate(edges):
            if v <= edge:
                counts[b] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    total = len(values)
    return [c / total for c in counts]


def _quantile_edges(reference: Sequence[float], bins: int) -> list[float]:
    ordered = sorted(reference)
    n = len(ordered)
    edges = []
    for b in range(1, bins):
        position = b * (n - 1) / bins
        lower = int(position)
        fraction = position - lower
        upper = min(lower + 1, n - 1)
        edges.append(ordered[lower] * (1 - fraction) + ordered[upper] * fraction)
    return edges


def psi(
    reference: Sequence[float], recent: Sequence[float], bins: int = 10
) -> float | None:
    """Population Stability Index over reference-quantile bins.

    Rule of thumb: < 0.1 stable, 0.1-0.25 moderate shift, > 0.25 major shift.
    """
    if len(reference) < bins or not recent:
        return None
    edges = _quantile_edges(reference, bins)
    ref_dist = _histogram(reference, edges)
    rec_dist = _histogram(recent, edges)
    total = 0.0
    for p_ref, p_rec in zip(ref_dist, rec_dist, strict=True):
        p_ref = max(p_ref, _EPSILON)
        p_rec = max(p_rec, _EPSILON)
        total += (p_rec - p_ref) * math.log(p_rec / p_ref)
    return total


def jensen_shannon(
    reference: Sequence[float], recent: Sequence[float], bins: int = 10
) -> float | None:
    """Jensen-Shannon distance (base-2, 0..1) over shared quantile bins."""
    if len(reference) < bins or not recent:
        return None
    edges = _quantile_edges([*reference, *recent], bins)
    p = _histogram(reference, edges)
    q = _histogram(recent, edges)

    def kl(a: list[float], b: list[float]) -> float:
        total = 0.0
        for pa, pb in zip(a, b, strict=True):
            if pa > 0:
                total += pa * math.log2(pa / max(pb, _EPSILON))
        return total

    mixture = [(pa + qa) / 2 for pa, qa in zip(p, q, strict=True)]
    divergence = (kl(p, mixture) + kl(q, mixture)) / 2
    return math.sqrt(max(0.0, min(1.0, divergence)))


def population_shift(
    reference: Sequence[float], recent: Sequence[float]
) -> float | None:
    """Mean shift between samples in pooled-standard-deviation units."""
    if len(reference) < 2 or len(recent) < 2:
        return None
    std_ref = pstdev(reference)
    std_rec = pstdev(recent)
    pooled = math.sqrt((std_ref**2 + std_rec**2) / 2)
    if pooled <= 0:
        return None
    return abs(fmean(recent) - fmean(reference)) / pooled


def lag1_autocorrelation(values: Sequence[float]) -> float | None:
    """Lag-1 autocorrelation — the noise proxy (white noise ~ 0)."""
    if len(values) < 3:
        return None
    mean = fmean(values)
    denominator = sum((v - mean) ** 2 for v in values)
    if denominator <= 0:
        return None
    numerator = sum(
        (values[i] - mean) * (values[i - 1] - mean) for i in range(1, len(values))
    )
    return numerator / denominator
