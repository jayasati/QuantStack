"""Circuit breaker unit tests (Volume 1, Chapter 13 gap-fill)."""

import time

from app.core.circuit_breaker import CircuitBreaker, CircuitBreakerRegistry, CircuitState


def test_closed_allows_requests_and_absorbs_isolated_failures() -> None:
    breaker = CircuitBreaker(name="x", failure_threshold=3)
    assert breaker.allow_request()
    assert breaker.record_failure("boom") is False
    assert breaker.state == CircuitState.CLOSED
    assert breaker.record_success() is False  # already closed, not a "recovery"
    assert breaker.consecutive_failures == 0


def test_opens_after_threshold_consecutive_failures() -> None:
    breaker = CircuitBreaker(name="x", failure_threshold=3)
    assert breaker.record_failure("1") is False
    assert breaker.record_failure("2") is False
    assert breaker.record_failure("3") is True  # this call trips it
    assert breaker.state == CircuitState.OPEN
    assert breaker.total_opens == 1
    assert breaker.allow_request() is False  # fails fast, no probe yet


def test_half_open_probe_after_recovery_timeout_then_closes_on_success() -> None:
    breaker = CircuitBreaker(name="x", failure_threshold=1, recovery_timeout=0.05)
    breaker.record_failure("down")
    assert breaker.state == CircuitState.OPEN
    assert breaker.allow_request() is False

    time.sleep(0.06)
    assert breaker.allow_request() is True  # single probe let through
    assert breaker.state == CircuitState.HALF_OPEN
    assert breaker.allow_request() is False  # second concurrent call still rejected

    assert breaker.record_success() is True  # probe succeeded -> recovered
    assert breaker.state == CircuitState.CLOSED
    assert breaker.consecutive_failures == 0


def test_half_open_probe_failure_reopens_circuit() -> None:
    breaker = CircuitBreaker(name="x", failure_threshold=1, recovery_timeout=0.02)
    breaker.record_failure("down")
    time.sleep(0.03)
    assert breaker.allow_request() is True
    assert breaker.state == CircuitState.HALF_OPEN

    assert breaker.record_failure("still down") is True
    assert breaker.state == CircuitState.OPEN
    assert breaker.total_opens == 2


def test_registry_reuses_named_breakers_and_snapshots() -> None:
    registry = CircuitBreakerRegistry(failure_threshold=2, recovery_timeout=10.0)
    a = registry.get("svc.a")
    a2 = registry.get("svc.a")
    b = registry.get("svc.b")
    assert a is a2
    assert a is not b
    assert a.failure_threshold == 2

    a.record_failure("1")
    a.record_failure("2")
    snapshot = registry.snapshot()
    names = {row["name"]: row for row in snapshot}
    assert names["svc.a"]["state"] == "open"
    assert names["svc.b"]["state"] == "closed"
