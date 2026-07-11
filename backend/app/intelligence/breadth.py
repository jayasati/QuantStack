"""Breadth Intelligence Engine (Volume 4, Prompt 4.3).

Consumes the breadth feature snapshot (breadth strength, participation %,
% of the universe above key EMAs, equal-weight/cap-weight divergence, the
collector's own 0-100 health composite) plus the momentum ladder over those
same signals (breadth-strength slope, advance-decline-line slope, and
52-week new-high/new-low count slopes — the feature store's stand-in for
raw "New Highs"/"New Lows" levels, which only exist as snapshot-time
momentum here, never as standalone stored features).

Outputs a bull/bear Breadth Composite Score (50 = neutral, matching Trend
Intelligence's convention), a 0-1 Participation Quality (broad vs. narrow),
Breadth Confidence, and a probabilistic Breadth Regime.

Breadth Regime is deliberately scoped to what breadth-only data can justify:
"broad_participation" / "narrow_participation" / "improving_breadth" /
"deteriorating_breadth" / "mixed". Chapter 4's Participation taxonomy also
lists Institutional Accumulation / Distribution / Retail Driven — those
require FII/DII/block-deal data and belong to Prompt 4.5's Institutional
Flow Intelligence; inventing them here from breadth-only proxies would put
a confident-looking label on data that doesn't actually measure it.
"""

import math
from collections.abc import Mapping

from app.intelligence.base import (
    Contribution,
    IntelligenceComponent,
    IntelligenceResult,
    clamp,
    normalize_states,
)

COMPONENT = "breadth"

MARKET_SYMBOL = "MARKET"
BREADTH_TIMEFRAME = "breadth"

MOMENTUM_WINDOWS = (5, 20, 50, 100)
# Breadth-strength slope (-1..1 scale per snapshot) that saturates the
# momentum signal; net advancers/snapshot and new-high/low count slopes
# that saturate their respective signals. All three are heuristic scales,
# same spirit as trend.py's MOMENTUM_SCALE — tunable once real history
# shows typical slope magnitudes for this universe.
MOMENTUM_SIGNAL_SCALE = 0.1
NEW_HL_MOMENTUM_SCALE = 5.0

# Term weights for the overall bull/bear breadth level; only present terms
# contribute, re-weighted by what's actually available (same graceful
# degradation as Trend Intelligence's momentum ladder).
STRENGTH_WEIGHT = 0.40
PARTICIPATION_WEIGHT = 0.25
TREND_PCT_WEIGHT = 0.15
MOMENTUM_WEIGHT = 0.10
NEW_HL_WEIGHT = 0.10


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _window_values(features: Mapping[str, float], feature: str) -> list[float]:
    return [
        features[f"{feature}_{w}"] for w in MOMENTUM_WINDOWS
        if features.get(f"{feature}_{w}") is not None
    ]


