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
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.events.bus import Event, EventBus
from app.intelligence.base import IntelligenceResult
from app.prediction.opportunity import OpportunityCandidate, OpportunityDetectionEngine
from app.prediction.snapshot import FeatureSnapshotEngine

logger = get_logger(__name__)

EVENT_TYPE = "trade_candidate.generated"
MAX_CANDIDATES = 20
IST = ZoneInfo("Asia/Kolkata")


# enrich_with_signal_since()'s streak-detection tuning: how far back to
# fetch persisted history per instrument, and how large a gap between
# consecutive same-direction records before treating it as a new episode
# rather than a continuation (generous margin over the 300s scheduled-sweep
# cadence, to tolerate an occasional missed/slow cycle).
SIGNAL_SINCE_HISTORY_LIMIT = 1000
SIGNAL_STREAK_GAP_MINUTES = 20.0


def _streak_start(
    history: list[dict[str, Any]], direction: str, max_gap: timedelta
) -> datetime | None:
    """history is newest-first (MarketEvent.id DESC). Walk it while
    direction keeps matching and consecutive records are within max_gap of
    each other; return the as_of of the earliest record in that run."""
    streak_start: datetime | None = None
    previous_ts: datetime | None = None
    for record in history:
        if record.get("direction") != direction:
            break
        ts_raw = record.get("as_of")
        if not ts_raw:
            break
        ts = datetime.fromisoformat(ts_raw)
        if previous_ts is not None and (previous_ts - ts) > max_gap:
            break
        streak_start = ts
        previous_ts = ts
    return streak_start


def _ist_display(dt: datetime) -> str:
    """Human-readable IST rendering for as_of/valid_until -- e.g.
    "15 Jul 2026, 02:27 PM IST". The canonical as_of/valid_until fields stay
    ISO-8601 UTC (unambiguous, and what the dashboard's own JS parses via
    `new Date(...)`); this is a display-only addition, not a replacement."""
    return dt.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")

