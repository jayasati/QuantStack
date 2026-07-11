"""Relative Strength Intelligence Engine (Volume 4, Prompt 4.9).

Per-instrument, like Trend/Volatility/Liquidity Intelligence — assess()
defaults to the benchmark symbol for interface consistency, but the
benchmark index itself carries no rs_* features (Volume 3's Relative
Strength Feature Engine only computes them for equities, comparing each
stock against Nifty/Sensex/its sector/industry/peer basket), so that
default gracefully reads neutral/low-confidence. Pass a stock symbol for a
meaningful read.

Blends the five-reference strength/momentum ladder and the percentile-rank/
outperformance composite the feature store already computes:

- IntelligenceResult.score      -> Outperformance Score (passthrough/blend
                                    of rs_outperformance_{w}, 50 = in line)
- IntelligenceResult.confidence -> Relative Strength Confidence
- metrics["relative_trend"]     -> Relative Trend (blended strength, -1..1)
- metrics["relative_momentum"]  -> Relative Momentum (blended drift, -1..1)
- metrics["leadership_ranking"] -> Leadership Ranking (percentile 0-100
                                    among peers + self)

States follow Trend Intelligence's own pattern most closely of every prior
component (both are per-symbol, directional reads): leading, lagging,
in_line, rotating — where "rotating" catches a stock whose references
disagree (e.g. beating Nifty but lagging its own sector), consistent with
Correlation Intelligence's already-established naming for a similar
disagreement concept.
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

COMPONENT = "relative_strength"

RS_WINDOWS = (5, 20, 50, 100)
REFERENCES = ("nifty", "sensex", "sector", "industry", "peers")

# % points of cumulative relative strength that saturate the per-reference
# signal; % per bar of relative momentum that saturates the momentum
# signal — heuristic scales, matching the feature engine's own tanh(s/5)
# choice for strength and a documented v1 guess for momentum.
STRENGTH_SATURATION = 5.0
MOMENTUM_SATURATION = 1.0


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _blend(features: Mapping[str, float], prefix: str, ref: str) -> float | None:
    values = [
        features[f"rs_{ref}_{prefix}_{w}"] for w in RS_WINDOWS
        if features.get(f"rs_{ref}_{prefix}_{w}") is not None
    ]
    return _mean(values)


def assess_relative_strength(features: Mapping[str, float]) -> IntelligenceResult:
    """Pure relative-strength assessment from the latest feature values."""
    contributions: list[Contribution] = []
    reasoning: list[str] = []

    strength_signals: dict[str, float] = {}
    for ref in REFERENCES:
        mean_strength = _blend(features, "strength", ref)
        if mean_strength is not None:
            strength_signals[ref] = math.tanh(mean_strength / STRENGTH_SATURATION)
            contributions.append(Contribution(
                feature=f"rs_{ref}_strength", value=mean_strength, weight=1 / len(REFERENCES),
                effect="outperforming" if mean_strength > 0 else "underperforming",
            ))
    level = clamp(_mean(list(strength_signals.values())) or 0.0, -1.0, 1.0)

    momentum_signals = []
    for ref in REFERENCES:
        mean_momentum = _blend(features, "momentum", ref)
        if mean_momentum is not None:
            momentum_signals.append(math.tanh(mean_momentum / MOMENTUM_SATURATION))
    momentum_level = clamp(_mean(momentum_signals) or 0.0, -1.0, 1.0) if momentum_signals else 0.0
    if momentum_signals:
        contributions.append(Contribution(
            feature="rs_momentum_blend", value=momentum_level, weight=0.15,
            effect="accelerating" if momentum_level > 0 else "decelerating",
        ))

    outperformance_values = [
        features[f"rs_outperformance_{w}"] for w in RS_WINDOWS
        if features.get(f"rs_outperformance_{w}") is not None
    ]
    outperformance_score = (
        _mean(outperformance_values) if outperformance_values
        else clamp(50 + 50 * level, 0.0, 100.0)
    )
    outperformance_score = clamp(outperformance_score or 50.0, 0.0, 100.0)

    percentile_values = [
        features[f"rs_percentile_rank_{w}"] for w in RS_WINDOWS
        if features.get(f"rs_percentile_rank_{w}") is not None
    ]
    leadership_ranking = _mean(percentile_values)

    overall_sign = 1 if level > 0 else (-1 if level < 0 else 0)
    agreeing = sum(
        1 for v in strength_signals.values()
        if (1 if v > 0 else (-1 if v < 0 else 0)) == overall_sign
    ) if overall_sign != 0 else 0
    consistency = agreeing / len(strength_signals) if strength_signals else 0.0

    data_completeness = len(strength_signals) / len(REFERENCES)
    confidence = clamp(
        0.2 + 0.3 * data_completeness + 0.3 * consistency
        + 0.2 * (1.0 if outperformance_values else 0.0),
        0.0, 1.0,
    )

    bull = max(level, 0.0)
    bear = max(-level, 0.0)
    states = normalize_states({
        "leading": bull * consistency,
        "lagging": bear * consistency,
        "in_line": 1 - abs(level),
        "rotating": abs(level) * (1 - consistency),
    })

    dominant = max(states, key=lambda s: states[s]) if states else "unknown"
    reasoning.extend([
        f"Relative level {level:+.2f} from {len(strength_signals)}/{len(REFERENCES)} "
        f"reference(s), {consistency:.0%} agreeing.",
        f"Outperformance {outperformance_score:.0f}/100"
        + (f", leadership percentile {leadership_ranking:.0f}." if leadership_ranking is not None
           else "."),
        f"Dominant state: {dominant}.",
    ])

    return IntelligenceResult(
        component=COMPONENT,
        score=outperformance_score,
        confidence=confidence,
        states=states,
        metrics={
            "relative_trend": round(level, 4),
            "relative_momentum": round(momentum_level, 4),
            "leadership_ranking": (
                round(leadership_ranking, 4) if leadership_ranking is not None else None
            ),
            "reference_agreement": round(consistency, 4),
        },
        contributions=contributions,
        reasoning=reasoning,
    )


class RelativeStrengthIntelligenceEngine(IntelligenceComponent):
    name = "relative_strength_intelligence"

    async def assess(
        self, symbol: str | None = None, timeframe: str = "D"
    ) -> IntelligenceResult:
        symbol = symbol or self._settings.feature_benchmark_symbol
        features = await self.latest_values(symbol, timeframe)
        result = assess_relative_strength(features)
        result.metrics["symbol"] = symbol
        await self._publish_assessment(symbol, result)
        return result
