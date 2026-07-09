"""Institutional Flow Intelligence Engine (Volume 4, Prompt 4.5).

Consumes the institutional flow feature set (FII, DII, ETF, combined block +
bulk deal activity, promoter net, insider net, SAST filing activity, and the
momentum ladder over FII/DII/participation) built for this prompt.

Of the five named outputs, one maps onto the base IntelligenceResult
contract's score field and one onto confidence:
- IntelligenceResult.score      -> Institutional Participation Score
                                    (0-100, 50 = neutral — passthrough of
                                    flow_participation_index when available,
                                    else recomputed from component scores)
- IntelligenceResult.confidence -> Flow Confidence
- metrics["accumulation_score"] -> Accumulation Score (0-100)
- metrics["distribution_score"] -> Distribution Score (0-100)
- metrics["flow_momentum"]      -> Flow Momentum (-1..1)

This is also where Chapter 4's Participation-dimension states that Breadth
Intelligence explicitly deferred finally belong: institutional_accumulation
and institutional_distribution require real FII/DII/deal/promoter/insider
data, which only exists here. retail_driven captures the complementary case
— institutional flows are quiet, so whatever move is happening isn't
institutionally backed.
"""

from collections.abc import Mapping

from app.intelligence.base import (
    Contribution,
    IntelligenceComponent,
    IntelligenceResult,
    clamp,
    normalize_states,
)

COMPONENT = "institutional_flow"

FLOW_TIMEFRAME = "flow"
MARKET_SYMBOL = "MARKET"

MOMENTUM_WINDOWS = (5, 20, 50, 100)
# Net-flow-direction blend weights — intentionally mirror
# collectors/domains/flows.py's PARTICIPATION_WEIGHTS, since this is the same
# composite concept recomputed here (as a fallback when the collector's own
# passthrough index is missing/stale, and to decompose into accumulation vs
# distribution, which the collector doesn't itself provide).
COMPONENT_WEIGHTS: dict[str, float] = {
    "flow_fii_score": 0.30,
    "flow_dii_score": 0.20,
    "flow_etf_score": 0.10,
    "flow_deal_activity_score": 0.15,
    "flow_promoter_score": 0.15,
    "flow_insider_score": 0.10,
}
# SAST filings signal M&A/control-change activity, not a buy/sell direction —
# folded into confidence only (more filings = more going on = a less clean
# directional read), same treatment as vol-of-vol in Volatility Intelligence.
SAST_CONFIDENCE_PENALTY = 0.2
# Mean absolute component score that reads as full institutional conviction
# for regime-state purposes — a heuristic scale, same spirit as the momentum
# saturation scales elsewhere in this layer.
CONVICTION_SATURATION = 0.3


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _window_values(features: Mapping[str, float], feature: str) -> list[float]:
    return [
        features[f"{feature}_momentum_{w}"] for w in MOMENTUM_WINDOWS
        if features.get(f"{feature}_momentum_{w}") is not None
    ]


