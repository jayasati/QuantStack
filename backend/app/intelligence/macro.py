"""Macro Intelligence Engine (Volume 4 gap fill, for Prompt 4.14's aggregate list).

Chapter 4's regime taxonomy lists "Macro" (Risk On, Risk Off, Inflation,
Disinflation, Rate Hike, Rate Cut, Growth, Recession, Stagflation) as its
own dimension, and Prompt 4.14 aggregates it as a peer of Trend/Volatility/
etc., but no Volume 4 prompt (4.1-4.13) was ever assigned to build it — the
macro data itself only got a Feature Store representation as part of
Prompt 4.8's own gap-fill (app/features/macro.py).

Scoped honestly to what the 14 tracked macro factors (USDINR, DXY, US10Y,
INDIA10Y, CRUDE, GOLD, SILVER, NATGAS, SPX, NDX, NIKKEI, HANGSENG, DAX,
CRYPTO_MCAP) can actually support: Risk On / Risk Off / Mixed. Each factor's
macro_score is already signed for India-equity impact (see
collectors/domains/macro.py's FACTOR_SIGNS), so the aggregate is a
straightforward mean rather than needing its own sign convention.
Inflation/Disinflation/Rate Hike/Rate Cut/Growth/Recession/Stagflation are
NOT attempted — none of the 14 factors is a direct inflation or growth
read (no CPI/GDP factor is tracked), and inventing those distinctions from
proxies that don't measure them would be fabricating precision the data
doesn't have.
"""

from collections.abc import Mapping

from app.intelligence.base import (
    Contribution,
    IntelligenceComponent,
    IntelligenceResult,
    clamp,
    normalize_states,
)

COMPONENT = "macro"
MACRO_TIMEFRAME = "macro"

FACTOR_UNIVERSE: tuple[str, ...] = (
    "USDINR", "DXY", "US10Y", "INDIA10Y", "CRUDE", "GOLD", "SILVER", "NATGAS",
    "SPX", "NDX", "NIKKEI", "HANGSENG", "DAX", "CRYPTO_MCAP",
)

# Mean |macro_score| that reads as full macro conviction — same scale and
# same rationale as Institutional Flow Intelligence's CONVICTION_SATURATION:
# a weighted/simple mean across many components rarely approaches +/-1
# even when every component genuinely agrees.
CONVICTION_SATURATION = 0.3


def assess_macro(factor_scores: Mapping[str, float | None]) -> IntelligenceResult:
    """Pure macro assessment from each factor's latest signed macro_score."""
    contributions: list[Contribution] = []
    present = {k: v for k, v in factor_scores.items() if v is not None}

    for name, value in present.items():
        contributions.append(Contribution(
            feature=f"macro_score[{name}]", value=value, weight=1 / len(FACTOR_UNIVERSE),
            effect="risk-on" if value > 0 else ("risk-off" if value < 0 else "flat"),
        ))

    level = clamp(sum(present.values()) / len(present), -1.0, 1.0) if present else 0.0
    score = clamp(50 + 50 * level, 0.0, 100.0)

    net_sign = 1.0 if level > 0 else (-1.0 if level < 0 else 0.0)
    agreeing = sum(
        1 for v in present.values() if (1.0 if v > 0 else (-1.0 if v < 0 else 0.0)) == net_sign
    ) if net_sign != 0 else 0
    consistency = agreeing / len(present) if present else 0.0

    raw_activity = sum(abs(v) for v in present.values()) / len(present) if present else 0.0
    conviction = clamp(raw_activity / CONVICTION_SATURATION, 0.0, 1.0)

    data_completeness = len(present) / len(FACTOR_UNIVERSE)
    confidence = (
        clamp(0.2 + 0.3 * data_completeness + 0.5 * consistency, 0.0, 1.0) if present else 0.0
    )

    states = normalize_states({
        "risk_on": conviction * consistency * max(net_sign, 0.0),
        "risk_off": conviction * consistency * max(-net_sign, 0.0),
        "mixed": 1 - conviction,
    })

    dominant = max(states, key=lambda s: states[s])
    reasoning = [
        f"{len(present)}/{len(FACTOR_UNIVERSE)} factors reporting; level {level:+.2f}, "
        f"{consistency:.0%} agreeing in direction.",
        f"Conviction {conviction:.0%}.",
        f"Dominant state: {dominant}.",
    ]

    return IntelligenceResult(
        component=COMPONENT,
        score=score,
        confidence=confidence,
        states=states,
        metrics={
            "macro_level": round(level, 4),
            "conviction": round(conviction, 4),
            "consistency": round(consistency, 4),
            "factors_present": len(present),
        },
        contributions=contributions,
        reasoning=reasoning,
    )


class MacroIntelligenceEngine(IntelligenceComponent):
    name = "macro_intelligence"

    async def assess(self) -> IntelligenceResult:
        """Macro is cross-sectional across the factor universe, not a single symbol."""
        factor_scores: dict[str, float | None] = {}
        for factor in FACTOR_UNIVERSE:
            features = await self.latest_values(factor, MACRO_TIMEFRAME)
            factor_scores[factor] = features.get("macro_score")
        return assess_macro(factor_scores)