# Bounds how many candidates' FeatureSnapshotEngine.capture() calls run
# concurrently. Each capture() internally runs MarketStateReportEngine.
# generate(), itself a 12-way concurrent fan-out across intelligence
# sub-engines (each doing its own DB reads/writes) -- so unbounded
# concurrency here scales as MAX_CANDIDATES x 12 (up to 240 simultaneous
# DB-touching calls), regardless of database_pool_size. Found live
# (2026-07-14): even after fixing the sequential capture loop (which was
# a real bug, see generate()'s own note) and adding missing indexes, this
# unbounded fan-out alone kept /prediction/candidates at ~13-14s against
# the real production watchlist. A semaphore caps peak concurrent DB
# pressure independent of how many candidates or how wide the intelligence
# fan-out grows, which raising the pool size alone can't do.
MAX_CONCURRENT_SNAPSHOT_CAPTURES = 3

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

    @property
    def valid_until(self) -> datetime:
        """as_of + estimated_lifetime_minutes -- the point past which this
        setup is no longer expected to hold (see estimate_lifetime_minutes()
        below). A computed property, not a stored field, so it can never
        drift out of sync with as_of/estimated_lifetime_minutes. Lets a
        caller directly check "did the market move as predicted before
        this timestamp" instead of doing the arithmetic by hand."""
        return self.as_of + timedelta(minutes=self.estimated_lifetime_minutes)

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
            "valid_until": self.valid_until.isoformat(),
            "as_of_ist": _ist_display(self.as_of),
            "valid_until_ist": _ist_display(self.valid_until),
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

        Snapshot captures are bounded by MAX_CONCURRENT_SNAPSHOT_CAPTURES
        (see its own docstring) -- fully unbounded concurrency here just
        traded one bottleneck (sequential awaits) for another (each
        capture's own 12-way intelligence fan-out overwhelming the DB
        connection pool when all candidates' fan-outs run at once).
        """
        opportunities = await self._detector.scan()  # already sorted by priority_score desc
        top = opportunities[:MAX_CANDIDATES]

        # breadth/macro/sector/correlation are market-wide (no symbol
        # argument) -- fetched once here rather than once per candidate
        # inside each capture()'s report generation.
        market_wide = await self._snapshots.market_wide_context()

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_SNAPSHOT_CAPTURES)

        async def bounded_capture(opportunity: OpportunityCandidate):
            async with semaphore:
                # opportunity.component_results (trend/market_structure/
                # institutional_flow/volatility/events/trend_transition)
                # already carries this exact symbol's live results forward
                # from detect() -- merged with the market-wide fetch above,
                # capture()'s report generation reuses everything instead
                # of recomputing it (perf-audit-2026-07-14).
                precomputed = {**market_wide, **opportunity.component_results}
                return await self._snapshots.capture(opportunity.symbol, precomputed=precomputed)

        snapshots = await asyncio.gather(*(bounded_capture(o) for o in top))
        candidates = [
            generate_candidate(opportunity, rank, snapshot.snapshot_id)
            for rank, (opportunity, snapshot) in enumerate(zip(top, snapshots, strict=True), start=1)
        ]
        await self._persist_all(candidates)
        return candidates

    async def _persist_all(self, candidates: list[TradeCandidate]) -> None:
        """One session/commit for every candidate in this batch, not one
        INSERT+COMMIT per candidate (perf-audit-2026-07-14 finding 15).
        to_dict() is computed at most once per candidate and reused for
        both the event payload and the DB row, and skipped entirely when
        nothing is subscribed to EVENT_TYPE (findings 16/17)."""
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
            query = query.where(MarketEvent.data["instrument"].astext == symbol)
        query = query.order_by(desc(MarketEvent.id)).limit(limit)
        async with self._sessions() as session:
            result = await session.execute(query)
            return list(result.scalars().all())

    async def enrich_with_signal_since(self, candidates: list[TradeCandidate]) -> list[dict[str, Any]]:
        """Each candidate's dict, plus signal_since/signal_since_ist: when
        the current continuous run of this exact (instrument, direction)
        started, not just when this scan last re-confirmed it -- "as_of"
        updates every call (every scan re-detects the same live condition),
        which answers "when was this last confirmed", not "when did this
        setup first appear".

        Walks each instrument's persisted trade_candidate.generated history
        backward from now, stopping at the first direction change or at a
        gap between consecutive records wider than SIGNAL_STREAK_GAP_MINUTES
        (treated as the signal having genuinely lapsed, not continued).

        One bounded query covering every instrument in this batch, not one
        per candidate. Deliberately NOT called from generate() itself
        (also used by the scheduled sweep, which has no use for this) --
        only the on-demand API route pays this extra cost."""
        dicts = [c.to_dict() for c in candidates]
        if self._sessions is None or not candidates:
            return dicts

        from sqlalchemy import desc, select

        from app.database.tables import MarketEvent

        instruments = list({c.instrument for c in candidates})
        query = (
            select(MarketEvent.data)
            .where(
                MarketEvent.event_type == EVENT_TYPE,
                MarketEvent.source == self.name,
                MarketEvent.data["instrument"].astext.in_(instruments),
            )
            .order_by(desc(MarketEvent.id))
            .limit(SIGNAL_SINCE_HISTORY_LIMIT)
        )
        async with self._sessions() as session:
            rows = (await session.execute(query)).scalars().all()

        by_instrument: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row and row.get("instrument"):
                by_instrument.setdefault(row["instrument"], []).append(row)

        gap = timedelta(minutes=SIGNAL_STREAK_GAP_MINUTES)
        for candidate_dict in dicts:
            history = by_instrument.get(candidate_dict["instrument"], [])
            since = _streak_start(history, candidate_dict["direction"], gap)
            candidate_dict["signal_since"] = since.isoformat() if since else None
            candidate_dict["signal_since_ist"] = _ist_display(since) if since else None
        return dicts