def assess_institutional_flow(features: Mapping[str, float]) -> IntelligenceResult:
    """Pure institutional-flow assessment from the latest feature values."""
    contributions: list[Contribution] = []
    reasoning: list[str] = []

    weighted = 0.0
    total_weight = 0.0
    signed_components: list[float] = []
    for feature_name, weight in COMPONENT_WEIGHTS.items():
        value = features.get(feature_name)
        if value is None:
            continue
        signed_components.append(value)
        weighted += weight * value
        total_weight += weight
        contributions.append(Contribution(
            feature=feature_name, value=value, weight=weight,
            effect="accumulating" if value > 0 else ("distributing" if value < 0 else "flat"),
        ))
    net_level = clamp(weighted / total_weight, -1.0, 1.0) if total_weight > 0 else 0.0

    participation_index = features.get("flow_participation_index")
    if participation_index is not None:
        score = clamp(participation_index, 0.0, 100.0)
        reasoning.append(f"Participation index passthrough: {score:.0f}/100.")
    else:
        score = clamp(50 + 50 * net_level, 0.0, 100.0)
        reasoning.append(
            f"Participation index unavailable; recomputed {score:.0f}/100 "
            f"from {len(signed_components)} component score(s)."
        )

    # Accumulation/Distribution: gross buying vs gross selling magnitude
    # across components — unlike net_level, both can be substantial at once
    # (e.g. FII selling while DII buys), which is itself informative.
    # Computed inline (not via _mean) so mypy sees a plain float, not
    # float | None — signed_components is checked non-empty right here.
    accumulation_score = (
        100 * sum(max(v, 0.0) for v in signed_components) / len(signed_components)
        if signed_components else 0.0
    )
    distribution_score = (
        100 * sum(max(-v, 0.0) for v in signed_components) / len(signed_components)
        if signed_components else 0.0
    )

    momentum_bases = ("flow_fii_score", "flow_dii_score", "flow_participation_score")
    momentum_means = [
        m for base in momentum_bases
        if (m := _mean(_window_values(features, base))) is not None
    ]
    flow_momentum = clamp(_mean(momentum_means) or 0.0, -1.0, 1.0) if momentum_means else 0.0
    if momentum_means:
        contributions.append(Contribution(
            feature="flow_momentum_ladder", value=flow_momentum, weight=0.1,
            effect="building" if flow_momentum > 0 else "fading",
        ))

    sast = features.get("flow_sast_score")
    if sast is not None:
        contributions.append(Contribution(
            feature="flow_sast_score", value=sast, weight=0.05,
            effect="elevated M&A/control activity" if sast > 0.5 else "quiet",
        ))

    # Flow Confidence: how many of the consume-list signals reported data,
    # how much they agree in direction, and docked for elevated SAST activity
    # (more M&A/control-change noise around the read).
    required = (
        "flow_fii_score", "flow_dii_score", "flow_etf_score",
        "flow_deal_activity_score", "flow_insider_score", "flow_sast_score",
    )
    data_completeness = sum(1 for f in required if features.get(f) is not None) / len(required)
    agreement = (
        sum(1 for v in signed_components if (v > 0) == (net_level > 0))
        / len(signed_components)
        if signed_components and net_level != 0 else (0.5 if signed_components else 0.0)
    )
    confidence = clamp(
        0.2 + 0.35 * data_completeness + 0.3 * agreement
        - SAST_CONFIDENCE_PENALTY * (sast or 0.0) * 0.5,
        0.0, 1.0,
    )

    # Conviction: mean absolute component score, saturating well below 1.0 —
    # institutional flow scores rarely all hit their theoretical extreme
    # simultaneously, so several components moderately-but-consistently
    # elevated (e.g. all around 0.3-0.6) already represents full institutional
    # conviction, not "mostly quiet." Using the *weighted, signed* net_level's
    # magnitude here instead (as Breadth Intelligence's first draft did with
    # its blended level) would make "retail_driven" win by construction any
    # time six components partly cancel through weighting — exactly the case
    # this needs to distinguish from genuine quiet/retail-driven flow.
    raw_activity = (
        sum(abs(v) for v in signed_components) / len(signed_components)
        if signed_components else 0.0
    )
    conviction = clamp(raw_activity / CONVICTION_SATURATION, 0.0, 1.0)
    net_sign = 1.0 if net_level > 0 else (-1.0 if net_level < 0 else 0.0)
    states = normalize_states({
        "institutional_accumulation": conviction * agreement * max(net_sign, 0.0),
        "institutional_distribution": conviction * agreement * max(-net_sign, 0.0),
        "retail_driven": 1 - conviction,
        "mixed_flow": conviction * (1 - agreement),
    })

    dominant = max(states, key=lambda s: states[s]) if states else "unknown"
    reasoning.extend([
        f"Net flow level {net_level:+.2f} from {len(signed_components)}/6 components, "
        f"{agreement:.0%} agreeing in direction.",
        f"Accumulation {accumulation_score:.0f}/100, distribution {distribution_score:.0f}/100, "
        f"momentum {flow_momentum:+.2f}.",
        f"Dominant state: {dominant}.",
    ])

    return IntelligenceResult(
        component=COMPONENT,
        score=score,
        confidence=confidence,
        states=states,
        metrics={
            "net_flow_level": round(net_level, 4),
            "accumulation_score": round(accumulation_score, 4),
            "distribution_score": round(distribution_score, 4),
            "flow_momentum": round(flow_momentum, 4),
            "participation_index_source": (
                "passthrough" if participation_index is not None else "recomputed"
            ),
        },
        contributions=contributions,
        reasoning=reasoning,
    )


class InstitutionalFlowIntelligenceEngine(IntelligenceComponent):
    name = "institutional_flow_intelligence"

    async def assess(self) -> IntelligenceResult:
        """Institutional flow is market-wide: always MARKET/"flow"."""
        features = await self.latest_values(MARKET_SYMBOL, FLOW_TIMEFRAME)
        result = assess_institutional_flow(features)
        result.metrics["symbol"] = MARKET_SYMBOL
        return result
