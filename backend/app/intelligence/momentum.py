"""Momentum Intelligence Engine (Volume 4, Chapter 3's Momentum dimension).

Previously momentum only existed embedded inside other components (trend.py
blends a momentum ladder into its own structural-bias score; breadth.py has
its own separate breadth-momentum ladder) -- there was no standalone engine
for the dimension Chapter 3 names in its own right. This engine is that
missing piece: a pure read of the price-momentum ladder (`price.py`'s
`price_momentum_{w}`/`price_acceleration_{w}` features, now normalized --
see `price.py`'s `_z` companions, added the same day as this engine) turned
into its own IntelligenceResult, focused on questions trend.py's blended
score doesn't answer on its own:

- Is momentum currently building or fading (`price_acceleration_*`), not
  just which direction it points?
- Is the current reading unusually extreme relative to its own trailing
  history (`price_momentum_*_z`), which trend.py's raw-value blend has no
  way to express -- a momentum reading can point strongly bullish while
  still being unremarkable for that instrument, or can be middling in
  absolute terms while being a genuine multi-sigma extreme.

Reuses trend.py's own MOMENTUM_WINDOWS/MOMENTUM_SCALE (same underlying
feature names, same tanh saturation calibration) rather than inventing a
second, inconsistent scale for the same raw numbers.
"""

import math
from collections.abc import Mapping

from app.intelligence.base import (
    Contribution,
    IntelligenceComponent,
    IntelligenceResult,
    clamp,
    intraday_direction_signal,
    normalize_states,
)
from app.intelligence.base import sign as _sign

COMPONENT = "momentum"

MOMENTUM_WINDOWS = (5, 20, 50, 200)
MOMENTUM_SCALE = {5: 3.0, 20: 6.0, 50: 10.0, 200: 20.0}
# Acceleration (% points/bar) that saturates the building/fading signal --
# a heuristic scale, same spirit as elsewhere in this layer.
ACCELERATION_SCALE = {5: 1.5, 20: 3.0, 50: 5.0, 200: 10.0}
# |z-score| at/above this reads as an "extreme" momentum reading.
EXTREME_Z_THRESHOLD = 2.0
# Same intraday-overlay convention as trend.py/structure.py (DEBT-1/DEBT-2,
# 2026-07-16) -- today's move-from-open reads as the "window 0" momentum
# term, the freshest possible reading in this ladder.
INTRADAY_DIRECTION_WEIGHT = 0.3
INTRADAY_CONFLICT_CONFIDENCE_PENALTY = 0.3


