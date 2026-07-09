"""Regime Transition Engine (Volume 4, Prompt 4.12).

Analyzes the belief history Bayesian Regime Detection (Prompt 4.11) already
persists for any (component, symbol, timeframe) — the same append-only
market_events rows — to detect whether the regime is actively changing,
not just what it currently is.

Two signals from the recent belief window drive everything:
- How close the current leader and runner-up states are right now
  (a near-tie reads as transition-prone regardless of trend).
- Whether the runner-up's share has been rising (least-squares slope over
  the window) — a leader that's comfortably ahead but losing ground fast
  is a different situation from one holding steady.

- IntelligenceResult.score      -> Instability Score (0-100, magnitude-only,
                                    same convention as Volatility/Liquidity/
                                    Event Intelligence)
- IntelligenceResult.confidence -> data sufficiency (how much belief history
                                    was available to judge the trend from)
- metrics["transition_probability"] -> Transition Probability
- metrics["transition_speed"]       -> Transition Speed (signed slope)
- metrics["confidence_loss"]        -> Confidence Loss (decline in the
                                        belief's own peak certainty)
- metrics["alert"]/["alert_message"] -> fires when Transition Probability
                                        crosses `alert_threshold`
"""

from collections.abc import Mapping, Sequence

from app.intelligence.base import (
    Contribution,
    IntelligenceComponent,
    IntelligenceResult,
    clamp,
    normalize_states,
)
from app.intelligence.regime import BayesianRegimeDetector

COMPONENT = "regime_transition"

DEFAULT_ALERT_THRESHOLD = 0.6
HISTORY_LIMIT = 20
# Number of belief snapshots at which data sufficiency is considered full —
# a heuristic scale, same spirit as elsewhere in this layer.
HISTORY_TARGET = 10
# Runner-up share slope (per snapshot step) that saturates the "rising
# runner-up" signal; transition-speed slope magnitude that saturates the
# instability contribution from speed.
RUNNER_UP_SLOPE_SCALE = 0.05
SPEED_SCALE = 0.1

CLOSENESS_WEIGHT = 0.6
MOMENTUM_WEIGHT = 0.4

LEVEL_ANCHORS: dict[str, float] = {"stable": 0.0, "transitioning": 0.5, "unstable": 1.0}
LEVEL_BAND = 0.4


def _slope(values: Sequence[float]) -> float:
    """Least-squares slope over evenly-spaced steps 0..n-1."""
    n = len(values)
    if n < 2:
        return 0.0
    mean_t = (n - 1) / 2
    mean_v = sum(values) / n
    var_t = sum((t - mean_t) ** 2 for t in range(n))
    if var_t == 0:
        return 0.0
    cov = sum((t - mean_t) * (v - mean_v) for t, v in enumerate(values))
    return cov / var_t


def _level_weights(level: float) -> dict[str, float]:
    return {
        name: max(0.0, 1 - abs(level - anchor) / LEVEL_BAND)
        for name, anchor in LEVEL_ANCHORS.items()
    }


def _empty_result(reason: str) -> IntelligenceResult:
    return IntelligenceResult(
        component=COMPONENT,
        score=0.0,
        confidence=0.1,
        states=normalize_states({"stable": 1.0, "transitioning": 0.0, "unstable": 0.0}),
        metrics={
            "transition_probability": None,
            "transition_speed": None,
            "confidence_loss": None,
            "current_state": None,
            "runner_up_state": None,
            "alert": False,
            "alert_message": None,
        },
        contributions=[],
        reasoning=[reason],
    )


