"""Volatility Intelligence Engine (Volume 4, Prompt 4.2).

Consumes the volatility feature ladder (historical/realized volatility,
ATR%, compression, expansion probability, and the VIX realized-vs-implied
distance) across multiple windows and blends them into a single continuous
0-1 volatility level (extremely low -> extreme). That level drives both the
Volatility Score and a probabilistic distribution across Chapter 4's eight
volatility regimes — states are blended, never hard-switched, per Chapter
15's philosophy.
"""

import math
from collections.abc import Mapping
from statistics import pstdev

from app.intelligence.base import (
    Contribution,
    IntelligenceComponent,
    IntelligenceResult,
    clamp,
    normalize_states,
)

COMPONENT = "volatility"

VOL_WINDOWS = (5, 20, 50, 100)

# Anchor positions on the 0 (extremely low) .. 1 (extreme) level axis that
# each regime label is centered on; a level maps to a blended distribution
# across nearby anchors rather than snapping to the nearest one.
LEVEL_ANCHORS: dict[str, float] = {
    "extremely_low": 0.0,
    "low": 0.2,
    "normal": 0.5,
    "elevated": 0.7,
    "high": 0.85,
    "extreme": 1.0,
}
LEVEL_BAND = 0.25
VIX_DISTANCE_SCALE = 10.0  # vol points that saturate the VIX-tilt signal


def _level_weights(level: float) -> dict[str, float]:
    return {
        name: max(0.0, 1 - abs(level - anchor) / LEVEL_BAND)
        for name, anchor in LEVEL_ANCHORS.items()
    }


def _window_values(features: Mapping[str, float], feature: str) -> list[float]:
    return [
        features[f"{feature}_{w}"] for w in VOL_WINDOWS if features.get(f"{feature}_{w}") is not None
    ]


