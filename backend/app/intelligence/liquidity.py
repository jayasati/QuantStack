"""Liquidity Intelligence Engine (Volume 4, Prompt 4.6).

Unlike the market-wide components (Breadth, Sector, Institutional Flow),
liquidity is inherently per-instrument — there is no single "the market's
liquidity" read the way there's a benchmark-index proxy for trend/volatility.
``assess()`` still defaults to the benchmark symbol to keep the same
zero-argument-callable shape every other component has, but since indices
quote without bid/ask/depth/volume by design, that default gracefully
returns a neutral, low-confidence read — not fabricated, just genuinely
"no liquidity data for this instrument." Pass a tradable symbol (stock)
for a meaningful one.

Consumes two timeframes for that symbol: the microstructure features under
"quote" (spread, order book depth/imbalance, market impact, the 0-100
Liquidity Score composite, and its rolling trend) and the daily features
under "D" (turnover, delivery %).

- IntelligenceResult.score      -> Liquidity Score (passthrough of
                                    liquidity_score when available, else 50)
- IntelligenceResult.confidence -> Liquidity Confidence
- metrics["liquidity_stress"]   -> Liquidity Stress (0-100)
- metrics["execution_risk"]     -> Execution Risk (0-100, cost of trading now)

States use Chapter 4's actual Liquidity taxonomy: Highly Liquid, Healthy,
Thin, Illiquid, Stress, Auction Driven. Stress/Auction Driven are
deliberately half-weighted in the blend — the same dilution issue fixed in
Breadth and Institutional Flow Intelligence (an undiluted overlay signal
structurally dominates the diluted level buckets) — applied here from the
start rather than discovered via a failing test.
"""

from collections.abc import Mapping

from app.features.liquidity import IMPACT_SCORE_CEILING_PCT, SPREAD_SCORE_CEILING_PCT
from app.intelligence.base import (
    Contribution,
    IntelligenceComponent,
    IntelligenceResult,
    clamp,
    normalize_states,
)

COMPONENT = "liquidity"

QUOTE_TIMEFRAME = "quote"
DAILY_TIMEFRAME = "D"

LIQUIDITY_WINDOWS = (5, 20, 50, 100)

LEVEL_ANCHORS: dict[str, float] = {
    "illiquid": 0.0,
    "thin": 0.25,
    "healthy": 0.6,
    "highly_liquid": 0.9,
}
LEVEL_BAND = 0.3
# Mean liquidity-trend slope (score points/snapshot) that saturates the
# deteriorating-liquidity stress signal — a heuristic scale, same spirit as
# the saturation constants elsewhere in this layer.
TREND_STRESS_SATURATION = -3.0
# turnover_z minus delivery_pct_z that saturates the "churn, not genuine
# interest" stress signal (elevated volume without matching delivery
# conviction reads as thin, speculative activity rather than real depth).
CHURN_SATURATION = 4.0


def _level_weights(level: float) -> dict[str, float]:
    return {
        name: max(0.0, 1 - abs(level - anchor) / LEVEL_BAND)
        for name, anchor in LEVEL_ANCHORS.items()
    }


def _window_values(features: Mapping[str, float], feature: str) -> list[float]:
    return [
        features[f"{feature}_{w}"] for w in LIQUIDITY_WINDOWS
        if features.get(f"{feature}_{w}") is not None
    ]


