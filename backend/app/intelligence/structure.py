"""Market Structure Intelligence Engine (Volume 4 gap fill, for Prompt
4.14's aggregate list).

Same situation as Macro Intelligence: Chapter 4 lists "Market Structure"
(Accumulation, Markup, Distribution, Markdown, Liquidity Sweep, Stop Hunt,
Expansion, Consolidation) as its own regime dimension, and Prompt 4.14
aggregates it as a peer of Trend/Volatility/etc., but no Volume 4 prompt
was ever assigned to build it — even though the underlying feature data
(Volume 3, Prompt 3.9) is rich: swing structure, break of structure/change
of character, structural bias, breakout/sweep probabilities.

Scoped to what's actually backed by that data:
- Markup/Markdown: a confirmed swing trend (ms_trend_direction != 0) with
  bias in that direction.
- Accumulation/Distribution: bias tilting up/down while NOT in a confirmed
  swing trend — a quiet directional lean without trend confirmation yet.
- Consolidation: flat bias, no confirmed trend.
- Liquidity Sweep: ms_sweep_probability directly. Stop Hunt is NOT split
  out separately — it's the same underlying signal in this feature set
  (proximity to a resting-liquidity level), and inventing a distinct
  "stop hunt vs. sweep" read from one probability would be fabricating a
  distinction the data doesn't support. Expansion is likewise not split
  from Markup/Markdown here — a confirmed trend already implies range
  expansion in that direction; treating it as a fully separate state would
  need its own band-width-regime read, deferred as a v2 extension.

Change of Character (a break of structure against the prevailing trend)
docks confidence rather than shifting the level — it signals the CURRENT
structural read may be about to invalidate, not a fact about direction.
"""

from collections.abc import Mapping

from app.intelligence.base import (
    Contribution,
    IntelligenceComponent,
    IntelligenceResult,
    clamp,
    normalize_states,
)

COMPONENT = "market_structure"

REQUIRED_FEATURES: tuple[str, ...] = (
    "ms_structural_bias", "ms_trend_direction", "ms_breakout_probability", "ms_sweep_probability",
)
# Confidence penalty applied when a Change of Character fired recently —
# the read may be about to invalidate, a heuristic scale like elsewhere.
CHOC_CONFIDENCE_PENALTY = 0.15


def assess_market_structure(features: Mapping[str, float]) -> IntelligenceResult:
    """Pure market-structure assessment from the latest feature values."""
    contributions: list[Contribution] = []

    bias = features.get("ms_structural_bias")
    trend_dir = features.get("ms_trend_direction")
    breakout_prob = features.get("ms_breakout_probability")
    sweep_prob = features.get("ms_sweep_probability")
    choc = features.get("ms_change_of_character")

    level = clamp(bias, -1.0, 1.0) if bias is not None else 0.0
    if bias is not None:
        contributions.append(Contribution(
            feature="ms_structural_bias", value=bias, weight=0.4,
            effect="bullish structure" if bias > 0 else "bearish structure",
        ))

    is_trending = trend_dir is not None and trend_dir != 0
    if trend_dir is not None:
        contributions.append(Contribution(
            feature="ms_trend_direction", value=trend_dir, weight=0.3,
            effect="confirmed trend" if is_trending else "ranging",
        ))

    bull = max(level, 0.0)
    bear = max(-level, 0.0)
    trending_weight = 1.0 if is_trending else 0.0
    ranging_weight = 1 - trending_weight

    sweep_weight = 0.5 * (sweep_prob or 0.0)
    if sweep_prob is not None:
        contributions.append(Contribution(
            feature="ms_sweep_probability", value=sweep_prob, weight=0.15,
            effect="elevated sweep risk" if sweep_prob > 0.5 else "low sweep risk",
        ))
    if breakout_prob is not None:
        contributions.append(Contribution(
            feature="ms_breakout_probability", value=breakout_prob, weight=0.1,
            effect="breakout building" if breakout_prob > 0.5 else "no breakout signal",
        ))

    # Liquidity Sweep is halved before blending: an undiluted 0-1 overlay
    # would otherwise structurally dominate the five diluted directional
    # buckets whenever sweep probability is simply elevated — the same
    # dilution issue fixed in Breadth/Institutional Flow/Liquidity Intelligence.
    states = normalize_states({
        "markup": bull * trending_weight,
        "markdown": bear * trending_weight,
        "accumulation": bull * ranging_weight,
        "distribution": bear * ranging_weight,
        "consolidation": (1 - abs(level)) * ranging_weight,
        "liquidity_sweep": sweep_weight,
    })

    score = clamp(50 + 50 * level, 0.0, 100.0)

    data_completeness = sum(
        1 for f in REQUIRED_FEATURES if features.get(f) is not None
    ) / len(REQUIRED_FEATURES)
    choc_penalty = CHOC_CONFIDENCE_PENALTY if choc not in (None, 0.0) else 0.0
    confidence = clamp(0.2 + 0.6 * data_completeness - choc_penalty, 0.0, 1.0)
    if choc not in (None, 0.0):
        contributions.append(Contribution(
            feature="ms_change_of_character", value=choc, weight=0.1,
            effect="structure may be invalidating",
        ))

    dominant = max(states, key=lambda s: states[s])
    reasoning = [
        f"Structural bias {level:+.2f}, {'confirmed trend' if is_trending else 'ranging'} "
        f"({data_completeness:.0%} of inputs available).",
        f"Sweep probability {sweep_prob if sweep_prob is not None else 'n/a'}, "
        f"breakout probability {breakout_prob if breakout_prob is not None else 'n/a'}.",
        f"Dominant state: {dominant}."
        + (" Change of character detected — read may be invalidating." if choc_penalty else ""),
    ]

    return IntelligenceResult(
        component=COMPONENT,
        score=score,
        confidence=confidence,
        states=states,
        metrics={
            "structural_bias": round(level, 4) if bias is not None else None,
            "trend_direction": trend_dir,
            "breakout_probability": breakout_prob,
            "sweep_probability": sweep_prob,
            "change_of_character": choc,
        },
        contributions=contributions,
        reasoning=reasoning,
    )


class MarketStructureIntelligenceEngine(IntelligenceComponent):
    name = "market_structure_intelligence"

    async def assess(
        self, symbol: str | None = None, timeframe: str = "D"
    ) -> IntelligenceResult:
        symbol = symbol or self._settings.feature_benchmark_symbol
        features = await self.latest_values(symbol, timeframe)
        result = assess_market_structure(features)
        result.metrics["symbol"] = symbol
        return result