def assess_regime_transition(
    belief_history: Sequence[Mapping[str, float]],
    alert_threshold: float = DEFAULT_ALERT_THRESHOLD,
) -> IntelligenceResult:
    """Pure transition assessment from a chronologically-ordered (oldest
    first) window of posterior belief snapshots for one regime dimension."""
    if len(belief_history) < 2 or not belief_history[-1]:
        return _empty_result("Insufficient belief history to assess transition risk.")

    contributions: list[Contribution] = []
    latest = belief_history[-1]
    ranked = sorted(latest.items(), key=lambda kv: -kv[1])
    current_state, current_share = ranked[0]
    runner_up_state, runner_up_share = ranked[1] if len(ranked) > 1 else (None, 0.0)

    current_state_series = [snap.get(current_state, 0.0) for snap in belief_history]
    runner_up_series = (
        [snap.get(runner_up_state, 0.0) for snap in belief_history]
        if runner_up_state else [0.0] * len(belief_history)
    )
    dominant_share_series = [max(snap.values()) if snap else 0.0 for snap in belief_history]

    transition_speed = _slope(current_state_series)
    runner_up_slope = _slope(runner_up_series)

    closeness = 1 - clamp(current_share - runner_up_share, 0.0, 1.0)
    rising_runner_up = clamp(max(runner_up_slope, 0.0) / RUNNER_UP_SLOPE_SCALE, 0.0, 1.0)
    transition_probability = clamp(
        CLOSENESS_WEIGHT * closeness + MOMENTUM_WEIGHT * rising_runner_up, 0.0, 1.0
    )
    contributions.append(Contribution(
        feature="leader_runner_up_closeness", value=closeness, weight=0.3,
        effect="near-tie" if closeness > 0.5 else "clear leader",
    ))
    contributions.append(Contribution(
        feature="runner_up_momentum", value=runner_up_slope, weight=0.3,
        effect="runner-up rising" if runner_up_slope > 0 else "runner-up fading",
    ))

    confidence_loss = clamp(dominant_share_series[0] - dominant_share_series[-1], 0.0, 1.0)
    contributions.append(Contribution(
        feature="confidence_loss", value=confidence_loss, weight=0.2,
        effect="losing conviction" if confidence_loss > 0.2 else "steady conviction",
    ))

    speed_signal = clamp(abs(transition_speed) / SPEED_SCALE, 0.0, 1.0)
    level = clamp(
        0.4 * transition_probability + 0.3 * speed_signal + 0.3 * confidence_loss, 0.0, 1.0
    )
    instability_score = clamp(100 * level, 0.0, 100.0)

    states = normalize_states(_level_weights(level))
    data_sufficiency = clamp(len(belief_history) / HISTORY_TARGET, 0.0, 1.0)

    alert = transition_probability >= alert_threshold
    alert_message = (
        f"Transition probability {transition_probability:.0%} exceeds threshold "
        f"{alert_threshold:.0%}: {current_state} -> {runner_up_state or 'unclear'}."
        if alert else None
    )

    dominant = max(states, key=lambda s: states[s])
    reasoning = [
        f"Leader '{current_state}' at {current_share:.0%}, runner-up "
        f"'{runner_up_state}' at {runner_up_share:.0%} ({len(belief_history)} snapshots).",
        f"Transition probability {transition_probability:.0%}, speed {transition_speed:+.3f}/step, "
        f"confidence loss {confidence_loss:.0%}.",
        f"Dominant state: {dominant}." + (f" ALERT: {alert_message}" if alert else ""),
    ]

    return IntelligenceResult(
        component=COMPONENT,
        score=instability_score,
        confidence=data_sufficiency,
        states=states,
        metrics={
            "transition_probability": round(transition_probability, 4),
            "transition_speed": round(transition_speed, 4),
            "confidence_loss": round(confidence_loss, 4),
            "current_state": current_state,
            "runner_up_state": runner_up_state,
            "alert": alert,
            "alert_message": alert_message,
        },
        contributions=contributions,
        reasoning=reasoning,
    )


class RegimeTransitionEngine(IntelligenceComponent):
    name = "regime_transition_engine"

    async def assess(
        self,
        component: str = "trend",
        symbol: str | None = None,
        timeframe: str = "D",
        limit: int = HISTORY_LIMIT,
        alert_threshold: float = DEFAULT_ALERT_THRESHOLD,
    ) -> IntelligenceResult:
        symbol = symbol or self._settings.feature_benchmark_symbol
        detector = BayesianRegimeDetector(session_factory=self._sessions, settings=self._settings)
        history = await detector.history(component, symbol, timeframe, limit=limit)
        result = assess_regime_transition(history, alert_threshold=alert_threshold)
        result.metrics["target_component"] = component
        result.metrics["symbol"] = symbol
        result.metrics["timeframe"] = timeframe
        return result
