"""Options Intelligence Engine.

Consumes the options feature snapshot (`OptionsFeatureEngine`, "chain"
timeframe) and turns it into a standard `IntelligenceResult`, the missing
tier between the options feature columns and every prediction/decision
consumer (`EnsemblePredictionEngine`, `ConvictionEngine`,
`CompositeMarketIntelligenceEngine`) -- mirroring the shape every other
Volume 4 domain already has (breadth, liquidity, institutional flow, ...).

Directional convention (a documented v1 heuristic, same honest-approximation
spirit as `features/options.py`'s own dealer-positioning heuristic and
`conviction.py`'s sector-strength stand-in -- not asserted as definitive
options-trading wisdom):

- `options_dealer_positioning` is used as-is (already -1..1, positive =
  put-writing dominance = "professional money selling downside" per its own
  docstring = structurally supportive = bullish).
- `options_pcr`: low PCR (call-heavy) leans bullish, high PCR (put-heavy)
  leans bearish -- the conventional (non-contrarian) reading. PCR=1 is
  treated as neutral.
- `options_max_pain_distance_pct`: positive (max pain above spot) is an
  "upward pull into expiry" per its own docstring -- treated as a mild
  bullish lean, and vice versa.
- `options_call_writing_score`/`options_put_writing_score` are NOT counted
  as a second, separate directional term -- `options_dealer_positioning` is
  already derived from exactly this pair (`features/options.py:186-188`),
  so adding both would double-weight the same underlying signal. They still
  surface in `contributions`/`reasoning` for explainability.
- `options_atm_iv`/`options_iv_rank` are magnitude (volatility level), not
  direction -- they feed `states` (elevated/compressed IV) and confidence,
  never the bull/bear level.
- `options_gamma_exposure` is deliberately excluded from the directional
  level: net gamma exposure signals whether dealer hedging is likely to
  amplify or dampen the next move (a volatility-character question), not
  which direction the market leans -- forcing a bull/bear sign onto it would
  overreach what the collector's own proxy actually measures.
"""

from collections.abc import Mapping

from app.intelligence.base import (
    Contribution,
    IntelligenceComponent,
    IntelligenceResult,
    clamp,
    normalize_states,
)

COMPONENT = "options"
OPTIONS_TIMEFRAME = "chain"

# Heuristic scales (tunable once real history shows typical magnitudes for
# this universe) -- same spirit as breadth.py's MOMENTUM_SIGNAL_SCALE.
PCR_NEUTRAL = 1.0
PCR_SCALE = 1.0  # PCR of (neutral +/- 1.0) saturates the PCR signal
MAX_PAIN_SCALE = 10.0  # 10% max-pain distance saturates that signal

DEALER_POSITIONING_WEIGHT = 0.40
PCR_WEIGHT = 0.30
MAX_PAIN_WEIGHT = 0.30

IV_RANK_HIGH = 70.0  # iv_rank at/above this reads as "elevated_iv"
IV_RANK_LOW = 30.0  # iv_rank at/below this reads as "compressed_iv"


