"""Candidate Generation Engine (Volume 5, Prompt 5.2).

Turns Prompt 5.1's opportunity triggers into the Top-20 ranked trade
candidates the doc specifies, each carrying Instrument/Direction/Reason/
Priority/Supporting Features/Feature Snapshot ID/Estimated Opportunity
Lifetime/Current Market Regime/Market Confidence.

Direction, Current Market Regime, and Supporting Features are all built
directly from the exact IntelligenceResults that triggered the candidate
(OpportunityCandidate.component_results, attached in-memory by Prompt 5.1's
detect() so this engine never needs a second, possibly-inconsistent fetch
of live market data for the same evaluation moment).

Feature Snapshot ID: resolves to a real FeatureSnapshotEngine record (Prompt
5.3, app/prediction/snapshot.py) — every candidate freezes its own feature
values/versions/market report/regime at generation time, addressable and
reconstructible by snapshot_id.

Store candidates independently from predictions: separate MarketEvent
event_type ("trade_candidate.generated") from both opportunity.detected
(Prompt 5.1) and any future prediction_results row (Prompt 5.4+).
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.events.bus import Event, EventBus
from app.intelligence.base import IntelligenceResult
from app.prediction.opportunity import OpportunityCandidate, OpportunityDetectionEngine
from app.prediction.snapshot import FeatureSnapshotEngine

logger = get_logger(__name__)

EVENT_TYPE = "trade_candidate.generated"
MAX_CANDIDATES = 20

DIRECTION_EPSILON = 0.05  # |signal| below this reads as neutral, matching macro.py's convention

# Directional evidence pulled from the exact fields already verified in
# opportunity.py's trigger mapping — (component, metric path, weight source).
_DIRECTION_SIGNALS: tuple[tuple[str, str], ...] = (
    ("trend", "trend_direction"),
    ("market_structure", "structural_bias"),
    ("institutional_flow", "net_flow_level"),
    ("relative_strength", "relative_trend"),
)

# How long a triggered setup is expected to stay valid, in minutes — a
# documented v1 heuristic (matching this codebase's established pattern,
# e.g. volatility.py's expansion_prob), calibratable/replaceable as v2 once
# real outcome data exists. Structural/liquidity triggers decay fast;
# institutional flow develops slowly. event_driven_opportunity is handled
# separately below using the real hours_until_event metric when available.
TRIGGER_LIFETIME_MINUTES: dict[str, float] = {
    "significant_breakout_probability": 4 * 60,
    "liquidity_sweep_detected": 2 * 60,
    "structural_trend_change": 24 * 60,
    "regime_transition": 24 * 60,
    "institutional_accumulation": 3 * 24 * 60,
    "institutional_distribution": 3 * 24 * 60,
    "exceptional_relative_strength": 2 * 24 * 60,
    "high_volatility_expansion": 3 * 60,
}
DEFAULT_LIFETIME_MINUTES = 4 * 60
EVENT_LIFETIME_CAP_MINUTES = 48 * 60


@dataclass(frozen=True)
class SupportingFeature:
    """One piece of evidence backing the candidate."""

    name: str
    value: float


@dataclass
class TradeCandidate:
    instrument: str
    direction: str  # "long" | "short" | "neutral"
    reason: str
    priority: int  # rank within this batch, 1 = highest
    priority_score: float
    supporting_features: list[SupportingFeature] = field(default_factory=list)
    feature_snapshot_id: str = ""
    estimated_lifetime_minutes: float = DEFAULT_LIFETIME_MINUTES
    current_market_regime: dict[str, str | None] = field(default_factory=dict)
    market_confidence: float | None = None
    as_of: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "direction": self.direction,
            "reason": self.reason,
            "priority": self.priority,
            "priority_score": round(self.priority_score, 4),
            "supporting_features": [
                {"name": f.name, "value": f.value} for f in self.supporting_features
            ],
            "feature_snapshot_id": self.feature_snapshot_id,
            "estimated_lifetime_minutes": self.estimated_lifetime_minutes,
            "current_market_regime": self.current_market_regime,
            "market_confidence": self.market_confidence,
            "as_of": self.as_of.isoformat(),
        }


def _dominant_state(result: IntelligenceResult | None) -> str | None:
    if result is None or not result.states:
        return None
    return max(result.states, key=lambda s: result.states[s])


def infer_direction(component_results: dict[str, IntelligenceResult | None]) -> str:
    """Confidence-weighted blend of every available -1..1 directional
    signal among the components that triggered this candidate."""
    weighted = 0.0
    total_weight = 0.0
    for component, metric in _DIRECTION_SIGNALS:
        result = component_results.get(component)
        if result is None:
            continue
        value = result.metrics.get(metric)
        if value is None:
            continue
        weighted += value * result.confidence
        total_weight += result.confidence
    if total_weight <= 0:
        return "neutral"
    level = weighted / total_weight
    if level > DIRECTION_EPSILON:
        return "long"
    if level < -DIRECTION_EPSILON:
        return "short"
    return "neutral"


def current_market_regime(
    component_results: dict[str, IntelligenceResult | None],
) -> dict[str, str | None]:
    """Dominant state per regime-bearing component, matching
    MarketStateReport's own current_regimes shape (Prompt 4.15)."""
    return {
        "trend": _dominant_state(component_results.get("trend")),
        "market_structure": _dominant_state(component_results.get("market_structure")),
        "volatility": _dominant_state(component_results.get("volatility")),
    }


