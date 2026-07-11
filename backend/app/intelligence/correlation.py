"""Cross-Asset Correlation Engine (Volume 4, Prompt 4.8).

Analyzes relationships among eight assets: Nifty, Bank Nifty, USDINR, Crude
Oil, Gold, US Markets, Global Indices, and Sector Indices. The last three
are aggregates (US Markets -> SPX; Global Indices -> mean of Nikkei/Hang
Seng/DAX; Sector Indices -> mean across the sector universe) rather than
individual tickers, matching how the prompt itself names them as categories.

The three source layers run on genuinely different native cadences — daily
price bars, ~5-minute macro collector runs, ~60s sector collector runs — so
naively correlating their raw histories positionally would misalign dates.
Every constituent series is downsampled to one value per IST calendar date
(the most recent observation of that day) before any correlation math runs.

- IntelligenceResult.score      -> Risk Concentration Score (0-100; high
                                    mean |correlation| = a single shock moves
                                    everything together, low diversification
                                    benefit)
- IntelligenceResult.confidence -> blend of asset/pair data completeness and
                                    Correlation Stability
- metrics["correlation_matrix"] -> Rolling Correlation Matrix (short window)
- metrics["correlation_breakdown"] -> pairs whose short vs. long window
                                    correlation diverged past a threshold
- metrics["correlation_stability"] -> Correlation Stability (0-1)

States (Chapter 4 has no dedicated Correlation dimension, so this defines
its own, same as Breadth/Sector/Institutional Flow): highly_correlated,
decorrelated, correlation_breakdown. The first two are already diluted by
the same `stability` factor breakdown's complement uses, so — unlike
Breadth/Institutional Flow/Liquidity's compression-style overlays — no
extra halving is needed here; the partition is symmetric by construction.
"""

import itertools
from collections.abc import Mapping, Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from app.features.normalize import rolling_correlation
from app.intelligence.base import (
    Contribution,
    IntelligenceComponent,
    IntelligenceResult,
    clamp,
    normalize_states,
)
from app.intelligence.sector import SECTOR_UNIVERSE

COMPONENT = "correlation"

IST = ZoneInfo("Asia/Kolkata")

SHORT_WINDOW = 20
LONG_WINDOW = 60
# Correlation-point swing between the short and long window that saturates
# the instability read for a pair, and the swing that counts as an outright
# breakdown — heuristic scales, same spirit as elsewhere in this layer.
STABILITY_SATURATION = 0.5
BREAKDOWN_THRESHOLD = 0.4
# Raw rows fetched per constituent series before day-bucketing. Generous
# because sector/macro sources run far more often than once a day (v1
# pragmatic choice — a real cadence-aware fetch can replace this later).
HISTORY_LIMIT = 20000

# Each asset's constituent (feature_name, symbol, timeframe) triples,
# averaged together day-by-day. US Markets/Global Indices/Sector Indices
# are aggregates because the prompt names them as categories, not tickers.
ASSET_SOURCES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "NIFTY": (("price_simple_return", "NIFTY", "D"),),
    "BANKNIFTY": (("price_simple_return", "BANKNIFTY", "D"),),
    "USDINR": (("macro_return_1d_pct", "USDINR", "macro"),),
    "CRUDE": (("macro_return_1d_pct", "CRUDE", "macro"),),
    "GOLD": (("macro_return_1d_pct", "GOLD", "macro"),),
    "US_MARKETS": (("macro_return_1d_pct", "SPX", "macro"),),
    "GLOBAL_INDICES": (
        ("macro_return_1d_pct", "NIKKEI", "macro"),
        ("macro_return_1d_pct", "HANGSENG", "macro"),
        ("macro_return_1d_pct", "DAX", "macro"),
    ),
    "SECTOR_INDICES": tuple(
        ("sector_relative_strength", sector, "sector") for sector in SECTOR_UNIVERSE
    ),
}
ASSET_UNIVERSE: tuple[str, ...] = tuple(ASSET_SOURCES)


