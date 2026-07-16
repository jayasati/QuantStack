"""Feature Selection Engine (Volume 3, Prompt 3.16).

Selects the strongest predictors of the symbol's next-bar log return from the
stored feature matrix — systematically, not by feeding hundreds of weak
features into every model. Pure Python (the stack carries no numpy/sklearn):

- Variance threshold: near-constant features are dropped outright.
- Correlation filtering: pairwise |Pearson| above the threshold marks the
  weaker feature (by mutual information) redundant; all offending pairs are
  reported.
- Mutual information: quantile-binned MI between each feature and the
  binned forward return, in nats.
- Model-based importances on the top candidates via a small ridge regression
  (standardized features, normal equations): permutation importance
  (MSE increase under a deterministic within-column shuffle) and exact
  linear-SHAP importance (mean |coef x (x - mean)|).
- Recursive feature elimination: backward elimination dropping the smallest
  standardized coefficient each round; the drop order is the RFE rank.

The composite ranking averages the per-method ranks. The recommended set is
persisted into feature_usage (consumer "feature_selection") so downstream
modules can read which features are currently trusted.
"""

import asyncio
import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from statistics import fmean, pstdev
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.features.normalize import NORMALIZED_SUFFIX

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession] | async_sessionmaker[AsyncSession]

MATRIX_BARS = 250
MIN_COVERAGE = 0.8
MIN_ROWS = 60
VARIANCE_EPSILON = 1e-9
CORRELATION_THRESHOLD = 0.95
MODEL_CANDIDATES = 20
RIDGE_LAMBDA = 1.0
MI_FEATURE_BINS = 5
MI_TARGET_BINS = 3
PERMUTATION_SEED = 42


@dataclass(frozen=True)
class SelectionReport:
    symbol: str
    timeframe: str
    rows: int
    recommended: list[str]
    ranking: list[dict[str, Any]]
    redundant: list[str]
    correlated_pairs: list[dict[str, Any]]
    dropped_low_variance: list[str] = field(default_factory=list)


# --- pure math helpers ---------------------------------------------------------


def _standardize(column: Sequence[float]) -> list[float] | None:
    mean = fmean(column)
    std = pstdev(column)
    if std <= VARIANCE_EPSILON:
        return None
    return [(v - mean) / std for v in column]


def _pearson(a: Sequence[float], b: Sequence[float]) -> float:
    mean_a, mean_b = fmean(a), fmean(b)
    var_a = fmean([(x - mean_a) ** 2 for x in a])
    var_b = fmean([(x - mean_b) ** 2 for x in b])
    if var_a <= 0 or var_b <= 0:
        return 0.0
    cov = fmean([(x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True)])
    return max(-1.0, min(1.0, cov / math.sqrt(var_a * var_b)))