def assess_liquidity(features: Mapping[str, float]) -> IntelligenceResult:
    """Pure liquidity assessment from the latest feature values (already
    merged across the "quote" and "D" timeframes for one symbol)."""
    contributions: list[Contribution] = []
    reasoning: list[str] = []

    score_raw = features.get("liquidity_score")
    level = clamp(score_raw / 100, 0.0, 1.0) if score_raw is not None else 0.5
    score = clamp(score_raw, 0.0, 100.0) if score_raw is not None else 50.0
    if score_raw is not None:
        contributions.append(Contribution(
            feature="liquidity_score", value=score_raw, weight=0.4,
            effect="liquid" if score_raw >= 50 else "illiquid",
        ))
        reasoning.append(f"Liquidity score passthrough: {score:.0f}/100.")
    else:
        reasoning.append("Liquidity score unavailable; defaulting to neutral.")

    spread_pct = features.get("liquidity_spread_pct")
    impact_pct = features.get("liquidity_market_impact_pct")
    if spread_pct is not None:
        contributions.append(Contribution(
            feature="liquidity_spread_pct", value=spread_pct, weight=0.2,
            effect="tight" if spread_pct < SPREAD_SCORE_CEILING_PCT / 2 else "wide",
        ))
    if impact_pct is not None:
        contributions.append(Contribution(
            feature="liquidity_market_impact_pct", value=impact_pct, weight=0.2,
            effect="low impact" if impact_pct < IMPACT_SCORE_CEILING_PCT / 2 else "high impact",
        ))

    # Execution Risk: specifically the cost of trading right now — spread and
    # market impact — distinct from the broader stress/level read below.
    risk_terms = []
    if spread_pct is not None:
        risk_terms.append(clamp(spread_pct / SPREAD_SCORE_CEILING_PCT, 0.0, 1.0))
    if impact_pct is not None:
        risk_terms.append(clamp(impact_pct / IMPACT_SCORE_CEILING_PCT, 0.0, 1.0))
    execution_risk = (
        100 * sum(risk_terms) / len(risk_terms) if risk_terms
        else clamp(100 - score, 0.0, 100.0)
    )

    imbalance = features.get("liquidity_order_book_imbalance")
    imbalance_magnitude = abs(imbalance) if imbalance is not None else 0.0
    if imbalance is not None:
        contributions.append(Contribution(
            feature="liquidity_order_book_imbalance", value=imbalance, weight=0.15,
            effect="buy-side heavy" if imbalance > 0 else "sell-side heavy",
        ))

    trend_values = _window_values(features, "liquidity_trend")
    trend_mean = sum(trend_values) / len(trend_values) if trend_values else 0.0
    deteriorating = clamp(trend_mean / TREND_STRESS_SATURATION, 0.0, 1.0)
    if trend_values:
        contributions.append(Contribution(
            feature="liquidity_trend", value=trend_mean, weight=0.1,
            effect="improving" if trend_mean > 0 else "deteriorating",
        ))

    turnover_z = features.get("liquidity_turnover_z")
    delivery_z = features.get("liquidity_delivery_pct_z")
    churn_signal = 0.0
    if turnover_z is not None and delivery_z is not None:
        churn_signal = clamp((turnover_z - delivery_z) / CHURN_SATURATION, 0.0, 1.0)
        contributions.append(Contribution(
            feature="liquidity_turnover_vs_delivery", value=turnover_z - delivery_z,
            weight=0.1,
            effect="speculative churn" if churn_signal > 0.3 else "genuine interest",
        ))

    stress_signal = clamp(
        0.45 * deteriorating + 0.35 * imbalance_magnitude + 0.2 * churn_signal, 0.0, 1.0
    )
    auction_signal = clamp((1 - level) * imbalance_magnitude, 0.0, 1.0)

    # Stress/Auction Driven halved before blending: undiluted 0-1 overlay
    # signals would otherwise structurally dominate the diluted level
    # buckets whenever they're simply elevated, even when the level itself
    # is unambiguous — see Breadth and Institutional Flow Intelligence.
    states = normalize_states({
        **_level_weights(level),
        "stress": 0.5 * stress_signal,
        "auction_driven": 0.5 * auction_signal,
    })

    required = (
        "liquidity_score", "liquidity_spread_pct", "liquidity_order_book_imbalance",
        "liquidity_market_impact_pct", "liquidity_turnover", "liquidity_delivery_pct",
    )
    data_completeness = sum(1 for f in required if features.get(f) is not None) / len(required)
    trend_agreement = (
        1.0 if trend_values and all((t > 0) == (trend_values[0] > 0) for t in trend_values)
        else (0.5 if trend_values else 0.0)
    )
    confidence = clamp(
        0.2 + 0.4 * data_completeness + 0.2 * trend_agreement
        - 0.15 * stress_signal,
        0.0, 1.0,
    )

    dominant = max(states, key=lambda s: states[s]) if states else "unknown"
    reasoning.extend([
        f"Execution risk {execution_risk:.0f}/100; order book "
        + (f"imbalance {imbalance:+.2f}." if imbalance is not None else "imbalance unavailable."),
        f"Stress signal {stress_signal:.0%} (deteriorating {deteriorating:.0%}, "
        f"churn {churn_signal:.0%}).",
        f"Dominant state: {dominant}.",
    ])

    return IntelligenceResult(
        component=COMPONENT,
        score=score,
        confidence=confidence,
        states=states,
        metrics={
            "liquidity_level": round(level, 4),
            "liquidity_stress": round(100 * stress_signal, 4),
            "execution_risk": round(execution_risk, 4),
            "order_book_imbalance": round(imbalance, 4) if imbalance is not None else None,
        },
        contributions=contributions,
        reasoning=reasoning,
    )


class LiquidityIntelligenceEngine(IntelligenceComponent):
    name = "liquidity_intelligence"

    async def assess(self, symbol: str | None = None) -> IntelligenceResult:
        symbol = symbol or self._settings.feature_benchmark_symbol
        quote_features = await self.latest_values(symbol, QUOTE_TIMEFRAME)
        daily_features = await self.latest_values(symbol, DAILY_TIMEFRAME)
        result = assess_liquidity({**quote_features, **daily_features})
        result.metrics["symbol"] = symbol
        await self._publish_assessment(symbol, result)
        return result
