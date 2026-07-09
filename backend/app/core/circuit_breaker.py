"""Circuit breaker (Volume 1, Chapter 13: Network -> Retry -> Exponential
Backoff -> Circuit Breaker -> Fallback -> Alert).

Sits after retry/backoff has already been exhausted for a call: once a
dependency fails ``failure_threshold`` times in a row, the circuit opens and
subsequent calls fail fast (no network attempt) until ``recovery_timeout``
elapses, at which point a single probe call is let through (half-open). A
successful probe closes the circuit; a failed probe re-opens it.
"""

import time
from dataclasses import dataclass, field
from enum import StrEnum


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(Exception):
    """Raised by callers that choose to reject instead of calling allow_request()."""


@dataclass
class CircuitBreaker:
    """Per-dependency breaker. Not thread-safe across event loops (not needed:
    the app is single-process asyncio)."""

    name: str
    failure_threshold: int = 3
    recovery_timeout: float = 60.0
    half_open_max_calls: int = 1

    state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    consecutive_failures: int = field(default=0, init=False)
    opened_at: float | None = field(default=None, init=False)
    total_opens: int = field(default=0, init=False)
    last_error: str | None = field(default=None, init=False)
    _half_open_calls: int = field(default=0, init=False, repr=False)

    def allow_request(self) -> bool:
        """Whether a call should be attempted right now. Advances OPEN ->
        HALF_OPEN once the recovery timeout has elapsed."""
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if self.opened_at is not None and (
                time.monotonic() - self.opened_at
            ) >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                self._half_open_calls = 0
            else:
                return False
        # HALF_OPEN: allow a bounded number of probe calls through.
        if self._half_open_calls >= self.half_open_max_calls:
            return False
        self._half_open_calls += 1
        return True

    def record_success(self) -> bool:
        """Call after a successful attempt. Returns True if this closed the
        circuit (i.e. the dependency just recovered)."""
        recovered = self.state != CircuitState.CLOSED
        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self.opened_at = None
        self._half_open_calls = 0
        return recovered

    def record_failure(self, error: str | None = None) -> bool:
        """Call after a failed attempt. Returns True if this call caused the
        circuit to (re-)open."""
        self.last_error = error
        if self.state == CircuitState.HALF_OPEN:
            self._open()
            return True
        self.consecutive_failures += 1
        still_closed = self.state == CircuitState.CLOSED
        if self.consecutive_failures >= self.failure_threshold and still_closed:
            self._open()
            return True
        return False

    def _open(self) -> None:
        self.state = CircuitState.OPEN
        self.opened_at = time.monotonic()
        self.total_opens += 1

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
            "total_opens": self.total_opens,
            "last_error": self.last_error,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
        }


class CircuitBreakerRegistry:
    """Named breakers sharing default thresholds, exposed for observability."""

    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 60.0) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(
        self,
        name: str,
        failure_threshold: int | None = None,
        recovery_timeout: float | None = None,
    ) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(
                name=name,
                failure_threshold=failure_threshold or self._failure_threshold,
                recovery_timeout=recovery_timeout or self._recovery_timeout,
            )
        return self._breakers[name]

    def snapshot(self) -> list[dict]:
        return [b.to_dict() for b in self._breakers.values()]