def assess_volatility(features: Mapping[str, float]) -> IntelligenceResult:
    """Pure volatility assessment from the latest feature values."""
    contributions: list[Contribution] = []
    reasoning: list[str] = []

    window_levels: list[float] = []
    for window in VOL_WINDOWS:
        regime = features.get(f"volatility_regime_{window}")
        if regime is None:
            continue
        level = regime / 2.0  # 0 low tercile, 0.5 normal, 1.0 high tercile
        window_levels.append(level)
        contributions.append(Contribution(
            feature=f"volatility_regime_{window}", value=regime, weight=0.6 / len(VOL_WINDOWS),
            effect="elevated" if level > 0.5 else ("low" if level < 0.5 else "normal"),
        ))
    regime_level = sum(window_levels) / len(window_levels) if window_levels else 0.5

    # VIX tilt: realized-minus-implied. Strongly negative (implied >> realized)
    # means the market is pricing more risk than has actually shown up yet —
    # a forward-looking nudge toward higher regimes; strongly positive means
    # realized vol is running hotter than what's currently priced in.
    vix_distances = _window_values(features, "volatility_vix_distance")
    vix_distance = sum(vix_distances) / len(vix_distances) if vix_distances else None
    vix_tilt = 0.0
    if vix_distance is not None:
        vix_tilt = -math.tanh(vix_distance / VIX_DISTANCE_SCALE)
        contributions.append(Contribution(
            feature="volatility_vix_distance", value=vix_distance, weight=0.15,
            effect="implied running hot" if vix_distance < 0 else "realized running hot",
        ))

    level = clamp(
        (0.85 * regime_level + 0.15 * (0.5 + 0.5 * vix_tilt))
        if vix_distance is not None else regime_level,
        0.0, 1.0,
    )

    compressions = _window_values(features, "volatility_compression")
    expansions = _window_values(features, "volatility_expansion_prob")
    compression_probability = sum(compressions) / len(compressions) if compressions else 0.0
    expansion_probability = sum(expansions) / len(expansions) if expansions else 0.0
    if compressions:
        contributions.append(Contribution(
            feature="volatility_compression", value=compression_probability, weight=0.15,
            effect="squeeze building" if compression_probability > 0.5 else "no squeeze",
        ))

    # Compression/expansion are halved before blending: unlike the six level
    # buckets (whose triangular weights are diluted across neighboring
    # anchors, so even a perfect match caps below 1.0), these two are raw
    # 0-1 values with no dilution — left undiluted they would structurally
    # dominate the state distribution any time a squeeze is simply tight,
    # even when the level itself is unambiguous.
    states = normalize_states({
        **_level_weights(level),
        "compression": 0.5 * compression_probability,
        "expansion": 0.5 * expansion_probability,
    })

    hist_vols = _window_values(features, "volatility_hist")
    expected_volatility_pct = sum(hist_vols) / len(hist_vols) if hist_vols else None

    expected_move = next(
        (features[f"volatility_expected_move_{w}"] for w in VOL_WINDOWS
         if features.get(f"volatility_expected_move_{w}") is not None),
        None,
    )

    # Instability of volatility itself: a high vol-of-vol relative to the
    # current level means today's regime read is less trustworthy, even
    # when the level itself looks clear.
    vol_of_vols = _window_values(features, "volatility_of_volatility")
    instability = 0.0
    if vol_of_vols and hist_vols:
        avg_vol_of_vol = sum(vol_of_vols) / len(vol_of_vols)
        avg_hist = sum(hist_vols) / len(hist_vols)
        instability = min(1.0, avg_vol_of_vol / max(avg_hist, 1e-6))

    window_agreement = (
        1 - min(1.0, pstdev(window_levels) * 2) if len(window_levels) > 1
        else (1.0 if window_levels else 0.0)
    )
    data_completeness = len(window_levels) / len(VOL_WINDOWS)
    confidence = clamp(
        0.25
        + 0.35 * window_agreement
        + 0.2 * data_completeness
        + 0.1 * (1.0 if vix_distance is not None else 0.0)
        - 0.15 * instability,
        0.0, 1.0,
    )

    score = clamp(100 * level)

    dominant = max(states, key=lambda s: states[s]) if states else "unknown"
    vix_note = (
        f"; VIX tilt {'up' if vix_tilt > 0 else 'down'} "
        f"({vix_distance:+.2f} vol pts realized-implied)."
        if vix_distance is not None else " (no VIX data)."
    )
    reasoning.extend([
        f"Regime ladder mean level {regime_level:.2f} across {len(window_levels)} windows"
        + vix_note,
        f"Compression {compression_probability:.0%}, expansion probability "
        f"{expansion_probability:.0%}; vol-of-vol instability {instability:.0%}.",
        f"Dominant state: {dominant}.",
    ])

    return IntelligenceResult(
        component=COMPONENT,
        score=score,
        confidence=confidence,
        states=states,
        metrics={
            "volatility_level": round(level, 4),
            "expected_volatility_pct": (
                round(expected_volatility_pct, 4) if expected_volatility_pct is not None else None
            ),
            "expected_move_price": round(expected_move, 4) if expected_move is not None else None,
            "compression_probability": round(compression_probability, 4),
            "expansion_probability": round(expansion_probability, 4),
            "vix_realized_minus_implied": (
                round(vix_distance, 4) if vix_distance is not None else None
            ),
            "vol_of_vol_instability": round(instability, 4),
            "volatility_confidence": round(confidence, 4),
        },
        contributions=contributions,
        reasoning=reasoning,
    )


class VolatilityIntelligenceEngine(IntelligenceComponent):
    name = "volatility_intelligence"

    async def assess(
        self, symbol: str | None = None, timeframe: str = "D"
    ) -> IntelligenceResult:
        symbol = symbol or self._settings.feature_benchmark_symbol
        features = await self.latest_values(symbol, timeframe)
        result = assess_volatility(features)
        result.metrics["symbol"] = symbol
        return result
