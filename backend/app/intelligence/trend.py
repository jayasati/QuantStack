"""Trend Intelligence Engine (Volume 4, Prompt 4.1).

Consumes the benchmark's price-momentum ladder (the multi-window momentum
features stand in for moving-average slope and ADX, which the feature store
expresses as momentum/acceleration), the market-structure features (swing
trend, higher highs / lower lows, structural bias, break of structure), and
volume confirmation when the instrument carries volume (indices do not — the
contribution is skipped, never fabricated).

Outputs the full trend picture: direction (-1..1), strength, age (bars since
the swing trend last flipped), stability, confidence, exhaustion,
acceleration — plus probabilistic trend-regime states from Chapter 4's
taxonomy and a normalized 0-100 Trend Score (50 = no trend, >50 bullish).
"""

import asyncio
import math
from collections.abc import Mapping, Sequence

from app.intelligence.base import (
    Contribution,
    IntelligenceComponent,
    IntelligenceResult,
    clamp,
    normalize_states,
)

COMPONENT = "trend"

MOMENTUM_WINDOWS = (5, 20, 50, 200)
# Momentum (% over window) that saturates the direction signal per window.
MOMENTUM_SCALE = {5: 3.0, 20: 6.0, 50: 10.0, 200: 20.0}
TREND_AGE_SATURATION_BARS = 60
STABILITY_LOOKBACK = 20


def _sign(value: float) -> float:
    return 1.0 if value > 0 else (-1.0 if value < 0 else 0.0)


