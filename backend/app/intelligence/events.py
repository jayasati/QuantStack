"""Event Intelligence Engine (Volume 4, Prompt 4.7).

Consumes the event risk feature set (hours until the nearest event, risk
window flag, expected volatility multiplier, category impact, confidence
reduction, trading freeze flag, market sensitivity composite, historical
event-mix similarity) — Volume 3's Prompt 3.11 already computes almost all
of Prompt 4.7's named outputs directly, so this engine's job is mostly
blending them into the standard IntelligenceResult contract rather than
computing them from scratch.

- IntelligenceResult.score      -> Current Event Risk (0-100; 0 = clear,
                                    not 50-centered — event risk has a
                                    magnitude, not a bull/bear direction)
- IntelligenceResult.confidence -> derived from Confidence Reduction
                                    (1 - reduction), further docked for a
                                    novel event mix (low Historical
                                    Similarity = no track record to trust
                                    the calibration against)
- metrics["hours_until_event"]     -> Hours Until Event (passthrough)
- metrics["expected_impact_score"] -> Expected Impact (0-100)
- metrics["confidence_reduction"]  -> Confidence Reduction (passthrough)
- metrics["historical_similarity"] -> Historical Similarity (passthrough)
- metrics["trading_freeze_recommended"] -> Trading Freeze Recommendation (bool)

No calendar data defaults risk to 0 (not a neutral 0.5) — absence of
evidence isn't evidence of absence, so confidence is docked instead of the
score being inflated to "moderate risk we can't see."
"""

from collections.abc import Mapping

from app.intelligence.base import (
    Contribution,
    IntelligenceComponent,
    IntelligenceResult,
    clamp,
    normalize_states,
)

COMPONENT = "events"

MARKET_SYMBOL = "MARKET"
EVENTS_TIMEFRAME = "events"

# Hours-until-event horizon beyond which urgency contributes ~nothing.
URGENCY_HORIZON_HOURS = 48.0
# Expected-volatility multiplier range (feature store: 1.0-5.0) normalized
# to 0-1 for blending into the risk level.
EXPECTED_VOL_FLOOR = 1.0
EXPECTED_VOL_CEILING = 5.0

LEVEL_ANCHORS: dict[str, float] = {
    "clear": 0.0,
    "elevated_risk": 0.4,
    "high_risk": 0.7,
    "freeze_recommended": 0.9,
}
LEVEL_BAND = 0.3
# Floor applied to freeze_recommended's raw weight when the collector's own
# trading-freeze flag is explicitly set — an explicit decision should never
# be diluted below prominence by the continuous level alone.
FREEZE_FLAG_FLOOR = 0.8


def _level_weights(level: float) -> dict[str, float]:
    return {
        name: max(0.0, 1 - abs(level - anchor) / LEVEL_BAND)
        for name, anchor in LEVEL_ANCHORS.items()
    }


