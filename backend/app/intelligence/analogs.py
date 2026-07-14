"""Historical Analog Engine (Volume 4, Prompt 4.10).

A genuinely different shape from every other Volume 4 component so far:
a search/retrieval engine, not a single blended assessment. For the
current market snapshot, finds the most similar historical snapshots and
reports what actually happened next.

Market state is a 7-dimension vector of already-z-scored NIFTY/D features
(trend at two horizons, acceleration, volatility level x2, position within
the recent high/low range) — z-scores are already standardized and
comparable across time, exactly what similarity search wants, with no
separate normalization step needed. v1 deliberately stays on one
(symbol, timeframe) pair to avoid the cross-timeframe alignment problem
Correlation Intelligence (Prompt 4.8) had to solve; blending in
breadth/sector/macro dimensions is a natural v2 extension.

Of the prompt's four named search methods:
- Cosine Similarity: implemented directly — the per-analog "Similarity"
  this engine stores and ranks by.
- Nearest Neighbor Search: the retrieval mechanism itself (top-K by a
  distance metric) — implemented via Euclidean distance over the same
  z-scored vectors.
- Mahalanobis Distance: a full empirical covariance matrix would need
  matrix inversion, which isn't available without a numerical dependency
  this codebase deliberately avoids elsewhere (see Volume 2's lexicon
  sentiment scorer, Volume 4's own liquidity/volatility v1 heuristics).
  Euclidean distance over pre-standardized (z-scored) features is the
  diagonal-covariance approximation of Mahalanobis distance — a documented
  simplification, not a silent omission.
- Dynamic Time Warping compares *sequences*, not single snapshots -- the v2
  upgrade this docstring used to defer. Implemented now: the analog unit
  gains a second, optional representation, a trailing DTW_WINDOW-bar path
  of state vectors ending at each candidate date (not just the single
  snapshot vector cosine/Euclidean compare), and dtw_distance() finds the
  cheapest-cost alignment between the current path and each historical
  path via classic O(n*m) dynamic programming. Unlike cosine/Euclidean, DTW
  can match two paths that moved similarly but at different local speeds
  (a slow 10-bar runup vs. a fast 6-bar runup that then paused) --
  precisely what a point-in-time vector comparison cannot express. Kept
  fully optional (current_window/historical_windows default to None) so
  every existing call site/test keeps working unchanged; when omitted, DTW
  simply doesn't run, the same graceful-degradation convention as a
  missing feature elsewhere in this engine.

Cosine, Euclidean, and (when window data is supplied) DTW rankings are
computed independently; their top-20 overlap becomes a genuine confidence
signal (do multiple different distance metrics agree on what's similar, or
does the answer depend on which one you ask?) rather than decorative --
this is why all three got implemented instead of just the one the prompt
lists first.

- IntelligenceResult.score      -> bull/bear tilt implied by the analog
                                    set's mean subsequent return
- IntelligenceResult.confidence -> blend of mean analog similarity, the
                                    two methods' top-20 overlap, and pool size
- metrics["analogs"]            -> the top 20, each with Similarity,
                                    Subsequent Returns, Volatility, Maximum
                                    Drawdown, Maximum Run-up
- metrics["win_rate"]           -> Win Rate across the 20 (not a per-analog
                                    field — a single historical instance
                                    doesn't have a "win rate", it either won
                                    or lost)
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from statistics import fmean, pstdev

from app.intelligence.base import (
    Contribution,
    IntelligenceComponent,
    IntelligenceResult,
    clamp,
    normalize_states,
)

COMPONENT = "historical_analogs"

BENCHMARK_TIMEFRAME = "D"
TOP_K = 20
FORWARD_HORIZON_BARS = 20
HISTORY_LIMIT = 5000
# Trailing bars per DTW path -- short enough to stay a "local shape" match
# (the prompt's own framing) rather than drifting into a second, redundant
# long-horizon trend comparison cosine/Euclidean already do at the
# snapshot level.
DTW_WINDOW = 10

STATE_FEATURES: tuple[str, ...] = (
    "price_momentum_20_z",
    "price_momentum_50_z",
    "price_acceleration_20_z",
    "volatility_hist_20_z",
    "volatility_regime_20_z",
    "price_dist_from_high_50_z",
    "price_dist_from_low_50_z",
)

# Cumulative subsequent return that saturates the score's tanh signal, and
# the win-rate distance-from-50% multiplier that separates a clear
# bullish/bearish precedent from a genuinely mixed one — heuristic scales,
# same spirit as elsewhere in this layer.
RETURN_SATURATION = 0.05
WIN_RATE_TILT_SCALE = 2.5


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float | None:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return None
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


def euclidean_distance(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b, strict=True)))


def dtw_distance(path_a: Sequence[Sequence[float]], path_b: Sequence[Sequence[float]]) -> float:
    """Classic Dynamic Time Warping distance between two paths of
    same-dimension vectors -- O(n*m) dynamic programming, pure Python (no
    numpy dependency, same convention as the rest of this codebase's
    feature/intelligence layer). Local cost between two points on the two
    paths is Euclidean distance; the DP finds the cheapest-cost alignment
    that may skip/repeat points on either path to match similarly-shaped
    but differently-paced moves.

    Unlike cosine/Euclidean over a single point-in-time vector, this can
    recognize two paths that moved the same way at different local speeds
    -- exactly what a snapshot comparison structurally cannot express."""
    n, m = len(path_a), len(path_b)
    if n == 0 or m == 0:
        return math.inf
    # Rolling two-row DP (only the previous row is ever needed) rather than
    # a full n x m table -- these paths are short (DTW_WINDOW bars) but this
    # keeps memory flat regardless of window size.
    previous = [math.inf] * (m + 1)
    previous[0] = 0.0
    for i in range(1, n + 1):
        current = [math.inf] * (m + 1)
        for j in range(1, m + 1):
            cost = euclidean_distance(path_a[i - 1], path_b[j - 1])
            current[j] = cost + min(previous[j], current[j - 1], previous[j - 1])
        previous = current
    return previous[m]


def path_outcomes(returns: Sequence[float]) -> dict[str, float] | None:
    """Subsequent return/volatility/drawdown/run-up from a forward daily-
    return path (fractions, e.g. 0.012 for +1.2%, compounded)."""
    if not returns:
        return None
    path = [1.0]
    for r in returns:
        path.append(path[-1] * (1 + r))
    running_max = path[0]
    running_min = path[0]
    max_drawdown = 0.0
    max_runup = 0.0
    for v in path[1:]:
        running_max = max(running_max, v)
        running_min = min(running_min, v)
        max_drawdown = min(max_drawdown, v / running_max - 1)
        max_runup = max(max_runup, v / running_min - 1)
    return {
        "subsequent_return": path[-1] - 1,
        "subsequent_volatility": pstdev(returns) if len(returns) > 1 else 0.0,
        "max_drawdown": max_drawdown,
        "max_runup": max_runup,
    }


@dataclass(frozen=True)
class Analog:
    date: str
    similarity: float
    subsequent_return: float
    subsequent_volatility: float
    max_drawdown: float
    max_runup: float


def assess_historical_analogs(
    current_vector: Sequence[float],
    historical: Sequence[tuple[str, Sequence[float]]],
    outcomes: Mapping[str, Mapping[str, float]],
    top_k: int = TOP_K,
    current_window: Sequence[Sequence[float]] | None = None,
    historical_windows: Mapping[str, Sequence[Sequence[float]]] | None = None,
) -> IntelligenceResult:
    """Pure analog search from a current state vector, a pool of (date,
    vector) historical candidates, and each candidate's precomputed
    forward-looking outcome. current_window/historical_windows are optional
    -- when supplied (a trailing DTW_WINDOW-bar path per date), DTW joins
    cosine/Euclidean as a third independent ranking; when omitted, DTW is
    skipped entirely and behavior is identical to before it existed."""
    contributions: list[Contribution] = []
    reasoning: list[str] = []

    cosine_ranked: list[tuple[str, float]] = []
    euclid_ranked: list[tuple[str, float]] = []
    dtw_ranked: list[tuple[str, float]] = []
    for date, vector in historical:
        if date not in outcomes:
            continue
        cosine = cosine_similarity(current_vector, vector)
        if cosine is not None:
            cosine_ranked.append((date, cosine))
        euclid_ranked.append((date, euclidean_distance(current_vector, vector)))
        if current_window is not None and historical_windows is not None:
            window = historical_windows.get(date)
            if window is not None:
                dtw_ranked.append((date, dtw_distance(current_window, window)))

    cosine_ranked.sort(key=lambda pair: -pair[1])
    euclid_ranked.sort(key=lambda pair: pair[1])
    dtw_ranked.sort(key=lambda pair: pair[1])
    cosine_top = cosine_ranked[:top_k]
    euclid_top_dates = {date for date, _ in euclid_ranked[:top_k]}
    dtw_top_dates = {date for date, _ in dtw_ranked[:top_k]}

    analogs = [
        Analog(
            date=date,
            similarity=round(similarity, 4),
            subsequent_return=round(outcomes[date]["subsequent_return"], 4),
            subsequent_volatility=round(outcomes[date]["subsequent_volatility"], 4),
            max_drawdown=round(outcomes[date]["max_drawdown"], 4),
            max_runup=round(outcomes[date]["max_runup"], 4),
        )
        for date, similarity in cosine_top
    ]

    if not analogs:
        reasoning.append("No historical analogs found; insufficient state-vector history.")
        return IntelligenceResult(
            component=COMPONENT,
            score=50.0,
            confidence=0.1,
            states=normalize_states(
                {"bullish_precedent": 0.0, "bearish_precedent": 0.0, "mixed_precedent": 1.0}
            ),
            metrics={
                "analogs": [],
                "win_rate": None,
                "mean_subsequent_return": None,
                "mean_subsequent_volatility": None,
                "mean_max_drawdown": None,
                "mean_max_runup": None,
                "mean_similarity": None,
                "method_agreement": None,
            },
            contributions=contributions,
            reasoning=reasoning,
        )

    subsequent_returns = [a.subsequent_return for a in analogs]
    win_rate = sum(1 for r in subsequent_returns if r > 0) / len(subsequent_returns)
    mean_subsequent_return = fmean(subsequent_returns)
    mean_volatility = fmean(a.subsequent_volatility for a in analogs)
    mean_max_drawdown = fmean(a.max_drawdown for a in analogs)
    mean_max_runup = fmean(a.max_runup for a in analogs)
    mean_similarity = fmean(a.similarity for a in analogs)

    analog_dates = {a.date for a in analogs}
    euclidean_overlap = len(analog_dates & euclid_top_dates) / len(analogs)
    contributions.append(Contribution(
        feature="cosine_vs_euclidean_overlap", value=euclidean_overlap, weight=0.3,
        effect="methods agree" if euclidean_overlap > 0.5 else "methods diverge",
    ))
    if dtw_ranked:
        dtw_overlap = len(analog_dates & dtw_top_dates) / len(analogs)
        contributions.append(Contribution(
            feature="cosine_vs_dtw_overlap", value=dtw_overlap, weight=0.3,
            effect="methods agree" if dtw_overlap > 0.5 else "methods diverge",
        ))
        # Three independent methods now agreeing is stronger evidence than
        # two -- average rather than letting either single pairwise overlap
        # dominate.
        method_agreement = (euclidean_overlap + dtw_overlap) / 2
    else:
        method_agreement = euclidean_overlap
    contributions.append(Contribution(
        feature="mean_analog_similarity", value=mean_similarity, weight=0.3,
        effect="close analogs" if mean_similarity > 0.5 else "distant analogs",
    ))

    similarity_confidence = clamp((mean_similarity + 1) / 2, 0.0, 1.0)
    pool_completeness = clamp(len(historical) / 100, 0.0, 1.0)
    confidence = clamp(
        0.15 + 0.35 * similarity_confidence + 0.25 * method_agreement
        + 0.25 * pool_completeness,
        0.0, 1.0,
    )

    score = clamp(50 + 50 * math.tanh(mean_subsequent_return / RETURN_SATURATION), 0.0, 100.0)

    tilt = clamp(abs(win_rate - 0.5) * WIN_RATE_TILT_SCALE, 0.0, 1.0)
    states = normalize_states({
        "bullish_precedent": tilt if win_rate > 0.5 else 0.0,
        "bearish_precedent": tilt if win_rate < 0.5 else 0.0,
        "mixed_precedent": 1 - tilt,
    })

    dominant = max(states, key=lambda s: states[s])
    reasoning.extend([
        f"{len(analogs)} analogs found (of {len(historical)} candidates searched); "
        f"mean similarity {mean_similarity:.2f}, method agreement {method_agreement:.0%}"
        + (f" (cosine/Euclidean/DTW, 3-way)." if dtw_ranked else " (cosine/Euclidean, 2-way)."),
        f"Win rate {win_rate:.0%}, mean subsequent return {mean_subsequent_return:+.2%} "
        f"over {FORWARD_HORIZON_BARS} bars.",
        f"Dominant state: {dominant}.",
    ])

    return IntelligenceResult(
        component=COMPONENT,
        score=score,
        confidence=confidence,
        states=states,
        metrics={
            "analogs": [asdict(a) for a in analogs],
            "win_rate": round(win_rate, 4),
            "mean_subsequent_return": round(mean_subsequent_return, 4),
            "mean_subsequent_volatility": round(mean_volatility, 4),
            "mean_max_drawdown": round(mean_max_drawdown, 4),
            "mean_max_runup": round(mean_max_runup, 4),
            "mean_similarity": round(mean_similarity, 4),
            "method_agreement": round(method_agreement, 4),
            "dtw_used": bool(dtw_ranked),
        },
        contributions=contributions,
        reasoning=reasoning,
    )


class HistoricalAnalogEngine(IntelligenceComponent):
    name = "historical_analog_engine"

    async def assess(self, symbol: str | None = None) -> IntelligenceResult:
        symbol = symbol or self._settings.feature_benchmark_symbol
        feature_series: dict[str, dict[str, float]] = {}
        for feature in STATE_FEATURES:
            rows = await self.store.history(
                feature, symbol=symbol, timeframe=BENCHMARK_TIMEFRAME, limit=HISTORY_LIMIT
            )
            feature_series[feature] = {r["ts"]: r["value"] for r in rows if r["value"] is not None}

        return_rows = await self.store.history(
            "price_simple_return", symbol=symbol, timeframe=BENCHMARK_TIMEFRAME,
            limit=HISTORY_LIMIT,
        )
        returns_by_ts = {r["ts"]: r["value"] for r in return_rows if r["value"] is not None}
        sorted_return_ts = sorted(returns_by_ts)
        return_ts_index = {ts: i for i, ts in enumerate(sorted_return_ts)}

        common_ts = sorted(
            ts for ts in feature_series[STATE_FEATURES[0]]
            if all(ts in feature_series[f] for f in STATE_FEATURES)
        )
        if not common_ts:
            return assess_historical_analogs([], [], {})

        current_ts = common_ts[-1]
        current_vector = [feature_series[f][current_ts] for f in STATE_FEATURES]
        common_ts_index = {ts: i for i, ts in enumerate(common_ts)}

        def trailing_window(ts: str) -> Sequence[Sequence[float]] | None:
            """The DTW_WINDOW-bar path of state vectors ending at (and
            including) `ts`, or None if fewer than DTW_WINDOW common bars
            are available yet (too close to the start of history)."""
            i = common_ts_index[ts]
            if i + 1 < DTW_WINDOW:
                return None
            window_ts = common_ts[i - DTW_WINDOW + 1 : i + 1]
            return [[feature_series[f][t] for f in STATE_FEATURES] for t in window_ts]

        current_window = trailing_window(current_ts)

        historical: list[tuple[str, Sequence[float]]] = []
        outcomes: dict[str, dict[str, float]] = {}
        historical_windows: dict[str, Sequence[Sequence[float]]] = {}
        for ts in common_ts[:-1]:
            if ts not in returns_by_ts:
                continue
            position = return_ts_index[ts]
            forward_window = sorted_return_ts[position + 1:position + 1 + FORWARD_HORIZON_BARS]
            forward = [returns_by_ts[t] for t in forward_window]
            if len(forward) < FORWARD_HORIZON_BARS:
                continue  # not enough completed future history yet
            outcome = path_outcomes(forward)
            if outcome is None:
                continue
            outcomes[ts] = outcome
            historical.append((ts, [feature_series[f][ts] for f in STATE_FEATURES]))
            window = trailing_window(ts)
            if window is not None:
                historical_windows[ts] = window

        result = assess_historical_analogs(
            current_vector, historical, outcomes,
            current_window=current_window if current_window is not None else None,
            historical_windows=historical_windows or None,
        )
        result.metrics["symbol"] = symbol
        result.metrics["as_of"] = current_ts
        await self._publish_assessment(symbol, result)
        return result
