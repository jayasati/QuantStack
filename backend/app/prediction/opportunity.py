"""Opportunity Detection Engine (Volume 5, Prompt 5.1).

Gate-keeper before any prediction model exists: scans the watchlist and
flags a symbol as an opportunity candidate only when at least one of 8
already-computed Volume 4 intelligence signals crosses a threshold, then
ranks triggered symbols by a confidence-weighted priority score. Nothing
downstream (candidate generation, prediction — Prompts 5.2+) should run on
a symbol that doesn't trigger here; that's the efficiency gate the doc asks
for ("avoid unnecessary model inference on low-quality candidates"),
meaningful even before a model exists because it already prevents wasted
work in every later stage.

Trigger conditions map to real, already-built Volume 4 fields (not
invented ones):

    Significant breakout probability      -> MarketStructureIntelligence
                                              .metrics["breakout_probability"]
    Structural trend change               -> RegimeTransitionEngine
                                              (component="trend").metrics["alert"]
    Liquidity sweep detected              -> MarketStructureIntelligence
                                              .states["liquidity_sweep"]
    Regime transition                     -> RegimeTransitionEngine
                                              (component="market_structure").metrics["alert"]
    Institutional accumulation/distribution -> InstitutionalFlowIntelligence
                                              .states["institutional_accumulation"/"institutional_distribution"]
    Exceptional relative strength         -> RelativeStrengthIntelligence
                                              .metrics["leadership_ranking"]
    High volatility expansion             -> VolatilityIntelligence
                                              .states["expansion"]
    Event-driven opportunity              -> EventIntelligence.score

RegimeTransitionEngine needs belief history that nothing in this codebase
was actually populating (BayesianRegimeDetector.update_from_result() had
no call sites at all) — this engine closes that loop itself: every scan
feeds the trend and market-structure reads it already fetches into the
Bayesian detector before asking for the transition read, so "structural
trend change" and "regime transition" actually mature over time instead of
staying permanently dormant.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.events.bus import Event, EventBus
from app.intelligence.base import IntelligenceResult
from app.intelligence.composite import _states_changed
from app.intelligence.events import EventIntelligenceEngine
from app.intelligence.explain import ExplainabilityStore
from app.intelligence.institutional_flow import InstitutionalFlowIntelligenceEngine
from app.intelligence.regime import BayesianRegimeDetector
from app.intelligence.relative import RelativeStrengthIntelligenceEngine
from app.intelligence.report import MarketStateReportEngine
from app.intelligence.structure import MarketStructureIntelligenceEngine
from app.intelligence.transitions import RegimeTransitionEngine
from app.intelligence.trend import TrendIntelligenceEngine
from app.intelligence.volatility import VolatilityIntelligenceEngine

logger = get_logger(__name__)

EVENT_TYPE = "opportunity.detected"

# Trigger thresholds — module-level, not hardcoded inline (drift.py's
# THRESHOLDS convention). Regime-transition triggers reuse
# RegimeTransitionEngine's own alert flag/threshold rather than a second one.
BREAKOUT_PROBABILITY_THRESHOLD = 0.65
LIQUIDITY_SWEEP_THRESHOLD = 0.5
INSTITUTIONAL_FLOW_THRESHOLD = 0.5
LEADERSHIP_RANKING_THRESHOLD = 85.0
VOLATILITY_EXPANSION_THRESHOLD = 0.5
EVENT_RISK_THRESHOLD = 40.0  # events.py's "elevated_risk" anchor


@dataclass(frozen=True)
class TriggerReason:
    """One condition that fired, and the evidence behind it."""

    condition: str
    evidence: str
    value: float
    weight: float


@dataclass
class OpportunityCandidate:
    symbol: str
    as_of: datetime
    triggers: list[TriggerReason] = field(default_factory=list)
    priority_score: float = 0.0
    market_confidence: float | None = None
    # The most recently persisted Composite Market Intelligence Score
    # (Ch18) -- a genuine read of that report rather than a redundant
    # live recompute (composite_intelligence_sweep, main.py, keeps this
    # fresh independently of any single detect() call).
    composite_score: float | None = None
    composite_confidence: float | None = None
    # Transient: the live IntelligenceResults this candidate was triggered
    # from, kept in-memory only (not part of to_dict()/persistence) so
    # CandidateGenerationEngine (Prompt 5.2) can build Direction/Regime off
    # the exact same data that fired, without a second, possibly-inconsistent
    # fetch. Excluded from repr since IntelligenceResult objects are large.
    component_results: dict[str, IntelligenceResult | None] = field(
        default_factory=dict, repr=False
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "as_of": self.as_of.isoformat(),
            "triggers": [
                {
                    "condition": t.condition, "evidence": t.evidence,
                    "value": t.value, "weight": t.weight,
                }
                for t in self.triggers
            ],
            "priority_score": round(self.priority_score, 4),
            "market_confidence": self.market_confidence,
            "composite_score": self.composite_score,
            "composite_confidence": self.composite_confidence,
        }


def evaluate_triggers(
    component_results: Mapping[str, IntelligenceResult | None],
) -> list[TriggerReason]:
    """Pure trigger evaluation. Missing/None components (a component that
    failed to compute, matching MarketStateReportEngine's own safe()
    swallowing) simply contribute no triggers rather than raising."""
    triggers: list[TriggerReason] = []

    structure = component_results.get("market_structure")
    if structure is not None:
        breakout = structure.metrics.get("breakout_probability")
        if breakout is not None and breakout > BREAKOUT_PROBABILITY_THRESHOLD:
            triggers.append(TriggerReason(
                "significant_breakout_probability", "ms_breakout_probability",
                breakout, structure.confidence,
            ))
        sweep = structure.states.get("liquidity_sweep", 0.0)
        if sweep > LIQUIDITY_SWEEP_THRESHOLD:
            triggers.append(TriggerReason(
                "liquidity_sweep_detected", "market_structure.states.liquidity_sweep",
                sweep, structure.confidence,
            ))

    trend_transition = component_results.get("trend_transition")
    if trend_transition is not None and trend_transition.metrics.get("alert"):
        triggers.append(TriggerReason(
            "structural_trend_change", "regime_transition[trend].alert",
            trend_transition.metrics.get("transition_probability") or 0.0,
            trend_transition.confidence,
        ))

    structure_transition = component_results.get("market_structure_transition")
    if structure_transition is not None and structure_transition.metrics.get("alert"):
        triggers.append(TriggerReason(
            "regime_transition", "regime_transition[market_structure].alert",
            structure_transition.metrics.get("transition_probability") or 0.0,
            structure_transition.confidence,
        ))

    flow = component_results.get("institutional_flow")
    if flow is not None:
        accumulation = flow.states.get("institutional_accumulation", 0.0)
        distribution = flow.states.get("institutional_distribution", 0.0)
        if accumulation > INSTITUTIONAL_FLOW_THRESHOLD:
            triggers.append(TriggerReason(
                "institutional_accumulation",
                "institutional_flow.states.institutional_accumulation",
                accumulation, flow.confidence,
            ))
        if distribution > INSTITUTIONAL_FLOW_THRESHOLD:
            triggers.append(TriggerReason(
                "institutional_distribution",
                "institutional_flow.states.institutional_distribution",
                distribution, flow.confidence,
            ))

    relative = component_results.get("relative_strength")
    if relative is not None:
        leadership = relative.metrics.get("leadership_ranking")
        if leadership is not None and leadership > LEADERSHIP_RANKING_THRESHOLD:
            triggers.append(TriggerReason(
                "exceptional_relative_strength", "relative_strength.metrics.leadership_ranking",
                leadership, relative.confidence,
            ))

    volatility = component_results.get("volatility")
    if volatility is not None:
        expansion = volatility.states.get("expansion", 0.0)
        if expansion > VOLATILITY_EXPANSION_THRESHOLD:
            triggers.append(TriggerReason(
                "high_volatility_expansion", "volatility.states.expansion",
                expansion, volatility.confidence,
            ))

    events = component_results.get("events")
    if events is not None and events.score > EVENT_RISK_THRESHOLD:
        triggers.append(TriggerReason(
            "event_driven_opportunity", "events.score",
            events.score, events.confidence,
        ))

    return triggers


def priority_score(triggers: list[TriggerReason]) -> float:
    """Confidence-weighted sum, not a raw trigger count — one high-confidence
    liquidity sweep should outrank three weak/uncertain triggers."""
    return round(sum(t.weight for t in triggers), 4)


class OpportunityDetectionEngine:
    """Not an IntelligenceComponent subclass: its output (a ranked candidate
    list) is a genuinely different shape than a single IntelligenceResult —
    the same reasoning MarketStateReportEngine's own report-shaped output
    already established as acceptable in this layer."""

    name = "opportunity_detection_engine"

    def __init__(
        self,
        session_factory: Any = None,
        settings: Settings | None = None,
        bus: EventBus | None = None,
        trend_engine: TrendIntelligenceEngine | None = None,
        market_structure_engine: MarketStructureIntelligenceEngine | None = None,
        institutional_flow_engine: InstitutionalFlowIntelligenceEngine | None = None,
        relative_strength_engine: RelativeStrengthIntelligenceEngine | None = None,
        volatility_engine: VolatilityIntelligenceEngine | None = None,
        event_engine: EventIntelligenceEngine | None = None,
        regime_detector: BayesianRegimeDetector | None = None,
        regime_transition_engine: RegimeTransitionEngine | None = None,
        report_engine: MarketStateReportEngine | None = None,
        explainability_store: ExplainabilityStore | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings or get_settings()
        self._bus = bus
        self._trend = trend_engine or TrendIntelligenceEngine(
            session_factory=session_factory, settings=self._settings, bus=bus,
        )
        self._market_structure = market_structure_engine or MarketStructureIntelligenceEngine(
            session_factory=session_factory, settings=self._settings, bus=bus,
        )
        self._institutional_flow = institutional_flow_engine or InstitutionalFlowIntelligenceEngine(
            session_factory=session_factory, settings=self._settings, bus=bus,
        )
        self._relative_strength = relative_strength_engine or RelativeStrengthIntelligenceEngine(
            session_factory=session_factory, settings=self._settings, bus=bus,
        )
        self._volatility = volatility_engine or VolatilityIntelligenceEngine(
            session_factory=session_factory, settings=self._settings, bus=bus,
        )
        self._events = event_engine or EventIntelligenceEngine(
            session_factory=session_factory, settings=self._settings, bus=bus,
        )
        self._regime_detector = regime_detector or BayesianRegimeDetector(
            session_factory=session_factory, settings=self._settings, bus=bus,
        )
        self._regime_transitions = regime_transition_engine or RegimeTransitionEngine(
            session_factory=session_factory, settings=self._settings, bus=bus,
        )
        self._report_engine = report_engine or MarketStateReportEngine(
            session_factory=session_factory, settings=self._settings, bus=bus,
        )
        self._explainability = explainability_store or ExplainabilityStore(
            session_factory=session_factory, settings=self._settings, bus=bus,
        )
        # Dedup for the regime-belief feed below -- same _states_changed
        # fix composite.py already applies to its own identical feed
        # (perf-audit-2026-07-14 finding 15): without it, every request
        # re-writes a belief update even when the underlying states haven't
        # moved since the last feed, inflating observation_count from
        # repetition rather than genuinely new evidence.
        self._last_fed_states: dict[tuple[str, str, str], dict[str, float]] = {}

    async def detect(
        self,
        symbol: str,
        *,
        institutional_flow: IntelligenceResult | None = None,
        events: IntelligenceResult | None = None,
    ) -> OpportunityCandidate | None:
        """`institutional_flow`/`events` are market-wide (no symbol
        argument), so their answer is identical for every symbol in a scan.
        `scan()` computes them once and passes them in here; a standalone
        caller that omits them gets the old per-call behavior."""
        async def safe(coro: Any) -> IntelligenceResult | None:
            try:
                return await coro
            except Exception as exc:
                logger.warning(
                    "opportunity detection component failed",
                    extra={"symbol": symbol, "error": str(exc)},
                )
                return None

        async def reuse_or_fetch(value, factory):
            if value is not None:
                return value
            return await safe(factory())

        # Separate tasks (not folded into the gather below) so their float |
        # None returns don't collapse the tuple's per-position typing to a
        # union across every element.
        confidence_task = asyncio.ensure_future(self._market_confidence(symbol))
        composite_task = asyncio.ensure_future(self._composite_context(symbol))

        trend, structure, flow, relative, volatility, events = await asyncio.gather(
            safe(self._trend.assess(symbol=symbol)),
            safe(self._market_structure.assess(symbol=symbol)),
            reuse_or_fetch(institutional_flow, self._institutional_flow.assess),
            safe(self._relative_strength.assess(symbol=symbol)),
            safe(self._volatility.assess(symbol=symbol)),
            reuse_or_fetch(events, self._events.assess),
        )
        confidence_report = await confidence_task
        composite_score, composite_confidence = await composite_task

        trend_transition = None
        structure_transition = None
        if trend is not None:
            key = ("trend", symbol, "D")
            if _states_changed(self._last_fed_states.get(key), trend.states):
                await self._regime_detector.update_from_result("trend", symbol, "D", trend)
                self._last_fed_states[key] = dict(trend.states)
            trend_transition = await safe(
                self._regime_transitions.assess(component="trend", symbol=symbol)
            )
        if structure is not None:
            key = ("market_structure", symbol, "D")
            if _states_changed(self._last_fed_states.get(key), structure.states):
                await self._regime_detector.update_from_result(
                    "market_structure", symbol, "D", structure
                )
                self._last_fed_states[key] = dict(structure.states)
            structure_transition = await safe(
                self._regime_transitions.assess(component="market_structure", symbol=symbol)
            )

        component_results: dict[str, IntelligenceResult | None] = {
            "trend": trend,
            "market_structure": structure,
            "trend_transition": trend_transition,
            "market_structure_transition": structure_transition,
            "institutional_flow": flow,
            "relative_strength": relative,
            "volatility": volatility,
            "events": events,
        }
        triggers = evaluate_triggers(component_results)
        if not triggers:
            return None

        candidate = OpportunityCandidate(
            symbol=symbol,
            as_of=datetime.now(UTC),
            triggers=triggers,
            priority_score=priority_score(triggers),
            market_confidence=confidence_report,
            composite_score=composite_score,
            composite_confidence=composite_confidence,
            component_results=component_results,
        )
        return candidate

    async def _market_confidence(self, symbol: str) -> float | None:
        if self._sessions is None:
            return None
        latest = await self._report_engine.report_as_of(symbol, datetime.now(UTC))
        if not latest:
            return None
        return (latest.get("market_confidence") or {}).get("score")

    async def _composite_context(self, symbol: str) -> tuple[float | None, float | None]:
        """Read the most recently PERSISTED Composite Market Intelligence
        Score (main.py's composite_intelligence_sweep keeps this fresh on
        its own schedule) rather than calling CompositeMarketIntelligenceEngine
        directly here -- this candidate already computes 6 of Composite's 11
        components itself for trigger evaluation; a second live recompute of
        the other 5 just to read one aggregate score would be exactly the
        redundant-computation pattern this fix exists to avoid."""
        if self._sessions is None:
            return None, None
        record = await self._explainability.latest("composite_market_intelligence", symbol, "D")
        if not record:
            return None, None
        return record.get("score"), record.get("confidence")

    async def scan(self) -> list[OpportunityCandidate]:
        """Every watchlist symbol, concurrently, sorted by priority
        descending. institutional_flow/events are market-wide -- fetched
        once here rather than once per symbol inside detect()."""
        async def safe(coro: Any) -> IntelligenceResult | None:
            try:
                return await coro
            except Exception as exc:
                logger.warning(
                    "opportunity scan market-wide component failed",
                    extra={"error": str(exc)},
                )
                return None

        institutional_flow, events = await asyncio.gather(
            safe(self._institutional_flow.assess()),
            safe(self._events.assess()),
        )
        results = await asyncio.gather(
            *(
                self.detect(symbol, institutional_flow=institutional_flow, events=events)
                for symbol in self._settings.watchlist
            )
        )
        candidates = [c for c in results if c is not None]
        candidates.sort(key=lambda c: c.priority_score, reverse=True)
        await self._persist_all(candidates)
        return candidates

    async def _persist_all(self, candidates: list[OpportunityCandidate]) -> None:
        """One session/commit for every triggered candidate from this scan,
        not one INSERT+COMMIT per candidate (perf-audit-2026-07-14 finding
        15) -- moved here from detect() itself, which is only ever called
        from scan() (never persisted a candidate on its own before this
        fix either, since scan() is the only caller). to_dict() is computed
        at most once per candidate and reused for both the event payload
        and the DB row, and skipped entirely when nothing is subscribed to
        EVENT_TYPE (findings 16/17)."""
        if not candidates:
            return
        publish = self._bus is not None and self._bus.has_subscribers(EVENT_TYPE)
        payloads = (
            [candidate.to_dict() for candidate in candidates]
            if publish or self._sessions is not None else []
        )
        if publish:
            await asyncio.gather(*(
                self._bus.publish(Event(type=EVENT_TYPE, payload=payload, source=self.name))
                for payload in payloads
            ))
        if self._sessions is None:
            return
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            session.add_all([
                MarketEvent(event_type=EVENT_TYPE, source=self.name, data=payload)
                for payload in payloads
            ])
            await session.commit()

    async def recent(self, symbol: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if self._sessions is None:
            return []
        from sqlalchemy import desc, select

        from app.database.tables import MarketEvent

        query = select(MarketEvent.data).where(MarketEvent.event_type == EVENT_TYPE)
        if symbol is not None:
            query = query.where(MarketEvent.data["symbol"].astext == symbol)
        query = query.order_by(desc(MarketEvent.id)).limit(limit)
        async with self._sessions() as session:
            result = await session.execute(query)
            return list(result.scalars().all())
