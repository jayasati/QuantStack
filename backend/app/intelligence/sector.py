"""Sector Intelligence Engine (Volume 4, Prompt 4.4).

Unlike Trend/Volatility/Breadth Intelligence, this component analyzes every
sector simultaneously (a cross-sectional read), consuming the sector feature
engine's per-sector output (relative strength, momentum, capital rotation,
heat score, cross-sectional leadership z-score, rank) plus the market-wide
rotation index and participation %.

Of the four named outputs, two map onto the base IntelligenceResult
contract's existing fields and two are metrics:
- IntelligenceResult.score       -> Sector Trend Score (50-centered bull/bear,
                                     same convention as Trend/Breadth Intelligence)
- IntelligenceResult.confidence  -> Sector Leadership Confidence
- metrics["sector_rotation_score"] -> Sector Rotation Score (0-100 intensity)
- metrics["sector_heat_score"]     -> Sector Heat Score (0-100, mean of sectors)
plus Leading/Lagging Sectors, Capital Rotation, Leadership Change, and each
sector's Relative Momentum, all in metrics for full explainability.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import pstdev

from app.intelligence.base import (
    Contribution,
    IntelligenceComponent,
    IntelligenceResult,
    clamp,
    normalize_states,
)

COMPONENT = "sector"

SECTOR_TIMEFRAME = "sector"
MARKET_SYMBOL = "SECTORS"

# Must match app.collectors.sources.broker_sectors.SECTOR_TOKENS' keys — the
# canonical list of sectors this system tracks. Duplicated here (rather than
# imported) because intelligence components consume the Feature Store only
# and never touch collectors, per base.py's contract.
SECTOR_UNIVERSE: tuple[str, ...] = (
    "Banking", "IT", "Auto", "Energy", "Pharma", "FMCG", "PSU",
    "PSU Bank", "Private Bank", "Realty", "Metal", "Infrastructure",
)

LEADER_COUNT = 3
# Cross-snapshot z-score move that counts as a genuine leadership change,
# beyond a plain sign flip — a heuristic scale, same spirit as the momentum
# saturation scales elsewhere in this layer.
LEADERSHIP_CHANGE_THRESHOLD = 1.0
RELATIVE_STRENGTH_SATURATION = 5.0  # % points that saturate the trend signal
MOMENTUM_SATURATION = 5.0  # % points that saturate the trend signal


@dataclass(frozen=True)
class _SectorRow:
    name: str
    heat: float | None
    leadership: float | None
    relative_strength: float | None
    momentum: float | None
    capital_rotation: float | None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _rows_from_features(
    per_sector_features: Mapping[str, Mapping[str, float]],
) -> list[_SectorRow]:
    return [
        _SectorRow(
            name=name,
            heat=feats.get("sector_heat_score"),
            leadership=feats.get("sector_leadership"),
            relative_strength=feats.get("sector_relative_strength"),
            momentum=feats.get("sector_momentum"),
            capital_rotation=feats.get("sector_capital_rotation"),
        )
        for name, feats in per_sector_features.items()
    ]


def assess_sectors(
    per_sector_features: Mapping[str, Mapping[str, float]],
    market_features: Mapping[str, float],
    previous_leadership: Mapping[str, float] | None = None,
) -> IntelligenceResult:
    """Pure cross-sectional sector assessment from the latest feature values.

    ``per_sector_features`` maps sector name -> its latest feature values;
    ``market_features`` are the market-wide (symbol SECTORS) features;
    ``previous_leadership`` maps sector name -> its sector_leadership z-score
    one snapshot back, for detecting leadership change.
    """
    previous_leadership = previous_leadership or {}
    contributions: list[Contribution] = []
    reasoning: list[str] = []

    rows = _rows_from_features(per_sector_features)

    ranked = sorted(
        (r for r in rows if r.heat is not None), key=lambda r: -(r.heat or 0.0)
    )
    leader_count = min(LEADER_COUNT, len(ranked) // 2)
    leading_sectors = [r.name for r in ranked[:leader_count]]
    lagging_sectors = [r.name for r in ranked[-leader_count:]] if leader_count else []
    for r in ranked[:leader_count]:
        contributions.append(Contribution(
            feature=f"sector_heat_score[{r.name}]", value=r.heat, weight=0.15,
            effect="leading",
        ))
    for r in (ranked[-leader_count:] if leader_count else []):
        contributions.append(Contribution(
            feature=f"sector_heat_score[{r.name}]", value=r.heat, weight=0.15,
            effect="lagging",
        ))

    capital_values = [r.capital_rotation for r in rows if r.capital_rotation is not None]
    capital_rotation_intensity = (
        clamp(_mean([abs(v) for v in capital_values]) or 0.0, 0.0, 100.0)
        if capital_values else None
    )

    rotation_index = market_features.get("sector_rotation_index")
    rotation_index_scaled = (
        clamp(rotation_index * 5, 0.0, 100.0) if rotation_index is not None else None
    )
    if rotation_index is not None and rotation_index_scaled is not None:
        contributions.append(Contribution(
            feature="sector_rotation_index", value=rotation_index, weight=0.2,
            effect="active rotation" if rotation_index_scaled > 50 else "quiet rotation",
        ))

    rotation_terms = [
        v for v in (capital_rotation_intensity, rotation_index_scaled) if v is not None
    ]
    sector_rotation_score = _mean(rotation_terms) if rotation_terms else 0.0
    sector_rotation_score = sector_rotation_score or 0.0

    heat_values = [r.heat for r in rows if r.heat is not None]
    sector_heat_score = _mean(heat_values)

    relative_momentum = {r.name: r.momentum for r in rows if r.momentum is not None}

    # Leadership change: a sector whose cross-sectional leadership z-score
    # flipped sign, or moved by more than the threshold, since last snapshot.
    leadership_changes = []
    for r in rows:
        previous = previous_leadership.get(r.name)
        if r.leadership is None or previous is None:
            continue
        flipped = (r.leadership > 0) != (previous > 0)
        moved = abs(r.leadership - previous) >= LEADERSHIP_CHANGE_THRESHOLD
        if flipped or moved:
            leadership_changes.append(r.name)
    compared_count = sum(
        1 for r in rows if r.leadership is not None and r.name in previous_leadership
    )
    change_fraction = len(leadership_changes) / compared_count if compared_count else 0.0

    # Sector Trend Score: bull/bear composite from participation % and mean
    # relative strength/momentum across the universe — same 50-centered
    # convention as Trend and Breadth Intelligence.
    participation_pct = market_features.get("sector_participation_pct")
    mean_relative_strength = _mean(
        [r.relative_strength for r in rows if r.relative_strength is not None]
    )
    mean_momentum = _mean(list(relative_momentum.values()))

    level_terms: list[tuple[float, float]] = []
    if participation_pct is not None:
        level_terms.append((clamp((participation_pct - 50) / 50, -1.0, 1.0), 0.40))
        contributions.append(Contribution(
            feature="sector_participation_pct", value=participation_pct, weight=0.40,
            effect=(
                "broadly outperforming" if participation_pct > 50
                else "broadly underperforming"
            ),
        ))
    if mean_relative_strength is not None:
        signal = math.tanh(mean_relative_strength / RELATIVE_STRENGTH_SATURATION)
        level_terms.append((signal, 0.35))
        contributions.append(Contribution(
            feature="sector_relative_strength_mean", value=mean_relative_strength,
            weight=0.35,
            effect="ahead of benchmark" if mean_relative_strength > 0 else "behind benchmark",
        ))
    if mean_momentum is not None:
        signal = math.tanh(mean_momentum / MOMENTUM_SATURATION)
        level_terms.append((signal, 0.25))
        contributions.append(Contribution(
            feature="sector_momentum_mean", value=mean_momentum, weight=0.25,
            effect="accelerating" if mean_momentum > 0 else "decelerating",
        ))
    total_weight = sum(w for _, w in level_terms)
    level = sum(v * w for v, w in level_terms) / total_weight if total_weight > 0 else 0.0
    level = clamp(level, -1.0, 1.0)

    # Sector Leadership Confidence: how much of the universe reported data,
    # how clearly separated the leaders are from the pack (leadership
    # z-score spread), and how stable that ordering has been.
    universe_size = max(len(per_sector_features), len(SECTOR_UNIVERSE))
    data_completeness = len(ranked) / universe_size if universe_size else 0.0
    leadership_zs = [r.leadership for r in rows if r.leadership is not None]
    separation = (
        clamp(pstdev(leadership_zs) / 2.0, 0.0, 1.0) if len(leadership_zs) > 1 else 0.0
    )
    stability = 1 - clamp(change_fraction, 0.0, 1.0) if compared_count else 0.5
    confidence = clamp(
        0.2 + 0.3 * data_completeness + 0.25 * separation + 0.25 * stability, 0.0, 1.0
    )

    rotation_signal = clamp(sector_rotation_score / 100, 0.0, 1.0)
    broad_signal = (
        clamp(participation_pct / 100, 0.0, 1.0) if participation_pct is not None else 0.5
    )
    change_signal = clamp(change_fraction, 0.0, 1.0)
    states = normalize_states({
        "broad_rotation": rotation_signal * broad_signal,
        "narrow_leadership": rotation_signal * (1 - broad_signal),
        "leadership_shift": change_signal,
        "sector_neutral": 1 - rotation_signal,
    })

    score = clamp(50 + 50 * level)
    dominant = max(states, key=lambda s: states[s]) if states else "unknown"
    reasoning.extend([
        f"{len(ranked)}/{universe_size} sectors reporting; leaders "
        f"{leading_sectors or 'none'}, laggards {lagging_sectors or 'none'}.",
        f"Rotation score {sector_rotation_score:.0f}/100, "
        f"{len(leadership_changes)} sector(s) changed leadership since last snapshot.",
        f"Dominant state: {dominant}.",
    ])

    return IntelligenceResult(
        component=COMPONENT,
        score=score,
        confidence=confidence,
        states=states,
        metrics={
            "leading_sectors": leading_sectors,
            "lagging_sectors": lagging_sectors,
            "sector_rotation_score": round(sector_rotation_score, 4),
            "sector_heat_score": (
                round(sector_heat_score, 4) if sector_heat_score is not None else None
            ),
            "capital_rotation_intensity": (
                round(capital_rotation_intensity, 4)
                if capital_rotation_intensity is not None else None
            ),
            "relative_momentum": {k: round(v, 4) for k, v in relative_momentum.items()},
            "leadership_changes": leadership_changes,
            "sector_trend_level": round(level, 4),
        },
        contributions=contributions,
        reasoning=reasoning,
    )


class SectorIntelligenceEngine(IntelligenceComponent):
    name = "sector_intelligence"

    async def assess(self, sectors: Sequence[str] = SECTOR_UNIVERSE) -> IntelligenceResult:
        per_sector_features: dict[str, dict[str, float]] = {}
        previous_leadership: dict[str, float] = {}
        for sector in sectors:
            per_sector_features[sector] = await self.latest_values(sector, SECTOR_TIMEFRAME)
            history = await self.feature_history(
                "sector_leadership", sector, SECTOR_TIMEFRAME, limit=2
            )
            if len(history) >= 2:
                previous_leadership[sector] = history[0]
        market_features = await self.latest_values(MARKET_SYMBOL, SECTOR_TIMEFRAME)
        return assess_sectors(per_sector_features, market_features, previous_leadership)