def assess_options(features: Mapping[str, float]) -> IntelligenceResult:
    """Pure options assessment from the latest chain-snapshot feature values."""
    contributions: list[Contribution] = []
    reasoning: list[str] = []
    level_terms: list[tuple[float, float]] = []

    dealer_positioning = features.get("options_dealer_positioning")
    if dealer_positioning is not None:
        signal = clamp(dealer_positioning, -1.0, 1.0)
        level_terms.append((signal, DEALER_POSITIONING_WEIGHT))
        contributions.append(Contribution(
            feature="options_dealer_positioning", value=dealer_positioning,
            weight=DEALER_POSITIONING_WEIGHT,
            effect="put-writing dominant (supportive)" if dealer_positioning > 0
            else "call-writing dominant (resistance)",
        ))

    pcr = features.get("options_pcr")
    if pcr is not None:
        signal = clamp((PCR_NEUTRAL - pcr) / PCR_SCALE, -1.0, 1.0)
        level_terms.append((signal, PCR_WEIGHT))
        contributions.append(Contribution(
            feature="options_pcr", value=pcr, weight=PCR_WEIGHT,
            effect="call-heavy (bullish tilt)" if pcr < PCR_NEUTRAL
            else "put-heavy (bearish tilt)",
        ))

    max_pain_distance = features.get("options_max_pain_distance_pct")
    if max_pain_distance is not None:
        signal = clamp(max_pain_distance / MAX_PAIN_SCALE, -1.0, 1.0)
        level_terms.append((signal, MAX_PAIN_WEIGHT))
        contributions.append(Contribution(
            feature="options_max_pain_distance_pct", value=max_pain_distance,
            weight=MAX_PAIN_WEIGHT,
            effect="max pain above spot (upward pull)" if max_pain_distance > 0
            else "max pain below spot (downward pull)",
        ))

    # Explainability only -- not double-counted into level_terms (see module
    # docstring: dealer_positioning already derives from this exact pair).
    call_writing = features.get("options_call_writing_score")
    put_writing = features.get("options_put_writing_score")
    if call_writing is not None and put_writing is not None:
        contributions.append(Contribution(
            feature="options_call_put_writing", value=put_writing - call_writing,
            weight=0.0,
            effect="put writers busier" if put_writing > call_writing else "call writers busier",
        ))

    total_weight = sum(w for _, w in level_terms)
    level = sum(v * w for v, w in level_terms) / total_weight if total_weight > 0 else 0.0
    level = clamp(level, -1.0, 1.0)

    atm_iv = features.get("options_atm_iv")
    iv_rank = features.get("options_iv_rank")
    if iv_rank is not None:
        contributions.append(Contribution(
            feature="options_iv_rank", value=iv_rank, weight=0.0,
            effect="elevated IV" if iv_rank >= IV_RANK_HIGH
            else ("compressed IV" if iv_rank <= IV_RANK_LOW else "mid-range IV"),
        ))

    data_completeness = len(level_terms) / 3.0
    confidence = clamp(0.3 + 0.5 * data_completeness + (0.2 if iv_rank is not None else 0.0), 0.0, 1.0)

    # elevated_iv/compressed_iv are scaled from the midpoint (iv_rank=50),
    # not raw iv_rank/(1-iv_rank) -- those two would always sum to exactly
    # 1.0 regardless of how extreme IV actually was, which would dilute the
    # directional states even when IV sits at a totally unremarkable
    # mid-range reading. Both are 0 at iv_rank=50 and mutually exclusive.
    elevated_iv = max((iv_rank / 100.0 - 0.5) * 2, 0.0) if iv_rank is not None else 0.0
    compressed_iv = max((0.5 - iv_rank / 100.0) * 2, 0.0) if iv_rank is not None else 0.0
    states = normalize_states({
        "bullish_positioning": max(level, 0.0),
        "bearish_positioning": max(-level, 0.0),
        "elevated_iv": elevated_iv,
        "compressed_iv": compressed_iv,
        "mixed": 1 - abs(level),
    })

    score = clamp(50 + 50 * level)
    dominant = max(states, key=lambda s: states[s]) if states else "unknown"
    reasoning.append(
        f"Options level {level:+.2f} from {len(level_terms)}/3 available signal groups."
    )
    if atm_iv is not None:
        reasoning.append(
            f"ATM IV {atm_iv:.1f}%"
            + (f", IV rank {iv_rank:.0f}/100" if iv_rank is not None else "") + "."
        )
    reasoning.append(f"Dominant state: {dominant}.")

    return IntelligenceResult(
        component=COMPONENT,
        score=score,
        confidence=confidence,
        states=states,
        metrics={
            "options_level": round(level, 4),
            "atm_iv": round(atm_iv, 4) if atm_iv is not None else None,
            "iv_rank": round(iv_rank, 4) if iv_rank is not None else None,
            "dealer_positioning": round(dealer_positioning, 4) if dealer_positioning is not None else None,
        },
        contributions=contributions,
        reasoning=reasoning,
    )


class OptionsIntelligenceEngine(IntelligenceComponent):
    """Options is inherently per-instrument (like Liquidity), not market-wide
    (like Breadth) -- `assess()` defaults to the benchmark symbol so it keeps
    the same zero-argument-callable shape every other component has, but
    pass a symbol the options collector actually tracks (per
    `Settings.watchlist`) for a meaningful read."""

    name = "options_intelligence"

    async def assess(self, symbol: str | None = None) -> IntelligenceResult:
        symbol = symbol or self._settings.feature_benchmark_symbol
        features = await self.latest_values(symbol, OPTIONS_TIMEFRAME)
        result = assess_options(features)
        result.metrics["symbol"] = symbol
        await self._publish_assessment(symbol, result)
        return result