def _quantile_bins(values: Sequence[float], bins: int) -> list[int]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    assignment = [0] * len(values)
    for position, index in enumerate(order):
        assignment[index] = min(bins - 1, position * bins // len(values))
    return assignment


def mutual_information(
    feature: Sequence[float], target: Sequence[float],
    feature_bins: int = MI_FEATURE_BINS, target_bins: int = MI_TARGET_BINS,
) -> float:
    """MI (nats) between quantile-binned feature and target."""
    n = len(feature)
    fb = _quantile_bins(feature, feature_bins)
    tb = _quantile_bins(target, target_bins)
    joint: dict[tuple[int, int], int] = {}
    f_counts = [0] * feature_bins
    t_counts = [0] * target_bins
    for fi, ti in zip(fb, tb, strict=True):
        joint[(fi, ti)] = joint.get((fi, ti), 0) + 1
        f_counts[fi] += 1
        t_counts[ti] += 1
    mi = 0.0
    for (fi, ti), count in joint.items():
        p_joint = count / n
        p_independent = (f_counts[fi] / n) * (t_counts[ti] / n)
        if p_joint > 0 and p_independent > 0:
            mi += p_joint * math.log(p_joint / p_independent)
    return max(0.0, mi)


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting for small dense systems."""
    n = len(rhs)
    augmented = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(augmented[r][col]))
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        pivot_value = augmented[col][col]
        if abs(pivot_value) < 1e-12:
            continue
        for r in range(n):
            if r != col:
                factor = augmented[r][col] / pivot_value
                for c in range(col, n + 1):
                    augmented[r][c] -= factor * augmented[col][c]
    solution = []
    for i in range(n):
        pivot_value = augmented[i][i]
        solution.append(augmented[i][n] / pivot_value if abs(pivot_value) > 1e-12 else 0.0)
    return solution


def ridge_fit(columns: list[list[float]], target: list[float],
              lam: float = RIDGE_LAMBDA) -> list[float]:
    """Ridge coefficients for standardized columns (no intercept)."""
    k = len(columns)
    n = len(target)
    gram = [
        [sum(columns[a][i] * columns[b][i] for i in range(n)) / n for b in range(k)]
        for a in range(k)
    ]
    for a in range(k):
        gram[a][a] += lam / n
    rhs = [sum(columns[a][i] * target[i] for i in range(n)) / n for a in range(k)]
    return _solve(gram, rhs)


def _mse(columns: list[list[float]], coefs: list[float], target: list[float]) -> float:
    n = len(target)
    total = 0.0
    for i in range(n):
        prediction = sum(coef * column[i] for coef, column in zip(coefs, columns, strict=True))
        total += (target[i] - prediction) ** 2
    return total / n


def permutation_importance(
    columns: list[list[float]], coefs: list[float], target: list[float],
    seed: int = PERMUTATION_SEED,
) -> list[float]:
    """MSE increase when each column is shuffled (model held fixed)."""
    baseline = _mse(columns, coefs, target)
    importances = []
    for index in range(len(columns)):
        shuffled = columns[index][:]
        random.Random(seed + index).shuffle(shuffled)
        trial = columns[:index] + [shuffled] + columns[index + 1 :]
        importances.append(max(0.0, _mse(trial, coefs, target) - baseline))
    return importances


def linear_shap_importance(columns: list[list[float]], coefs: list[float]) -> list[float]:
    """Exact mean |SHAP| for a linear model: |coef| x mean|x - mean(x)|."""
    importances = []
    for column, coef in zip(columns, coefs, strict=True):
        mean = fmean(column)
        importances.append(abs(coef) * fmean([abs(v - mean) for v in column]))
    return importances


def rfe_ranks(columns: list[list[float]], target: list[float]) -> list[int]:
    """Backward elimination order: rank 1 = survives longest."""
    remaining = list(range(len(columns)))
    ranks = [0] * len(columns)
    next_rank = len(columns)
    while remaining:
        coefs = ridge_fit([columns[i] for i in remaining], target)
        weakest_position = min(range(len(remaining)), key=lambda p: abs(coefs[p]))
        ranks[remaining.pop(weakest_position)] = next_rank
        next_rank -= 1
    return ranks


# --- selection ---------------------------------------------------------------------


def select_features(
    matrix: dict[str, list[float]],
    target: list[float],
    max_features: int = 10,
    correlation_threshold: float = CORRELATION_THRESHOLD,
) -> dict[str, Any]:
    """Pure selection over an aligned feature matrix and target vector."""
    dropped_low_variance: list[str] = []
    standardized: dict[str, list[float]] = {}
    for name, column in matrix.items():
        scaled = _standardize(column)
        if scaled is None:
            dropped_low_variance.append(name)
        else:
            standardized[name] = scaled

    mi_scores = {
        name: mutual_information(column, target)
        for name, column in standardized.items()
    }

    correlated_pairs: list[dict[str, Any]] = []
    redundant: set[str] = set()
    names = sorted(standardized, key=lambda n: -mi_scores[n])
    # `candidates` below is the first MODEL_CANDIDATES *names* entries not in
    # `redundant`, in this same MI-descending order -- once that many
    # survivors have been seen, nothing later in `names` can ever enter
    # `candidates` regardless of its redundancy status, so the O(len(names)^2)
    # pairwise-correlation scan can stop. At live scale (781 stored features
    # for one symbol/D group) the unbounded version measured 12.5s of real
    # CPU per symbol; this cut is exact for recommended/ranking (the only
    # fields the ridge/permutation/RFE stage and the final output consume) --
    # it only makes `redundant`/`correlated_pairs` a partial view (pairs
    # among the never-reached low-MI tail go unreported), which matches
    # their existing bounded nature (they were never a full pairwise audit).
    survivors = 0
    for a_index in range(len(names)):
        if survivors >= MODEL_CANDIDATES:
            break
        first = names[a_index]
        if first in redundant:
            continue
        survivors += 1
        if survivors >= MODEL_CANDIDATES:
            break
        for second in names[a_index + 1 :]:
            if second in redundant:
                continue
            correlation = _pearson(standardized[first], standardized[second])
            if abs(correlation) >= correlation_threshold:
                redundant.add(second)  # keep the higher-MI member of the pair
                correlated_pairs.append(
                    {"keep": first, "drop": second, "correlation": round(correlation, 4)}
                )

    candidates = [n for n in names if n not in redundant][:MODEL_CANDIDATES]
    ranking: list[dict[str, Any]] = []
    if candidates:
        columns = [standardized[n] for n in candidates]
        coefs = ridge_fit(columns, target)
        permutation = permutation_importance(columns, coefs, target)
        shap = linear_shap_importance(columns, coefs)
        rfe = rfe_ranks(columns, target)

        def ranks_of(scores: list[float], reverse: bool = True) -> list[int]:
            order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=reverse)
            out = [0] * len(scores)
            for position, index in enumerate(order, start=1):
                out[index] = position
            return out

        mi_rank = ranks_of([mi_scores[n] for n in candidates])
        perm_rank = ranks_of(permutation)
        shap_rank = ranks_of(shap)
        for i, name in enumerate(candidates):
            composite = fmean([mi_rank[i], perm_rank[i], shap_rank[i], rfe[i]])
            ranking.append({
                "feature_name": name,
                "mutual_information": round(mi_scores[name], 6),
                "permutation_importance": round(permutation[i], 8),
                "shap_importance": round(shap[i], 8),
                "rfe_rank": rfe[i],
                "composite_rank": round(composite, 2),
            })
        ranking.sort(key=lambda entry: entry["composite_rank"])

    return {
        "recommended": [entry["feature_name"] for entry in ranking[:max_features]],
        "ranking": ranking,
        "redundant": sorted(redundant),
        "correlated_pairs": correlated_pairs,
        "dropped_low_variance": sorted(dropped_low_variance),
    }


class FeatureSelectionEngine:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._sessions = session_factory

    async def select(
        self, symbol: str, timeframe: str = "D", max_features: int = 10
    ) -> SelectionReport:
        matrix, target, rows = await self._build_matrix(symbol, timeframe)
        if rows < MIN_ROWS:
            return SelectionReport(symbol, timeframe, rows, [], [], [], [])
        # CPU-bound (correlation/MI/ridge/permutation/RFE over the full
        # stored feature set before truncating to MODEL_CANDIDATES) --
        # measured 0.7-2.7s against real live data even after the O(n^2)
        # early-exit fix below, so this must not run directly on the event
        # loop (I-4), same convention as trend/volatility/correlation/analogs.
        outcome = await asyncio.to_thread(select_features, matrix, target, max_features)
        report = SelectionReport(
            symbol=symbol,
            timeframe=timeframe,
            rows=rows,
            recommended=outcome["recommended"],
            ranking=outcome["ranking"],
            redundant=outcome["redundant"],
            correlated_pairs=outcome["correlated_pairs"],
            dropped_low_variance=outcome["dropped_low_variance"],
        )
        await self._persist_usage(report)
        return report

    async def _build_matrix(
        self, symbol: str, timeframe: str
    ) -> tuple[dict[str, list[float]], list[float], int]:
        """Aligned (feature matrix, forward-return target) on daily bars."""
        from app.features.quality import FeatureQualityEngine

        quality = FeatureQualityEngine(self._sessions)
        series_map = await quality._load_group(symbol, timeframe, include_normalized=False)
        forward_returns = await quality._forward_returns(symbol)
        if not series_map or not forward_returns:
            return {}, [], 0

        target_timestamps = sorted(forward_returns)[-MATRIX_BARS:]
        index = {ts: i for i, ts in enumerate(target_timestamps)}
        n = len(target_timestamps)
        aligned: dict[str, list[float]] = {}
        for name, observations in series_map.items():
            if name.endswith(NORMALIZED_SUFFIX):
                continue
            column: list[float | None] = [None] * n
            for ts, value in observations:
                position = index.get(ts)
                if position is not None:
                    column[position] = value
            coverage = sum(1 for v in column if v is not None) / n
            if coverage < MIN_COVERAGE:
                continue
            filled: list[float] = []
            last: float | None = None
            usable = True
            for v in column:
                if v is not None:
                    last = v
                elif last is None:
                    usable = False
                    break
                filled.append(last if last is not None else 0.0)
            if usable:
                aligned[name] = filled
        target = [forward_returns[ts] for ts in target_timestamps]
        return aligned, target, n

    async def _persist_usage(self, report: SelectionReport) -> None:
        """Record the recommended set in feature_usage for downstream readers."""
        if not report.recommended:
            return
        from app.database.tables import FeatureUsageRow

        rows = [
            {
                "feature_name": name,
                "consumer": "feature_selection",
                "symbol": report.symbol,
                "timeframe": report.timeframe,
                "data": {"rank": rank},
            }
            for rank, name in enumerate(report.recommended, start=1)
        ]
        async with self._sessions() as session:
            statement = pg_insert(FeatureUsageRow).values(rows)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["feature_name", "consumer", "symbol", "timeframe"],
                    set_={"data": statement.excluded.data},
                )
            )
            await session.commit()