def _ist_date(ts_iso: str) -> str:
    dt = datetime.fromisoformat(ts_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(IST).date().isoformat()


def daily_series(rows: Sequence[dict]) -> dict[str, float]:
    """Downsample newest-first (ts, value) rows to one value per IST date —
    the first row seen for a date is its most recent observation."""
    daily: dict[str, float] = {}
    for row in rows:
        if row.get("value") is None or row.get("ts") is None:
            continue
        date = _ist_date(row["ts"])
        if date not in daily:
            daily[date] = float(row["value"])
    return daily


def average_daily_series(series_list: Sequence[Mapping[str, float]]) -> dict[str, float]:
    """Mean of several per-date series, date-by-date, over whichever
    constituents have data for that date."""
    all_dates: set[str] = set()
    for series in series_list:
        all_dates |= series.keys()
    result: dict[str, float] = {}
    for date in all_dates:
        values = [series[date] for series in series_list if date in series]
        if values:
            result[date] = sum(values) / len(values)
    return result


def _latest_value(series: list[float | None]) -> float | None:
    return next((v for v in reversed(series) if v is not None), None)


def assess_correlations(
    asset_daily_series: Mapping[str, Mapping[str, float]],
) -> IntelligenceResult:
    """Pure correlation assessment from per-asset {date: return} series."""
    contributions: list[Contribution] = []
    reasoning: list[str] = []

    assets = [a for a in ASSET_UNIVERSE if asset_daily_series.get(a)]
    all_dates = sorted({d for a in assets for d in asset_daily_series[a]})
    aligned: dict[str, list[float | None]] = {
        a: [asset_daily_series[a].get(d) for d in all_dates] for a in assets
    }

    matrix: dict[str, dict[str, float]] = {a: {a: 1.0} for a in assets}
    short_corr: dict[tuple[str, str], float] = {}
    long_corr: dict[tuple[str, str], float] = {}
    for a, b in itertools.combinations(assets, 2):
        short_value = _latest_value(rolling_correlation(aligned[a], aligned[b], SHORT_WINDOW))
        long_value = _latest_value(rolling_correlation(aligned[a], aligned[b], LONG_WINDOW))
        if short_value is not None:
            matrix[a][b] = round(short_value, 4)
            matrix[b][a] = round(short_value, 4)
            short_corr[(a, b)] = short_value
        if long_value is not None:
            long_corr[(a, b)] = long_value

    stability_values = []
    breakdown_pairs = []
    for pair, short_value in short_corr.items():
        long_value = long_corr.get(pair)
        if long_value is None:
            continue
        diff = abs(short_value - long_value)
        stability_values.append(1 - clamp(diff / STABILITY_SATURATION, 0.0, 1.0))
        if diff >= BREAKDOWN_THRESHOLD:
            breakdown_pairs.append(f"{pair[0]}-{pair[1]}")
    # 0.0 (not a "neutral" default) when windows can't be compared at all —
    # absence of evidence that correlations are stable isn't evidence they are.
    stability = sum(stability_values) / len(stability_values) if stability_values else 0.0

    mean_abs_corr = (
        sum(abs(v) for v in short_corr.values()) / len(short_corr) if short_corr else 0.0
    )
    risk_concentration_score = clamp(100 * mean_abs_corr, 0.0, 100.0)

    if short_corr:
        strongest = max(short_corr.items(), key=lambda kv: kv[1])
        weakest = min(short_corr.items(), key=lambda kv: kv[1])
        contributions.append(Contribution(
            feature=f"correlation[{strongest[0][0]}-{strongest[0][1]}]",
            value=strongest[1], weight=0.2, effect="most correlated pair",
        ))
        contributions.append(Contribution(
            feature=f"correlation[{weakest[0][0]}-{weakest[0][1]}]",
            value=weakest[1], weight=0.2, effect="least correlated pair",
        ))
    for pair_name in breakdown_pairs:
        contributions.append(Contribution(
            feature=f"correlation_breakdown[{pair_name}]", value=None, weight=0.15,
            effect="correlation structure shifting",
        ))

    total_pairs = len(ASSET_UNIVERSE) * (len(ASSET_UNIVERSE) - 1) // 2
    data_completeness = len(assets) / len(ASSET_UNIVERSE)
    pair_completeness = len(short_corr) / total_pairs if total_pairs else 0.0
    confidence = clamp(
        0.2 + 0.3 * data_completeness + 0.2 * pair_completeness + 0.3 * stability,
        0.0, 1.0,
    )

    states = normalize_states({
        "highly_correlated": mean_abs_corr * stability,
        "decorrelated": (1 - mean_abs_corr) * stability,
        "correlation_breakdown": 1 - stability,
    })

    dominant = max(states, key=lambda s: states[s]) if states else "unknown"
    reasoning.extend([
        f"{len(assets)}/{len(ASSET_UNIVERSE)} assets reporting, {len(short_corr)}/{total_pairs} "
        f"pairs correlated; mean |correlation| {mean_abs_corr:.2f}.",
        f"Stability {stability:.0%}, {len(breakdown_pairs)} pair(s) breaking down"
        + (f": {', '.join(breakdown_pairs)}." if breakdown_pairs else "."),
        f"Dominant state: {dominant}.",
    ])

    return IntelligenceResult(
        component=COMPONENT,
        score=risk_concentration_score,
        confidence=confidence,
        states=states,
        metrics={
            "correlation_matrix": matrix,
            "correlation_stability": round(stability, 4),
            "correlation_breakdown": breakdown_pairs,
            "risk_concentration_score": round(risk_concentration_score, 4),
            "mean_absolute_correlation": round(mean_abs_corr, 4),
        },
        contributions=contributions,
        reasoning=reasoning,
    )


class CorrelationIntelligenceEngine(IntelligenceComponent):
    name = "correlation_intelligence"

    async def assess(self) -> IntelligenceResult:
        asset_daily_series: dict[str, dict[str, float]] = {}
        for asset, sources in ASSET_SOURCES.items():
            constituents = []
            for feature_name, symbol, timeframe in sources:
                rows = await self.store.history(
                    feature_name, symbol=symbol, timeframe=timeframe, limit=HISTORY_LIMIT
                )
                constituents.append(daily_series(rows))
            asset_daily_series[asset] = average_daily_series(constituents)
        result = assess_correlations(asset_daily_series)
        await self._publish_assessment(None, result)
        return result
