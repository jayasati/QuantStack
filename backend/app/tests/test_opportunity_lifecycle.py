"""Tests for the Opportunity Lifecycle Manager (Volume 5, Prompt 5.15)."""

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
