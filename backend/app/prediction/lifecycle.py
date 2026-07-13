"""Opportunity Lifecycle Manager (Volume 5, Prompt 5.15).

"Signals evolve. Every opportunity moves through a tracked lifecycle:
Detected -> Confirmed -> Qualified -> Sent -> Monitoring -> Expired ->
Succeeded / Failed." A genuinely different shape from every other engine
built this volume so far -- not a scoring/ranking/filtering pass over
already-computed values, but a STATE MACHINE and append-only ledger. No
prior engine mints a stable identity that survives across the whole
pipeline (OpportunityCandidate/TradeCandidate/RankedSignal are each
independently regenerated on every fresh scan), so this manager mints its
own `lifecycle_id` at "detected" -- the same `uuid.uuid4().hex` convention
FeatureSnapshotEngine's own `snapshot_id` already established -- and every
later stage transition is looked up and recorded against that id.

Transitions are validated, not merely recorded:
- The five non-terminal stages (detected/confirmed/qualified/sent/
  monitoring) must be entered strictly in order, one at a time -- no
  skipping, no going backward.
- Any of the three terminal stages (expired/succeeded/failed) may be
  entered from ANY non-terminal stage -- realistic, since most detected
  opportunities never reach "sent" at all (Prompt 5.12's Trade
  Qualification Engine rejects most of them, Prompt 5.14's Duplicate
  Signal Engine suppresses more), and a qualified-but-suppressed signal
  should still be able to terminate as "expired" without ever being sent.
- Once terminal, a lifecycle is closed: no further transitions.

State is reconstructed from the persisted transition log itself (one
MarketEvent row per transition, replayed through the same pure
`apply_transition` the write path uses) rather than a separately
maintained "current state" row -- the append-only ledger IS the source of
truth, the same shape this codebase's other append-only histories
(Market Confidence's score history, Bayesian Regime belief history) use.

Concurrency: `_advance()` is a read-then-write (read current state, compute
the next one, append it) with no natural atomicity of its own -- two
concurrent calls to the same transition (a retried request, a duplicate
webhook delivery) could otherwise both read the same prior stage and both
append a valid-looking row, leaving TWO consecutive rows at the same
stage. That doesn't just look redundant: replaying the log afterwards hits
the second identical-stage row with `state.stage` already at that stage,
fails the "advance exactly one stage" check, and raises -- permanently,
since the corrupted rows never leave the log and every future `get()`
replays the same failure. Two independent guards close this:
- Every `_advance()` call for a given `lifecycle_id` is serialized through
  an in-process `asyncio.Lock`, so a concurrent call always sees the
  other's write before doing its own read.
- Re-requesting the stage a lifecycle is already at is treated as an
  idempotent no-op (return the current state, write nothing) rather than
  a fresh transition -- this is what makes a duplicate/retried call safe
  instead of corrupting.

An in-process `asyncio.Lock` only excludes concurrent calls within the SAME
OS process -- it provides zero exclusion across processes. If this app is
ever deployed with more than one worker process (`uvicorn --workers N`,
gunicorn, multiple pods), each process gets its own independent `_locks`
dict, two workers can concurrently advance the same `lifecycle_id`, and the
exact corruption this fix closed comes back with no error at deploy time to
flag it -- the failure would only surface later, replaying a corrupted log.
Since neither uvicorn nor gunicorn expose a reliable signal the app can
introspect to detect its own worker count, the manager instead requires
`Settings.deployment_workers` to be declared truthfully by whoever changes
the deploy command, and refuses to start (`RuntimeError` at construction,
resolved eagerly at app boot -- see `main.py`'s `lifespan`) unless it's `1`.
Scaling out requires first replacing this lock with a cross-process one
(e.g. a Postgres advisory lock keyed on `lifecycle_id`, or a Redis lock).
`get()` additionally skips a persisted row that repeats the immediately
preceding stage during replay, rather than raising -- a self-healing
read path for any lifecycle_id that was corrupted by this exact pattern
before the fix above existed, so no record is permanently unreadable.

The five measured fields:
- Detection Delay: confirmed_at - detected_at. The doc doesn't define
  this against a system where "the market event" and "detection" are
  distinct timestamps -- this codebase's own detection IS the moment
  OpportunityDetectionEngine scans current feature state, so there is no
  separate "when the opportunity actually arose" instant to measure
  against. The most honestly computable reading, given this lifecycle's
  own instrumentation, is the gap between being flagged and being
  confirmed still valid -- a real, meaningful delay, not a fabricated one.
- Signal Age: now - detected_at. How stale the signal is.
- Signal Lifetime: (terminal timestamp) - detected_at, only once a
  lifecycle has actually reached a terminal stage -- None while still
  open, never estimated or backfilled.
- Expiration Reason: set only when the terminal stage is "expired";
  required at that transition (a required, not optional, explanation).
- Outcome: set only when the terminal stage is "succeeded" or "failed".
  No P&L/execution tracking exists yet (that's Volume 6's job) --
  outcome is never inferred from data this codebase doesn't have, only
  ever set by an explicit `succeed()`/`fail()` call from a caller that
  actually knows what happened.
"""

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings, get_settings
from app.events.bus import Event, EventBus

