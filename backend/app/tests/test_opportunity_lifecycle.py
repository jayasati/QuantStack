"""Tests for the Opportunity Lifecycle Manager (Volume 5, Prompt 5.15)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.prediction.lifecycle import (
    ALL_STAGES,
    NON_TERMINAL_STAGES,
    TERMINAL_STAGES,
    InvalidTransitionError,
    LifecycleState,
    OpportunityLifecycleManager,
    apply_transition,
    replay_transitions,
)

BASE_TS = datetime(2026, 7, 1, tzinfo=UTC)


def detected(at: datetime = BASE_TS) -> LifecycleState:
    return apply_transition(None, "detected", at, symbol="NIFTY", direction="long")


# --- apply_transition: genesis -------------------------------------------


def test_a_lifecycle_must_start_at_detected() -> None:
    with pytest.raises(InvalidTransitionError):
        apply_transition(None, "confirmed", BASE_TS)


def test_detected_mints_a_fresh_id_when_none_given() -> None:
    a = detected()
    b = detected()
    assert a.lifecycle_id != b.lifecycle_id


def test_detected_records_the_detected_timestamp() -> None:
    state = detected(BASE_TS)
    assert state.stage == "detected"
    assert state.stage_timestamps["detected"] == BASE_TS


# --- apply_transition: ordering rules -------------------------------------


def test_stages_must_advance_one_at_a_time_no_skipping() -> None:
    state = detected()
    with pytest.raises(InvalidTransitionError):
        apply_transition(state, "sent", BASE_TS + timedelta(minutes=1))


def test_stages_cannot_go_backward() -> None:
    state = apply_transition(detected(), "confirmed", BASE_TS + timedelta(minutes=1))
    state = apply_transition(state, "qualified", BASE_TS + timedelta(minutes=2))
    with pytest.raises(InvalidTransitionError):
        apply_transition(state, "confirmed", BASE_TS + timedelta(minutes=3))


def test_sequential_non_terminal_advance_succeeds() -> None:
    state = detected()
    for stage in NON_TERMINAL_STAGES[1:]:
        state = apply_transition(state, stage, BASE_TS + timedelta(minutes=1))
    assert state.stage == NON_TERMINAL_STAGES[-1]


@pytest.mark.parametrize("terminal_stage", TERMINAL_STAGES)
def test_terminal_stages_are_reachable_from_any_non_terminal_stage(terminal_stage: str) -> None:
    """A qualified-but-suppressed signal must be able to terminate
    without ever reaching 'sent'."""
    state = apply_transition(detected(), "confirmed", BASE_TS + timedelta(minutes=1))
    state = apply_transition(state, "qualified", BASE_TS + timedelta(minutes=2))
    kwargs = {"expiration_reason": "suppressed_duplicate"} if terminal_stage == "expired" else {}
    result = apply_transition(state, terminal_stage, BASE_TS + timedelta(minutes=3), **kwargs)
    assert result.stage == terminal_stage
    assert result.is_terminal


def test_no_transitions_allowed_once_terminal() -> None:
    state = apply_transition(detected(), "expired", BASE_TS + timedelta(minutes=1),
                              expiration_reason="lifetime_exceeded")
    with pytest.raises(InvalidTransitionError):
        apply_transition(state, "confirmed", BASE_TS + timedelta(minutes=2))


def test_expired_requires_an_expiration_reason() -> None:
    with pytest.raises(InvalidTransitionError):
        apply_transition(detected(), "expired", BASE_TS + timedelta(minutes=1))


def test_succeeded_and_failed_default_outcome_to_the_stage_name() -> None:
    succeeded = apply_transition(detected(), "succeeded", BASE_TS + timedelta(minutes=1))
    failed = apply_transition(detected(), "failed", BASE_TS + timedelta(minutes=1))
    assert succeeded.outcome == "succeeded"
    assert failed.outcome == "failed"


def test_unknown_stage_is_rejected() -> None:
    with pytest.raises(InvalidTransitionError):
        apply_transition(detected(), "made_up_stage", BASE_TS + timedelta(minutes=1))


def test_all_stages_covers_every_non_terminal_and_terminal_stage() -> None:
    assert set(ALL_STAGES) == set(NON_TERMINAL_STAGES) | set(TERMINAL_STAGES)


# --- measured fields -----------------------------------------------------


def test_detection_delay_is_none_before_confirmation() -> None:
    assert detected().detection_delay_seconds() is None


def test_detection_delay_is_the_gap_between_detected_and_confirmed() -> None:
    state = apply_transition(detected(BASE_TS), "confirmed", BASE_TS + timedelta(seconds=90))
    assert state.detection_delay_seconds() == pytest.approx(90.0)


def test_signal_age_grows_with_now() -> None:
    state = detected(BASE_TS)
    assert state.signal_age_seconds(BASE_TS + timedelta(minutes=30)) == pytest.approx(1800.0)


def test_signal_lifetime_is_none_while_not_terminal() -> None:
    state = apply_transition(detected(), "confirmed", BASE_TS + timedelta(minutes=1))
    assert state.signal_lifetime_seconds() is None


def test_signal_lifetime_is_set_once_terminal() -> None:
    state = apply_transition(
        detected(BASE_TS), "failed", BASE_TS + timedelta(minutes=45)
    )
    assert state.signal_lifetime_seconds() == pytest.approx(45 * 60)


def test_expiration_reason_only_set_on_expiry() -> None:
    state = apply_transition(detected(), "expired", BASE_TS + timedelta(minutes=1),
                              expiration_reason="lifetime_exceeded")
    assert state.expiration_reason == "lifetime_exceeded"

    other = apply_transition(detected(), "succeeded", BASE_TS + timedelta(minutes=1))
    assert other.expiration_reason is None


def test_outcome_only_set_on_succeeded_or_failed() -> None:
    state = apply_transition(detected(), "confirmed", BASE_TS + timedelta(minutes=1))
    assert state.outcome is None


def test_to_dict_includes_all_measured_fields() -> None:
    state = apply_transition(
        detected(BASE_TS), "succeeded", BASE_TS + timedelta(minutes=10), outcome="succeeded"
    )
    payload = state.to_dict(now=BASE_TS + timedelta(minutes=20))
    assert payload["lifecycle_id"] == state.lifecycle_id
    assert payload["signal_lifetime_seconds"] == pytest.approx(600.0)
    assert payload["outcome"] == "succeeded"
    assert payload["expiration_reason"] is None


# --- manager: persistence-backed round trip (real DB required) --------------


async def test_manager_without_a_db_still_computes_a_real_detected_state() -> None:
    manager = OpportunityLifecycleManager(session_factory=None)
    state = await manager.detect("NIFTY", "long")
    assert state.stage == "detected"
    assert state.symbol == "NIFTY"


async def test_manager_get_returns_none_without_a_session_factory() -> None:
    manager = OpportunityLifecycleManager(session_factory=None)
    assert await manager.get("anything") is None


async def test_manager_advance_without_a_db_raises_unknown_lifecycle() -> None:
    """No DB means get() can't reconstruct the lifecycle just detected in
    a prior in-memory call -- advancing an unknown id is a real error,
    not a silently fabricated success."""
    manager = OpportunityLifecycleManager(session_factory=None)
    with pytest.raises(InvalidTransitionError):
        await manager.confirm("some-id")


async def test_recent_returns_empty_list_without_a_session_factory() -> None:
    manager = OpportunityLifecycleManager(session_factory=None)
    assert await manager.recent() == []


# --- replay_transitions: self-healing against a corrupted log ---------------


def _row(stage: str, at: datetime, **extra) -> dict:
    return {"stage": stage, "at": at.isoformat(), "symbol": "NIFTY", "direction": "long", **extra}


def test_replay_transitions_reconstructs_a_clean_log() -> None:
    rows = [
        _row("detected", BASE_TS),
        _row("confirmed", BASE_TS + timedelta(minutes=1)),
        _row("qualified", BASE_TS + timedelta(minutes=2)),
    ]
    state = replay_transitions(rows, "lc-1")
    assert state is not None
    assert state.stage == "qualified"


def test_replay_transitions_skips_a_duplicate_consecutive_stage_row() -> None:
    """The exact corruption pattern a racing/retried _advance() call used
    to produce: two consecutive rows at the same stage. Replay must skip
    the duplicate rather than raising, so the record stays readable."""
    rows = [
        _row("detected", BASE_TS),
        _row("confirmed", BASE_TS + timedelta(minutes=1)),
        _row("confirmed", BASE_TS + timedelta(minutes=1, seconds=5)),  # the duplicate write
        _row("qualified", BASE_TS + timedelta(minutes=2)),
    ]
    state = replay_transitions(rows, "lc-1")
    assert state is not None
    assert state.stage == "qualified"  # replay proceeded past the duplicate, not corrupted


def test_replay_transitions_on_empty_rows_is_none() -> None:
    assert replay_transitions([], "lc-1") is None


def test_replay_transitions_still_raises_on_a_genuinely_invalid_sequence() -> None:
    """Only an exact duplicate-of-the-current-stage is tolerated -- a real
    skipped-stage sequence (not a duplicate) is still a genuine error,
    not silently swallowed."""
    rows = [_row("detected", BASE_TS), _row("sent", BASE_TS + timedelta(minutes=1))]
    with pytest.raises(InvalidTransitionError):
        replay_transitions(rows, "lc-1")


# --- _advance: idempotency guard against duplicate/retried calls ------------


async def test_advance_is_idempotent_for_a_duplicate_call_at_the_current_stage(
    monkeypatch,
) -> None:
    """A retried/duplicate call re-requesting the stage already reached
    must be a safe no-op -- no new row written, no exception raised."""
    manager = OpportunityLifecycleManager(session_factory=None)
    confirmed = apply_transition(
        apply_transition(None, "detected", BASE_TS, symbol="NIFTY", direction="long"),
        "confirmed", BASE_TS + timedelta(minutes=1),
    )
    persisted = []

    async def fake_get(lifecycle_id: str) -> LifecycleState:
        return confirmed

    async def fake_persist(new_state: LifecycleState, at: datetime) -> None:
        persisted.append(new_state.stage)

    monkeypatch.setattr(manager, "get", fake_get)
    monkeypatch.setattr(manager, "_persist", fake_persist)

    result = await manager.confirm(confirmed.lifecycle_id)
    assert result.stage == "confirmed"
    assert persisted == []  # nothing written -- the idempotent no-op path, not a fresh transition


async def test_advance_still_advances_normally_when_not_a_duplicate(monkeypatch) -> None:
    manager = OpportunityLifecycleManager(session_factory=None)
    state = apply_transition(None, "detected", BASE_TS, symbol="NIFTY", direction="long")
    persisted = []

    async def fake_get(lifecycle_id: str) -> LifecycleState:
        return state

    async def fake_persist(new_state: LifecycleState, at: datetime) -> None:
        persisted.append(new_state.stage)

    monkeypatch.setattr(manager, "get", fake_get)
    monkeypatch.setattr(manager, "_persist", fake_persist)

    result = await manager.confirm(state.lifecycle_id)
    assert result.stage == "confirmed"
    assert persisted == ["confirmed"]


# --- _advance: lock serializes concurrent calls for the same id -------------


async def test_lock_for_returns_the_same_lock_for_the_same_id() -> None:
    manager = OpportunityLifecycleManager(session_factory=None)
    assert manager._lock_for("lc-1") is manager._lock_for("lc-1")


async def test_lock_for_returns_different_locks_for_different_ids() -> None:
    manager = OpportunityLifecycleManager(session_factory=None)
    assert manager._lock_for("lc-1") is not manager._lock_for("lc-2")


async def test_concurrent_duplicate_calls_do_not_corrupt_state(monkeypatch) -> None:
    """The exact scenario from the bug report: two concurrent calls to
    confirm() for the SAME lifecycle_id (a retried request, a duplicate
    delivery -- made likelier by every lifecycle route being a GET).
    Without the lock, both would read "detected" at once and both write a
    "confirmed" row -- two consecutive identical-stage rows, corrupting
    future replay. With the lock + idempotency guard, the second call
    sees the first's write and takes the no-op path: exactly one row
    written, no exception, both callers get a valid "confirmed" state."""
    manager = OpportunityLifecycleManager(session_factory=None)
    backing_store = {
        "state": apply_transition(None, "detected", BASE_TS, symbol="NIFTY", direction="long"),
    }
    persisted_stages = []

    async def fake_get(lifecycle_id: str) -> LifecycleState:
        await asyncio.sleep(0.01)  # force a real yield, so a race WOULD show up unguarded
        return backing_store["state"]

    async def fake_persist(new_state: LifecycleState, at: datetime) -> None:
        await asyncio.sleep(0)
        persisted_stages.append(new_state.stage)
        backing_store["state"] = new_state

    monkeypatch.setattr(manager, "get", fake_get)
    monkeypatch.setattr(manager, "_persist", fake_persist)

    lifecycle_id = backing_store["state"].lifecycle_id
    results = await asyncio.gather(
        manager.confirm(lifecycle_id), manager.confirm(lifecycle_id),
    )
    assert all(r.stage == "confirmed" for r in results)
    assert persisted_stages == ["confirmed"]  # only ONE write -- the second was the no-op
