"""Market Confidence Engine (Volume 4, Prompt 4.13).

The first component whose job is to synthesize ACROSS several other
components already built, rather than read one feature domain: Instead of
trusting every signal equally, this measures confidence in the platform's
own market assessment.

Maps the prompt's six named inputs onto what's already built:
- Data Quality       -> mean FeatureQualityRow.quality_score (Volume 3,
                        Prompt 3.14) — are values within expected ranges.
- Feature Quality    -> 1 - FeatureDriftRow breach rate (Prompt 3.15) — has
                        the underlying distribution stayed stable. Genuinely
                        distinct from Data Quality: a feature can look
                        statistically reasonable right now while its
                        distribution has quietly drifted from what it used
                        to be, or vice versa.
- Regime Certainty   -> 1 - Regime Transition Detection's Instability Score
                        (Prompt 4.12) — how settled the current regime read is.
- Breadth            -> Breadth Intelligence's own confidence (Prompt 4.3).
- Institutional Agreement -> Institutional Flow Intelligence's own
                        confidence (Prompt 4.5), which already blends
                        component sign-agreement into its formula.
- Correlation Stability   -> Correlation Intelligence's own stability metric
                        (Prompt 4.8) — an exact, direct passthrough.

- IntelligenceResult.score      -> Market Confidence Score (0-100)
- metrics["confidence_grade"]   -> Confidence Grade (A-F letter)
- metrics["confidence_trend"]   -> Confidence Trend (improving/declining/
                                    stable), from persisted score history —
                                    the same append-only market_events
                                    pattern Bayesian Regime Detection uses,
                                    since a trend needs continuity no single
                                    snapshot can provide.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from statistics import fmean

from app.core.cache import CacheService
from app.core.config import Settings
from app.events.bus import EventBus
from app.intelligence.base import (
    Contribution,
    IntelligenceComponent,
    IntelligenceResult,
    SessionFactory,
    clamp,
    normalize_states,
    slope,
)
from app.intelligence.breadth import BreadthIntelligenceEngine
from app.intelligence.correlation import CorrelationIntelligenceEngine
from app.intelligence.institutional_flow import InstitutionalFlowIntelligenceEngine
from app.intelligence.transitions import RegimeTransitionEngine

COMPONENT = "market_confidence"
CONFIDENCE_EVENT_TYPE = "market_confidence.observation"
CONFIDENCE_SOURCE = "market_confidence_engine"

REQUIRED_INPUTS: tuple[str, ...] = (
    "data_quality", "feature_quality", "regime_certainty",
    "breadth", "institutional_agreement", "correlation_stability",
)

QUALITY_SAMPLE_LIMIT = 200
DRIFT_SAMPLE_LIMIT = 200
SCORE_HISTORY_LIMIT = 20
# Score-history points at which the trend read is considered fully backed —
# a heuristic scale, same spirit as elsewhere in this layer.
HISTORY_TARGET = 5
# Slope (score points per observation) below which the trend reads "stable"
# rather than improving/declining.
TREND_EPSILON = 1.0

GRADE_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (80.0, "A"), (65.0, "B"), (50.0, "C"), (35.0, "D"),
)

LEVEL_ANCHORS: dict[str, float] = {
    "low_confidence": 0.0, "moderate_confidence": 0.5, "high_confidence": 1.0,
}
LEVEL_BAND = 0.4


def _grade(score: float) -> str:
    for threshold, letter in GRADE_THRESHOLDS:
        if score >= threshold:
            return letter
    return "F"


def _level_weights(level: float) -> dict[str, float]:
    return {
        name: max(0.0, 1 - abs(level - anchor) / LEVEL_BAND)
        for name, anchor in LEVEL_ANCHORS.items()
    }


def assess_market_confidence(
    inputs: Mapping[str, float | None],
    score_history: Sequence[float] = (),
) -> IntelligenceResult:
    """Pure confidence synthesis from the six named inputs (each already a
    0-1 signal, or None if unavailable) and past Market Confidence scores."""
    contributions: list[Contribution] = []
    present = {k: v for k, v in inputs.items() if v is not None}

    score = 100 * fmean(present.values()) if present else 50.0
    for name in REQUIRED_INPUTS:
        value = inputs.get(name)
        if value is not None:
            contributions.append(Contribution(
                feature=name, value=value, weight=1 / len(REQUIRED_INPUTS),
                effect="supportive" if value >= 0.5 else "weak",
            ))

    data_completeness = len(present) / len(REQUIRED_INPUTS)
    grade = _grade(score)

    trend_series = [*score_history, score]
    trend_slope = slope(trend_series) if len(trend_series) >= 2 else 0.0
    if trend_slope > TREND_EPSILON:
        trend = "improving"
    elif trend_slope < -TREND_EPSILON:
        trend = "declining"
    else:
        trend = "stable"

    history_sufficiency = clamp(len(score_history) / HISTORY_TARGET, 0.0, 1.0)
    confidence = clamp(0.6 * data_completeness + 0.4 * history_sufficiency, 0.0, 1.0)

    level = clamp(score / 100, 0.0, 1.0)
    states = normalize_states(_level_weights(level))
    dominant = max(states, key=lambda s: states[s])

    reasoning = [
        f"{len(present)}/{len(REQUIRED_INPUTS)} input(s) available; "
        f"grade {grade} ({score:.0f}/100).",
        f"Trend {trend} (slope {trend_slope:+.2f}/observation over "
        f"{len(score_history)} prior reading(s)).",
        f"Dominant state: {dominant}.",
    ]

    return IntelligenceResult(
        component=COMPONENT,
        score=round(score, 4),
        confidence=confidence,
        states=states,
        metrics={
            "confidence_grade": grade,
            "confidence_trend": trend,
            "trend_slope": round(trend_slope, 4),
            "data_completeness": round(data_completeness, 4),
            **{name: inputs.get(name) for name in REQUIRED_INPUTS},
        },
        contributions=contributions,
        reasoning=reasoning,
    )


class MarketConfidenceEngine(IntelligenceComponent):
    name = "market_confidence_engine"

    def __init__(
        self,
        session_factory: SessionFactory | None = None,
        cache: CacheService | None = None,
        settings: Settings | None = None,
        bus: EventBus | None = None,
        regime_transition_engine: RegimeTransitionEngine | None = None,
        breadth_engine: BreadthIntelligenceEngine | None = None,
        institutional_flow_engine: InstitutionalFlowIntelligenceEngine | None = None,
        correlation_engine: CorrelationIntelligenceEngine | None = None,
    ) -> None:
        super().__init__(session_factory=session_factory, cache=cache, settings=settings, bus=bus)
        self._regime_transitions = regime_transition_engine or RegimeTransitionEngine(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )
        self._breadth = breadth_engine or BreadthIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )
        self._institutional_flow = institutional_flow_engine or InstitutionalFlowIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )
        self._correlation = correlation_engine or CorrelationIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )

    async def assess(self, symbol: str | None = None) -> IntelligenceResult:
        symbol = symbol or self._settings.feature_benchmark_symbol

        data_quality = await self._data_quality()
        feature_quality = await self._feature_quality()

        regime = await self._regime_transitions.assess(symbol=symbol)
        regime_certainty = clamp(1 - regime.score / 100, 0.0, 1.0)

        breadth = await self._breadth.assess()

        flow = await self._institutional_flow.assess()

        correlation = await self._correlation.assess()
        correlation_stability = correlation.metrics.get("correlation_stability")

        inputs: dict[str, float | None] = {
            "data_quality": data_quality,
            "feature_quality": feature_quality,
            "regime_certainty": regime_certainty,
            "breadth": breadth.confidence,
            "institutional_agreement": flow.confidence,
            "correlation_stability": correlation_stability,
        }

        history = await self._load_score_history(symbol)
        result = assess_market_confidence(inputs, history)
        await self._store_score(symbol, result.score)
        result.metrics["symbol"] = symbol
        await self._publish_assessment(symbol, result)
        return result

    async def _data_quality(self) -> float | None:
        if self._sessions is None:
            return None
        from sqlalchemy import desc, select

        from app.database.tables import FeatureQualityRow

        async with self._sessions() as session:
            result = await session.execute(
                select(FeatureQualityRow.quality_score)
                .order_by(desc(FeatureQualityRow.id))
                .limit(QUALITY_SAMPLE_LIMIT)
            )
            scores = result.scalars().all()
        if not scores:
            return None
        return clamp(fmean(scores) / 100, 0.0, 1.0)

    async def _feature_quality(self) -> float | None:
        if self._sessions is None:
            return None
        from sqlalchemy import desc, select

        from app.database.tables import FeatureDriftRow

        async with self._sessions() as session:
            result = await session.execute(
                select(FeatureDriftRow.breached)
                .order_by(desc(FeatureDriftRow.id))
                .limit(DRIFT_SAMPLE_LIMIT)
            )
            breaches = result.scalars().all()
        if not breaches:
            return None
        breach_rate = sum(1 for b in breaches if b) / len(breaches)
        return clamp(1 - breach_rate, 0.0, 1.0)

    async def _load_score_history(
        self, symbol: str, limit: int = SCORE_HISTORY_LIMIT
    ) -> list[float]:
        if self._sessions is None:
            return []
        from sqlalchemy import desc, select

        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            result = await session.execute(
                select(MarketEvent.data)
                .where(
                    MarketEvent.event_type == CONFIDENCE_EVENT_TYPE,
                    MarketEvent.source == CONFIDENCE_SOURCE,
                    MarketEvent.data["symbol"].astext == symbol,
                )
                .order_by(desc(MarketEvent.id))
                .limit(limit)
            )
            rows = result.scalars().all()
        return [row["score"] for row in reversed(rows) if row.get("score") is not None]

    async def _store_score(self, symbol: str, score: float) -> None:
        if self._sessions is None:
            return
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            session.add(MarketEvent(
                event_type=CONFIDENCE_EVENT_TYPE,
                source=CONFIDENCE_SOURCE,
                data={
                    "symbol": symbol,
                    "score": score,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            ))
            await session.commit()