EVENT_TYPE = "opportunity_lifecycle.transition"

NON_TERMINAL_STAGES: tuple[str, ...] = ("detected", "confirmed", "qualified", "sent", "monitoring")
TERMINAL_STAGES: tuple[str, ...] = ("expired", "succeeded", "failed")
ALL_STAGES: tuple[str, ...] = NON_TERMINAL_STAGES + TERMINAL_STAGES
_STAGE_INDEX: dict[str, int] = {stage: i for i, stage in enumerate(NON_TERMINAL_STAGES)}


class InvalidTransitionError(ValueError):
    """A stage transition that would skip a stage, go backward, or resume
    an already-terminal lifecycle."""


@dataclass
class LifecycleState:
    lifecycle_id: str
    symbol: str
    direction: str
    stage: str
    stage_timestamps: dict[str, datetime] = field(default_factory=dict)
    expiration_reason: str | None = None
    outcome: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.stage in TERMINAL_STAGES

    def detection_delay_seconds(self) -> float | None:
        detected_at = self.stage_timestamps.get("detected")
        confirmed_at = self.stage_timestamps.get("confirmed")
        if detected_at is None or confirmed_at is None:
            return None
        return (confirmed_at - detected_at).total_seconds()

    def signal_age_seconds(self, now: datetime) -> float | None:
        detected_at = self.stage_timestamps.get("detected")
        if detected_at is None:
            return None
        return (now - detected_at).total_seconds()

    def signal_lifetime_seconds(self) -> float | None:
        detected_at = self.stage_timestamps.get("detected")
        if detected_at is None or not self.is_terminal:
            return None
        terminal_at = self.stage_timestamps.get(self.stage)
        if terminal_at is None:
            return None
        return (terminal_at - detected_at).total_seconds()

    def to_dict(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        return {
            "lifecycle_id": self.lifecycle_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "stage": self.stage,
            "stage_timestamps": {
                stage: ts.isoformat() for stage, ts in self.stage_timestamps.items()
            },
            "detection_delay_seconds": self.detection_delay_seconds(),
            "signal_age_seconds": self.signal_age_seconds(now),
            "signal_lifetime_seconds": self.signal_lifetime_seconds(),
            "expiration_reason": self.expiration_reason,
            "outcome": self.outcome,
        }


def apply_transition(
    state: LifecycleState | None,
    stage: str,
    at: datetime,
    *,
    symbol: str | None = None,
    direction: str | None = None,
    lifecycle_id: str | None = None,
    expiration_reason: str | None = None,
    outcome: str | None = None,
) -> LifecycleState:
    """Pure state transition -- no DB access. `state=None` is only valid
    for stage="detected" (a lifecycle's genesis, minting a fresh id if
    none is supplied)."""
    if state is None:
        if stage != "detected":
            raise InvalidTransitionError(f"a lifecycle must start at 'detected', not '{stage}'")
        return LifecycleState(
            lifecycle_id=lifecycle_id or uuid.uuid4().hex,
            symbol=symbol or "", direction=direction or "",
            stage="detected", stage_timestamps={"detected": at},
        )

    if state.is_terminal:
        raise InvalidTransitionError(
            f"lifecycle {state.lifecycle_id} is already terminal ('{state.stage}')"
        )

    if stage in TERMINAL_STAGES:
        pass  # terminal transitions are allowed from any non-terminal stage
    elif stage in _STAGE_INDEX:
        if _STAGE_INDEX[stage] != _STAGE_INDEX[state.stage] + 1:
            raise InvalidTransitionError(
                f"cannot move from '{state.stage}' to '{stage}' "
                "(stages must advance one at a time)"
            )
    else:
        raise InvalidTransitionError(f"unknown stage '{stage}'")

    if stage == "expired" and expiration_reason is None:
        raise InvalidTransitionError("'expired' requires an expiration_reason")
    if stage in ("succeeded", "failed") and outcome is None:
        outcome = stage

    return LifecycleState(
        lifecycle_id=state.lifecycle_id, symbol=state.symbol, direction=state.direction,
        stage=stage, stage_timestamps={**state.stage_timestamps, stage: at},
        expiration_reason=expiration_reason if stage == "expired" else state.expiration_reason,
        outcome=outcome if stage in ("succeeded", "failed") else state.outcome,
    )


def replay_transitions(
    rows: Sequence[Mapping[str, Any]], lifecycle_id: str
) -> LifecycleState | None:
    """Pure reconstruction of current state from persisted transition
    rows (oldest first) -- no DB access. Skips a row that repeats the
    immediately preceding stage rather than replaying it through
    `apply_transition` (which would raise): a self-healing read path for
    any lifecycle_id a past write-side race already left with a
    duplicate-consecutive-stage row, so no record is permanently
    unreadable."""
    if not rows:
        return None

    state: LifecycleState | None = None
    for row in rows:
        if state is not None and row["stage"] == state.stage:
            continue
        state = apply_transition(
            state, row["stage"], datetime.fromisoformat(row["at"]),
            symbol=row.get("symbol"), direction=row.get("direction"),
            lifecycle_id=lifecycle_id,
            expiration_reason=row.get("expiration_reason"), outcome=row.get("outcome"),
        )
    return state


class OpportunityLifecycleManager:
    name = "opportunity_lifecycle_manager"

    def __init__(
        self,
        session_factory: Any = None,
        settings: Settings | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings or get_settings()
        self._bus = bus
        if self._settings.deployment_workers != 1:
            raise RuntimeError(
                "OpportunityLifecycleManager's asyncio.Lock only provides "
                "exclusion within a single process (IRR Critical #2's fix), "
                f"but Settings.deployment_workers={self._settings.deployment_workers} "
                "declares more than one worker process. Deploying multiple "
                "workers with this lock as-is silently reintroduces the "
                "lifecycle race/corruption that fix closed. Replace the lock "
                "with a cross-process one (e.g. a Postgres advisory lock "
                "keyed on lifecycle_id, or a Redis lock) before scaling out."
            )
        # One lock per lifecycle_id, created lazily -- serializes concurrent
        # _advance() calls for the SAME id so a race can never write two
        # transitions off the same stale read. Dict access itself needs no
        # extra guard: asyncio is single-threaded and nothing below awaits
        # between the `in` check and the assignment.
        self._locks: dict[str, asyncio.Lock] = {}

    async def detect(
        self, symbol: str, direction: str, at: datetime | None = None
    ) -> LifecycleState:
        state = apply_transition(None, "detected", at or datetime.now(UTC),
                                  symbol=symbol, direction=direction)
        await self._persist(state, at=state.stage_timestamps["detected"])
        return state

    async def confirm(self, lifecycle_id: str, at: datetime | None = None) -> LifecycleState:
        return await self._advance(lifecycle_id, "confirmed", at)

    async def qualify(self, lifecycle_id: str, at: datetime | None = None) -> LifecycleState:
        return await self._advance(lifecycle_id, "qualified", at)

    async def mark_sent(self, lifecycle_id: str, at: datetime | None = None) -> LifecycleState:
        return await self._advance(lifecycle_id, "sent", at)

    async def monitor(self, lifecycle_id: str, at: datetime | None = None) -> LifecycleState:
        return await self._advance(lifecycle_id, "monitoring", at)

    async def expire(
        self, lifecycle_id: str, reason: str, at: datetime | None = None
    ) -> LifecycleState:
        return await self._advance(lifecycle_id, "expired", at, expiration_reason=reason)

    async def succeed(
        self, lifecycle_id: str, at: datetime | None = None, outcome: str = "succeeded"
    ) -> LifecycleState:
        return await self._advance(lifecycle_id, "succeeded", at, outcome=outcome)

    async def fail(
        self, lifecycle_id: str, at: datetime | None = None, outcome: str = "failed"
    ) -> LifecycleState:
        return await self._advance(lifecycle_id, "failed", at, outcome=outcome)

    def _lock_for(self, lifecycle_id: str) -> asyncio.Lock:
        lock = self._locks.get(lifecycle_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[lifecycle_id] = lock
        return lock

    async def _advance(
        self,
        lifecycle_id: str,
        stage: str,
        at: datetime | None,
        *,
        expiration_reason: str | None = None,
        outcome: str | None = None,
    ) -> LifecycleState:
        async with self._lock_for(lifecycle_id):
            state = await self.get(lifecycle_id)
            if state is None:
                raise InvalidTransitionError(f"unknown lifecycle_id '{lifecycle_id}'")

            if state.stage == stage:
                # Idempotent no-op: a retried/duplicate call re-requesting
                # the stage this lifecycle is already at. Returning the
                # current state (rather than re-running apply_transition,
                # which would either raise on a terminal stage or append a
                # second identical-stage row) is what makes a duplicate
                # call safe instead of corrupting the log.
                return state

            at = at or datetime.now(UTC)
            new_state = apply_transition(
                state, stage, at, expiration_reason=expiration_reason, outcome=outcome,
            )
            await self._persist(new_state, at=at)
            return new_state

    async def get(self, lifecycle_id: str) -> LifecycleState | None:
        """Fetches the persisted transition log for this lifecycle_id and
        reconstructs current state via `replay_transitions`."""
        if self._sessions is None:
            return None
        from sqlalchemy import select

        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            result = await session.execute(
                select(MarketEvent.data)
                .where(
                    MarketEvent.event_type == EVENT_TYPE,
                    MarketEvent.data["lifecycle_id"].astext == lifecycle_id,
                )
                .order_by(MarketEvent.id)
            )
            rows = result.scalars().all()
        return replay_transitions(rows, lifecycle_id)

    async def _persist(self, state: LifecycleState, at: datetime) -> None:
        payload = {
            "lifecycle_id": state.lifecycle_id,
            "symbol": state.symbol,
            "direction": state.direction,
            "stage": state.stage,
            "at": at.isoformat(),
            "expiration_reason": state.expiration_reason,
            "outcome": state.outcome,
        }
        if self._bus is not None:
            await self._bus.publish(Event(type=EVENT_TYPE, payload=payload, source=self.name))
        if self._sessions is None:
            return
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            session.add(MarketEvent(event_type=EVENT_TYPE, source=self.name, data=payload))
            await session.commit()

    async def recent(
        self,
        symbol: str | None = None,
        lifecycle_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Raw transition-log rows (an audit trail), newest first --
        distinct from `get()`, which returns one reconstructed current
        state."""
        if self._sessions is None:
            return []
        from sqlalchemy import desc, select

        from app.database.tables import MarketEvent

        query = select(MarketEvent.data).where(MarketEvent.event_type == EVENT_TYPE)
        if symbol is not None:
            query = query.where(MarketEvent.data["symbol"].astext == symbol)
        if lifecycle_id is not None:
            query = query.where(MarketEvent.data["lifecycle_id"].astext == lifecycle_id)
        query = query.order_by(desc(MarketEvent.id)).limit(limit)
        async with self._sessions() as session:
            result = await session.execute(query)
            return list(result.scalars().all())