def assess_trend(
    features: Mapping[str, float],
    direction_history: Sequence[float] = (),
) -> IntelligenceResult:
    """Pure trend assessment from the latest feature values."""
    contributions: list[Contribution] = []
    reasoning: list[str] = []

    momentum_signals: list[float] = []
    for window in MOMENTUM_WINDOWS:
        momentum = features.get(f"price_momentum_{window}")
        if momentum is None:
            continue
        signal = math.tanh(momentum / MOMENTUM_SCALE[window])
        momentum_signals.append(signal)
        contributions.append(Contribution(
            feature=f"price_momentum_{window}", value=momentum, weight=0.5 / 4,
            effect="bullish" if signal > 0 else "bearish",
        ))

    structure_direction = features.get("ms_trend_direction")
    structural_bias = features.get("ms_structural_bias")
    if structure_direction is not None:
        contributions.append(Contribution(
            feature="ms_trend_direction", value=structure_direction, weight=0.3,
            effect="bullish" if structure_direction > 0 else
                   ("bearish" if structure_direction < 0 else "neutral"),
        ))
    if structural_bias is not None:
        contributions.append(Contribution(
            feature="ms_structural_bias", value=structural_bias, weight=0.2,
            effect="bullish" if structural_bias > 0 else "bearish",
        ))

    momentum_mean = (
        sum(momentum_signals) / len(momentum_signals) if momentum_signals else 0.0
    )
    direction = (
        0.5 * momentum_mean
        + 0.3 * (structure_direction or 0.0)
        + 0.2 * (structural_bias or 0.0)
    )
    direction = max(-1.0, min(1.0, direction))
    strength = min(
        1.0,
        0.7 * (sum(abs(s) for s in momentum_signals) / len(momentum_signals)
               if momentum_signals else 0.0)
        + 0.3 * abs(structural_bias or 0.0),
    )

    # Age: consecutive bars of the current swing-trend sign.
    age = 0
    current_sign = _sign(direction_history[-1]) if direction_history else 0.0
    if current_sign != 0:
        for value in reversed(direction_history):
            if _sign(value) == current_sign:
                age += 1
            else:
                break
    recent = list(direction_history[-STABILITY_LOOKBACK:])
    stability = (
        sum(1 for v in recent if _sign(v) == current_sign) / len(recent)
        if recent and current_sign != 0
        else 0.0
    )

    acceleration_raw = features.get("price_acceleration_20")
    acceleration = math.tanh((acceleration_raw or 0.0) / 2.0)
    if acceleration_raw is not None:
        contributions.append(Contribution(
            feature="price_acceleration_20", value=acceleration_raw, weight=0.1,
            effect="accelerating" if acceleration * direction > 0 else "decelerating",
        ))

    # Exhaustion: old trend, decelerating against its direction, at its extreme.
    age_fraction = min(age / TREND_AGE_SATURATION_BARS, 1.0)
    opposing = max(0.0, -_sign(direction) * acceleration)
    if direction >= 0:
        distance = features.get("price_dist_from_high_50")
        proximity = 1 - min(abs(distance), 5.0) / 5.0 if distance is not None else 0.0
    else:
        distance = features.get("price_dist_from_low_50")
        proximity = 1 - min(abs(distance), 5.0) / 5.0 if distance is not None else 0.0
    exhaustion = min(1.0, 0.4 * age_fraction + 0.4 * opposing + 0.2 * proximity * opposing)

    # Volume confirmation only when the instrument reports volume.
    volume_confirmation = None
    rvol = features.get("volume_rvol_20")
    obv_z = features.get("volume_obv_z")
    if rvol is not None or obv_z is not None:
        pieces = []
        if rvol is not None:
            pieces.append(min(rvol / 1.5, 1.0))
        if obv_z is not None:
            pieces.append(max(0.0, min(1.0, 0.5 + 0.5 * _sign(direction) * math.tanh(obv_z))))
        volume_confirmation = sum(pieces) / len(pieces)
        contributions.append(Contribution(
            feature="volume_rvol_20" if rvol is not None else "volume_obv_z",
            value=rvol if rvol is not None else obv_z,
            weight=0.1, effect="confirms trend" if volume_confirmation > 0.5 else "weak volume",
        ))
    else:
        reasoning.append("No volume data for this instrument; volume confirmation skipped.")

    # Confidence: agreement of momentum signs with the direction, plus stability.
    if momentum_signals and direction != 0:
        agreement = sum(
            1 for s in momentum_signals if _sign(s) == _sign(direction)
        ) / len(momentum_signals)
    else:
        agreement = 0.0
    confidence = 0.25 + 0.45 * agreement + 0.2 * stability
    if volume_confirmation is not None:
        confidence += 0.1 * volume_confirmation
    confidence = max(0.0, min(1.0, confidence))

    breakout_probability = features.get("ms_breakout_probability") or 0.0
    bos = features.get("ms_break_of_structure") or 0.0

    bull = max(direction, 0.0)
    bear = max(-direction, 0.0)
    ranging = (1 - abs(direction)) * (1 - strength)
    states = normalize_states({
        "strong_bull_trend": bull * strength,
        "weak_bull_trend": bull * (1 - strength),
        "strong_bear_trend": bear * strength,
        "weak_bear_trend": bear * (1 - strength),
        "range_bound": ranging,
        "breakout": max(bos, 0.0) * breakout_probability,
        "breakdown": max(-bos, 0.0) * breakout_probability,
        "transition": (1 - stability) * abs(direction) * 0.5,
    })

    score = clamp(50 + 50 * direction)
    reasoning.extend([
        f"Momentum ladder mean signal {momentum_mean:+.2f} across "
        f"{len(momentum_signals)} windows; swing structure "
        f"{'agrees' if _sign(structure_direction or 0) == _sign(direction) else 'differs'}.",
        f"Trend age {age} bars with {stability:.0%} directional stability; "
        f"exhaustion {exhaustion:.0%}.",
        f"Dominant state: "
        f"{max(states, key=lambda s: states[s]) if states else 'unknown'}.",
    ])

    return IntelligenceResult(
        component=COMPONENT,
        score=score,
        confidence=confidence,
        states=states,
        metrics={
            "trend_direction": round(direction, 4),
            "trend_strength": round(strength, 4),
            "trend_age_bars": age,
            "trend_stability": round(stability, 4),
            "trend_confidence": round(confidence, 4),
            "trend_exhaustion": round(exhaustion, 4),
            "trend_acceleration": round(acceleration, 4),
            "volume_confirmation": (
                round(volume_confirmation, 4) if volume_confirmation is not None else None
            ),
        },
        contributions=contributions,
        reasoning=reasoning,
    )


class TrendIntelligenceEngine(IntelligenceComponent):
    name = "trend_intelligence"

    async def assess(
        self, symbol: str | None = None, timeframe: str = "D"
    ) -> IntelligenceResult:
        symbol = symbol or self._settings.feature_benchmark_symbol
        features = await self.latest_values(symbol, timeframe)
        direction_history = await self.feature_history(
            "ms_trend_direction", symbol, timeframe
        )
        # Offloaded to a worker thread: pure CPU work, same convention as
        # BaseFeatureEngine.run() (perf-audit-2026-07-14 finding 13 -- py-spy
        # caught this running synchronously on the event loop mid-request).
        result = await asyncio.to_thread(assess_trend, features, direction_history)
        result.metrics["symbol"] = symbol
        await self._publish_assessment(symbol, result)
        return result