def assess_event_risk(features: Mapping[str, float]) -> IntelligenceResult:
    """Pure event-risk assessment from the latest feature values."""
    contributions: list[Contribution] = []
    reasoning: list[str] = []

    sensitivity = features.get("event_market_sensitivity")
    hours_until = features.get("event_hours_until_next")
    expected_vol = features.get("event_expected_volatility")
    category_impact = features.get("event_category_impact")
    confidence_reduction = features.get("event_confidence_reduction")
    trading_freeze = features.get("event_trading_freeze")
    historical_similarity = features.get("event_historical_similarity")

    level_terms: list[tuple[float, float]] = []
    if sensitivity is not None:
        level_terms.append((clamp(sensitivity, 0.0, 1.0), 0.5))
        contributions.append(Contribution(
            feature="event_market_sensitivity", value=sensitivity, weight=0.5,
            effect="elevated" if sensitivity > 0.5 else "contained",
        ))
    if hours_until is not None:
        urgency = clamp(1 - hours_until / URGENCY_HORIZON_HOURS, 0.0, 1.0)
        level_terms.append((urgency, 0.3))
        contributions.append(Contribution(
            feature="event_hours_until_next", value=hours_until, weight=0.3,
            effect="imminent" if urgency > 0.5 else "distant",
        ))
    if expected_vol is not None:
        vol_signal = clamp(
            (expected_vol - EXPECTED_VOL_FLOOR) / (EXPECTED_VOL_CEILING - EXPECTED_VOL_FLOOR),
            0.0, 1.0,
        )
        level_terms.append((vol_signal, 0.2))
        contributions.append(Contribution(
            feature="event_expected_volatility", value=expected_vol, weight=0.2,
            effect="high multiplier" if expected_vol > 2.0 else "modest multiplier",
        ))
    total_weight = sum(w for _, w in level_terms)
    risk_level = sum(v * w for v, w in level_terms) / total_weight if total_weight > 0 else 0.0

    expected_impact_score = (
        100 * category_impact if category_impact is not None
        else (100 * (risk_level)) if level_terms else None
    )

    states_raw = _level_weights(risk_level)
    if trading_freeze == 1.0:
        states_raw["freeze_recommended"] = max(states_raw["freeze_recommended"], FREEZE_FLAG_FLOOR)
        contributions.append(Contribution(
            feature="event_trading_freeze", value=trading_freeze, weight=0.3,
            effect="freeze recommended",
        ))
    states = normalize_states(states_raw)

    if confidence_reduction is not None:
        base_confidence = clamp(1 - confidence_reduction, 0.0, 1.0)
        contributions.append(Contribution(
            feature="event_confidence_reduction", value=confidence_reduction, weight=0.3,
            effect="reduces confidence" if confidence_reduction > 0 else "no reduction",
        ))
    else:
        base_confidence = 0.7  # no calendar signal to reduce trust in; not full trust either

    if historical_similarity is not None:
        novelty_penalty = 0.15 * (1 - historical_similarity)
        contributions.append(Contribution(
            feature="event_historical_similarity", value=historical_similarity, weight=0.1,
            effect="familiar mix" if historical_similarity > 0.5 else "novel mix",
        ))
    else:
        novelty_penalty = 0.1

    confidence = clamp(base_confidence - novelty_penalty, 0.0, 1.0)

    score = clamp(100 * risk_level, 0.0, 100.0)
    dominant = max(states, key=lambda s: states[s]) if states else "unknown"
    reduction_note = (
        f"{confidence_reduction}" if confidence_reduction is not None else "n/a"
    )
    similarity_note = (
        f"{historical_similarity}" if historical_similarity is not None else "n/a"
    )
    reasoning.extend([
        f"Risk level {risk_level:.2f} from {len(level_terms)}/3 available signal(s)"
        + (f"; nearest event in {hours_until:.1f}h." if hours_until is not None else "."),
        f"Confidence reduction {reduction_note}, historical similarity {similarity_note}.",
        f"Dominant state: {dominant}.",
    ])

    return IntelligenceResult(
        component=COMPONENT,
        score=score,
        confidence=confidence,
        states=states,
        metrics={
            "hours_until_event": hours_until,
            "expected_impact_score": (
                round(expected_impact_score, 4) if expected_impact_score is not None else None
            ),
            "confidence_reduction": (
                round(confidence_reduction, 4) if confidence_reduction is not None else None
            ),
            "historical_similarity": (
                round(historical_similarity, 4) if historical_similarity is not None else None
            ),
            "trading_freeze_recommended": trading_freeze == 1.0,
        },
        contributions=contributions,
        reasoning=reasoning,
    )


class EventIntelligenceEngine(IntelligenceComponent):
    name = "event_intelligence"

    async def assess(self) -> IntelligenceResult:
        """Event risk is market-wide: always MARKET/"events"."""
        features = await self.latest_values(MARKET_SYMBOL, EVENTS_TIMEFRAME)
        result = assess_event_risk(features)
        result.metrics["symbol"] = MARKET_SYMBOL
        return result