def assess_momentum(
    features: Mapping[str, float],
    intraday_features: Mapping[str, float] | None = None,
) -> IntelligenceResult:
    """Pure momentum assessment from the latest feature values.

    ``intraday_features`` is optional and additive (DEBT-1/DEBT-2): absent,
    every calculation is byte-identical to before this parameter existed.
    """
    contributions: list[Contribution] = []
    reasoning: list[str] = []

    momentum_signals: list[float] = []
    acceleration_signals: list[float] = []
    z_scores: list[float] = []

    for window in MOMENTUM_WINDOWS:
        momentum = features.get(f"price_momentum_{window}")
        if momentum is not None:
            signal = math.tanh(momentum / MOMENTUM_SCALE[window])
            momentum_signals.append(signal)
            contributions.append(Contribution(
                feature=f"price_momentum_{window}", value=momentum,
                weight=1 / len(MOMENTUM_WINDOWS),
                effect="bullish momentum" if signal > 0 else "bearish momentum",
            ))

        acceleration = features.get(f"price_acceleration_{window}")
        if acceleration is not None:
            acc_signal = math.tanh(acceleration / ACCELERATION_SCALE[window])
            acceleration_signals.append(acc_signal)
            contributions.append(Contribution(
                feature=f"price_acceleration_{window}", value=acceleration, weight=0.0,
                effect="building" if acc_signal > 0 else "fading",
            ))

        z = features.get(f"price_momentum_{window}_z")
        if z is not None:
            z_scores.append(z)

    d_level = (
        sum(momentum_signals) / len(momentum_signals) if momentum_signals else 0.0
    )

    intraday_dir = intraday_direction_signal(intraday_features)
    intraday_conflict = 0.0
    if intraday_dir is not None:
        level = (1 - INTRADAY_DIRECTION_WEIGHT) * d_level + INTRADAY_DIRECTION_WEIGHT * intraday_dir
        contributions.append(Contribution(
            feature="intraday_move_from_open_pct",
            value=(intraday_features or {}).get("intraday_move_from_open_pct"),
            weight=INTRADAY_DIRECTION_WEIGHT,
            effect="bullish today" if intraday_dir > 0 else
                   ("bearish today" if intraday_dir < 0 else "flat today"),
        ))
        intraday_conflict = max(0.0, -intraday_dir * _sign(d_level))
    else:
        level = d_level
    level = clamp(level, -1.0, 1.0)

    acceleration_mean = (
        sum(acceleration_signals) / len(acceleration_signals) if acceleration_signals else 0.0
    )

    max_abs_z = max((abs(z) for z in z_scores), default=0.0)
    is_extreme = max_abs_z >= EXTREME_Z_THRESHOLD

    data_completeness = len(momentum_signals) / len(MOMENTUM_WINDOWS)
    sign_agreement = 0.0
    if momentum_signals:
        dominant_sign = 1 if level >= 0 else -1
        sign_agreement = sum(
            1 for s in momentum_signals if (1 if s >= 0 else -1) == dominant_sign
        ) / len(momentum_signals)
    confidence = clamp(
        0.4 * data_completeness + 0.4 * sign_agreement + 0.2 * bool(z_scores)
        - INTRADAY_CONFLICT_CONFIDENCE_PENALTY * intraday_conflict,
        0.0, 1.0,
    )
    if intraday_conflict > 0:
        reasoning.append(
            f"Today's intraday move opposes the underlying read "
            f"(conflict {intraday_conflict:.0%}) -- confidence docked."
        )

    # "Accelerating" means the move is INTENSIFYING: bullish momentum getting
    # more positive, or bearish momentum getting more negative -- level and
    # acceleration share the same sign. "Decelerating" is the opposite sign
    # pairing: the move is losing steam regardless of which direction it's
    # losing steam from.
    states = normalize_states({
        "accelerating_bullish": max(level, 0.0) * max(acceleration_mean, 0.0),
        "accelerating_bearish": max(-level, 0.0) * max(-acceleration_mean, 0.0),
        "decelerating": (
            max(level, 0.0) * max(-acceleration_mean, 0.0)
            + max(-level, 0.0) * max(acceleration_mean, 0.0)
        ),
        "extreme": abs(level) * (1.0 if is_extreme else 0.0),
        "mixed": (1 - abs(level)) * (1 - abs(acceleration_mean)),
    })

    score = clamp(50 + 50 * level)
    dominant = max(states, key=lambda s: states[s]) if states else "unknown"
    accel_state = (
        "building" if acceleration_mean > 0 else "fading" if acceleration_mean < 0 else "flat"
    )
    reasoning.append(
        f"Momentum level {level:+.2f} from {len(momentum_signals)}/{len(MOMENTUM_WINDOWS)} "
        f"windows; acceleration {acceleration_mean:+.2f} ({accel_state})."
    )
    if z_scores:
        reasoning.append(
            f"Max |z-score| {max_abs_z:.2f} across {len(z_scores)} windows"
            + (" -- an extreme reading relative to trailing history." if is_extreme else ".")
        )
    reasoning.append(f"Dominant state: {dominant}.")
    if intraday_dir is not None:
        move = (intraday_features or {}).get("intraday_move_from_open_pct")
        reasoning.append(
            f"Today's session move {move:+.2f}% from open "
            f"(intraday signal {intraday_dir:+.2f})."
        )

    return IntelligenceResult(
        component=COMPONENT,
        score=score,
        confidence=confidence,
        states=states,
        metrics={
            "momentum_level": round(level, 4),
            "acceleration": round(acceleration_mean, 4),
            "max_abs_z": round(max_abs_z, 4) if z_scores else None,
            "is_extreme": is_extreme,
            "intraday_direction": round(intraday_dir, 4) if intraday_dir is not None else None,
        },
        contributions=contributions,
        reasoning=reasoning,
    )


class MomentumIntelligenceEngine(IntelligenceComponent):
    """Momentum is per-instrument, like Liquidity/Market Structure -- not
    market-wide like Breadth. assess() defaults to the benchmark symbol,
    pass a tradable symbol for a meaningful read."""

    name = "momentum_intelligence"

    async def assess(self, symbol: str | None = None, timeframe: str = "D") -> IntelligenceResult:
        symbol = symbol or self._settings.feature_benchmark_symbol
        features = await self.latest_values(symbol, timeframe)
        intraday_features = await self.intraday_values(symbol) if timeframe == "D" else None
        result = assess_momentum(features, intraday_features)
        result.metrics["symbol"] = symbol
        await self._publish_assessment(symbol, result)
        return result