def estimate_lifetime_minutes(
    opportunity: OpportunityCandidate,
) -> float:
    """The tightest (soonest-to-decay) estimate across every active trigger
    — a candidate's overall valid window is bounded by whichever signal
    decays fastest, not the slowest."""
    estimates: list[float] = []
    events = opportunity.component_results.get("events")
    for trigger in opportunity.triggers:
        if trigger.condition == "event_driven_opportunity" and events is not None:
            hours_until = events.metrics.get("hours_until_event")
            if hours_until is not None and hours_until > 0:
                estimates.append(min(hours_until * 60, EVENT_LIFETIME_CAP_MINUTES))
                continue
        estimates.append(TRIGGER_LIFETIME_MINUTES.get(trigger.condition, DEFAULT_LIFETIME_MINUTES))
    return min(estimates) if estimates else DEFAULT_LIFETIME_MINUTES


def build_reason(opportunity: OpportunityCandidate, direction: str) -> str:
    condition_labels = [t.condition.replace("_", " ") for t in opportunity.triggers]
    return (
        f"{direction.capitalize()} bias on {', '.join(condition_labels)} "
        f"({len(opportunity.triggers)} signal(s), priority {opportunity.priority_score:.2f})."
    )


def build_supporting_features(opportunity: OpportunityCandidate) -> list[SupportingFeature]:
    return [SupportingFeature(name=t.evidence, value=t.value) for t in opportunity.triggers]


def generate_candidate(
    opportunity: OpportunityCandidate, priority: int, feature_snapshot_id: str
) -> TradeCandidate:
    """Pure transformation: one triggered OpportunityCandidate -> one ranked
    TradeCandidate, using only data already attached to the opportunity plus
    the snapshot id the caller froze for it (FeatureSnapshotEngine, Prompt
    5.3 — capturing a snapshot is an async DB operation, so it happens in
    CandidateGenerationEngine.generate() below, not in this pure function)."""
    direction = infer_direction(opportunity.component_results)
    return TradeCandidate(
        instrument=opportunity.symbol,
        direction=direction,
        reason=build_reason(opportunity, direction),
        priority=priority,
        priority_score=opportunity.priority_score,
        supporting_features=build_supporting_features(opportunity),
        feature_snapshot_id=feature_snapshot_id,
        estimated_lifetime_minutes=estimate_lifetime_minutes(opportunity),
        current_market_regime=current_market_regime(opportunity.component_results),
        market_confidence=opportunity.market_confidence,
        as_of=opportunity.as_of,
    )


class CandidateGenerationEngine:
    name = "candidate_generation_engine"

    def __init__(
        self,
        session_factory: Any = None,
        settings: Settings | None = None,
        bus: EventBus | None = None,
        detector: OpportunityDetectionEngine | None = None,
        snapshot_engine: FeatureSnapshotEngine | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings or get_settings()
        self._bus = bus
        self._detector = detector or OpportunityDetectionEngine(
            session_factory=session_factory, settings=self._settings, bus=bus,
        )
        self._snapshots = snapshot_engine or FeatureSnapshotEngine(
            session_factory=session_factory, settings=self._settings
        )

    async def generate(self) -> list[TradeCandidate]:
        """Top MAX_CANDIDATES ranked trade candidates from a fresh scan.

        Snapshot capture (each one a fresh MarketStateReportEngine.generate()
        call -- not cheap) and persistence both fan out concurrently rather
        than looping one candidate at a time: up to MAX_CANDIDATES=20
        sequential awaits was the dominant cost in signal generation,
        directly threatening Volume 1 Sec16's <2s target (measured ~2.6s
        for just 6 candidates from a 3-symbol watchlist, sequential; see
        test_load_and_performance.py). Ordering is preserved -- gather()
        returns results in the same order as the input coroutines.
        """
        opportunities = await self._detector.scan()  # already sorted by priority_score desc
        top = opportunities[:MAX_CANDIDATES]
        snapshots = await asyncio.gather(*(self._snapshots.capture(o.symbol) for o in top))
        candidates = [
            generate_candidate(opportunity, rank, snapshot.snapshot_id)
            for rank, (opportunity, snapshot) in enumerate(zip(top, snapshots, strict=True), start=1)
        ]
        await asyncio.gather(*(self._persist(candidate) for candidate in candidates))
        return candidates

    async def _persist(self, candidate: TradeCandidate) -> None:
        if self._bus is not None:
            await self._bus.publish(
                Event(type=EVENT_TYPE, payload=candidate.to_dict(), source=self.name)
            )
        if self._sessions is None:
            return
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            session.add(MarketEvent(
                event_type=EVENT_TYPE,
                source=self.name,
                data=candidate.to_dict(),
            ))
            await session.commit()

    async def recent(self, symbol: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if self._sessions is None:
            return []
        from sqlalchemy import desc, select

        from app.database.tables import MarketEvent

        query = select(MarketEvent.data).where(MarketEvent.event_type == EVENT_TYPE)
        if symbol is not None:
            query = query.where(MarketEvent.data["instrument"].astext == symbol)
        query = query.order_by(desc(MarketEvent.id)).limit(limit)
        async with self._sessions() as session:
            result = await session.execute(query)
            return list(result.scalars().all())