def assess_breadth(features: Mapping[str, float]) -> IntelligenceResult:
    """Pure breadth assessment from the latest feature values."""
    contributions: list[Contribution] = []
    reasoning: list[str] = []
    level_terms: list[tuple[float, float]] = []

    strength = features.get("breadth_strength")
    if strength is not None:
        signal = clamp(strength, -1.0, 1.0)
        level_terms.append((signal, STRENGTH_WEIGHT))
        contributions.append(Contribution(
            feature="breadth_strength", value=strength, weight=STRENGTH_WEIGHT,
            effect="advancers lead" if strength > 0 else "decliners lead",
        ))

    participation_pct = features.get("breadth_participation_pct")
    if participation_pct is not None:
        signal = clamp((participation_pct - 50) / 50, -1.0, 1.0)
        level_terms.append((signal, PARTICIPATION_WEIGHT))
        contributions.append(Contribution(
            feature="breadth_participation_pct", value=participation_pct,
            weight=PARTICIPATION_WEIGHT,
            effect="majority advancing" if participation_pct > 50 else "majority declining",
        ))

    trend_pct = features.get("breadth_trend_pct")
    if trend_pct is not None:
        signal = clamp((trend_pct - 50) / 50, -1.0, 1.0)
        level_terms.append((signal, TREND_PCT_WEIGHT))
        contributions.append(Contribution(
            feature="breadth_trend_pct", value=trend_pct, weight=TREND_PCT_WEIGHT,
            effect="above key EMAs" if trend_pct > 50 else "below key EMAs",
        ))

    momentum_values = _window_values(features, "breadth_momentum")
    momentum_mean = _mean(momentum_values)
    momentum_signal = 0.0
    if momentum_mean is not None:
        momentum_signal = math.tanh(momentum_mean / MOMENTUM_SIGNAL_SCALE)
        level_terms.append((momentum_signal, MOMENTUM_WEIGHT))
        contributions.append(Contribution(
            feature="breadth_momentum", value=momentum_mean, weight=MOMENTUM_WEIGHT,
            effect="breadth improving" if momentum_signal > 0 else "breadth deteriorating",
        ))

    new_high_momentum = _mean(_window_values(features, "breadth_new_high_momentum"))
    new_low_momentum = _mean(_window_values(features, "breadth_new_low_momentum"))
    nh_nl_signal = 0.0
    if new_high_momentum is not None and new_low_momentum is not None:
        nh_nl_net = new_high_momentum - new_low_momentum
        nh_nl_signal = math.tanh(nh_nl_net / NEW_HL_MOMENTUM_SCALE)
        level_terms.append((nh_nl_signal, NEW_HL_WEIGHT))
        contributions.append(Contribution(
            feature="breadth_new_high_low_momentum", value=nh_nl_net, weight=NEW_HL_WEIGHT,
            effect="new highs expanding" if nh_nl_signal > 0 else "new lows expanding",
        ))

    total_weight = sum(w for _, w in level_terms)
    level = sum(v * w for v, w in level_terms) / total_weight if total_weight > 0 else 0.0
    level = clamp(level, -1.0, 1.0)

    # Participation Quality: broadness of the move (participation % and %
    # above key EMAs), docked when equal-weight badly trails cap-weight
    # (breadth_divergence very negative = a handful of large caps carrying
    # the index while the broad market lags).
    quality_terms = [v for v in (
        participation_pct / 100 if participation_pct is not None else None,
        trend_pct / 100 if trend_pct is not None else None,
    ) if v is not None]
    # Computed inline (not via _mean) so mypy sees a plain float, not
    # float | None — quality_terms is checked non-empty right here.
    base_quality = sum(quality_terms) / len(quality_terms) if quality_terms else 0.5

    divergence = features.get("breadth_divergence")
    divergence_penalty = 1.0
    if divergence is not None:
        divergence_penalty = clamp(1 + min(divergence, 0.0) / 10.0, 0.0, 1.0)
        contributions.append(Contribution(
            feature="breadth_divergence", value=divergence, weight=0.1,
            effect="narrow (large-cap led)" if divergence < 0 else "broadly confirmed",
        ))
    participation_quality = clamp(base_quality * divergence_penalty, 0.0, 1.0)

    health_score = features.get("breadth_health_score")
    if health_score is not None:
        breadth_health = clamp(health_score, 0.0, 100.0)
        contributions.append(Contribution(
            feature="breadth_health_score", value=health_score, weight=0.2,
            effect="healthy" if health_score >= 50 else "unhealthy",
        ))
    else:
        # Collector composite unavailable: derive a proxy from quality and level.
        breadth_health = clamp(
            100 * (0.5 * participation_quality + 0.5 * (0.5 + 0.5 * level)), 0.0, 100.0
        )
        reasoning.append("No collector health composite; derived a proxy from quality/level.")

    data_completeness = len(level_terms) / 5.0
    momentum_sign = (
        (1 if momentum_signal > 0 else -1 if momentum_signal < 0 else 0)
        if momentum_mean is not None else None
    )
    nh_nl_sign = (
        (1 if nh_nl_signal > 0 else -1 if nh_nl_signal < 0 else 0)
        if new_high_momentum is not None and new_low_momentum is not None else None
    )
    signs = [s for s in (momentum_sign, nh_nl_sign) if s is not None]
    nonzero_signs = {s for s in signs if s != 0}
    # 0.0 (not a "neutral" 0.5) when there is nothing to agree on at all —
    # a missing-data floor consistent with Trend/Volatility Intelligence,
    # which don't give confidence credit for undetermined agreement either.
    momentum_agreement = 1.0 if signs and len(nonzero_signs) <= 1 else (0.5 if signs else 0.0)
    quality_confidence_term = participation_quality if quality_terms else 0.0
    confidence = clamp(
        0.25 + 0.35 * data_completeness + 0.25 * momentum_agreement
        + 0.15 * quality_confidence_term,
        0.0, 1.0,
    )

    # Regime states use "conviction" (raw breadth strength, undiluted by
    # participation/trend) rather than the blended composite `level`: a
    # genuine narrow rally has strong strength but weak participation/trend,
    # which drags the blended `level` toward zero and would otherwise dilute
    # narrow_participation's own weight into "mixed" — exactly the case a
    # narrow-breadth regime needs to be distinguishable from.
    conviction = clamp(strength, -1.0, 1.0) if strength is not None else level
    narrowness = 1 - participation_quality
    states = normalize_states({
        "broad_participation": abs(conviction) * participation_quality,
        "narrow_participation": abs(conviction) * narrowness,
        "improving_breadth": max(momentum_signal, 0.0),
        "deteriorating_breadth": max(-momentum_signal, 0.0),
        "mixed": (1 - abs(conviction)) * participation_quality,
    })

    score = clamp(50 + 50 * level)
    dominant = max(states, key=lambda s: states[s]) if states else "unknown"
    reasoning.extend([
        f"Breadth level {level:+.2f} from {len(level_terms)}/5 available signal groups; "
        f"participation quality {participation_quality:.0%}.",
        f"Breadth health {breadth_health:.0f}/100; "
        + (f"divergence {divergence:+.1f}pp." if divergence is not None else "no divergence data."),
        f"Dominant state: {dominant}.",
    ])

    return IntelligenceResult(
        component=COMPONENT,
        score=score,
        confidence=confidence,
        states=states,
        metrics={
            "breadth_level": round(level, 4),
            "breadth_health": round(breadth_health, 4),
            "participation_quality": round(participation_quality, 4),
            "breadth_confidence": round(confidence, 4),
            "breadth_divergence": round(divergence, 4) if divergence is not None else None,
        },
        contributions=contributions,
        reasoning=reasoning,
    )


class BreadthIntelligenceEngine(IntelligenceComponent):
    name = "breadth_intelligence"

    async def assess(self) -> IntelligenceResult:
        """Breadth is market-wide: always MARKET/"breadth", never a symbol arg."""
        features = await self.latest_values(MARKET_SYMBOL, BREADTH_TIMEFRAME)
        result = assess_breadth(features)
        result.metrics["symbol"] = MARKET_SYMBOL
        await self._publish_assessment(MARKET_SYMBOL, result)
        return result
