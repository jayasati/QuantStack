"""Base collector (Volume 2, Chapters 1, 3, 4).

Every collector follows exactly the same lifecycle:

    initialize -> authenticate -> collect -> validate -> normalize
        -> calculate_confidence -> quality gate -> store -> publish -> sleep

Collectors are independent, restartable, own their schedule, expose health
metrics, produce the standard output schema, and must never crash the system:
``run_once`` isolates every failure and records it instead of raising.
"""

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.collectors.schema import CollectorCategory, CollectorOutput
from app.core.alerts import AlertService, AlertSeverity
from app.core.circuit_breaker import CircuitBreaker
from app.core.config import get_settings
from app.core.logging import get_logger

IST = ZoneInfo("Asia/Kolkata")


def is_nse_market_open(now: datetime | None = None) -> bool:
    """NSE equity/derivatives session: Mon-Fri 09:15-15:35 IST (incl. close
    prints), excluding configured exchange holidays (feature_market_holidays)
    -- a weekday holiday (e.g. 2026-03-04, a Wednesday) previously still
    counted as "open" here, since only weekday + time-of-day were checked."""
    current = (now or datetime.now(UTC)).astimezone(IST)
    if current.weekday() >= 5:
        return False
    if current.date().isoformat() in get_settings().feature_market_holidays:
        return False
    minutes = current.hour * 60 + current.minute
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 35


class CollectorError(Exception):
    """Base class for collector failures."""


class AuthenticationError(CollectorError):
    pass


class CollectionError(CollectorError):
    pass


class ValidationError(CollectorError):
    pass


@dataclass
class CollectorHealthStatus:
    """Observability metrics exposed via the health API (Chapter 20)."""

    name: str
    category: str
    enabled: bool = True
    status: str = "idle"  # idle | ok | degraded | failed | disabled
    last_run: datetime | None = None
    last_success: datetime | None = None
    next_run: datetime | None = None
    run_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    retry_count: int = 0
    avg_latency_ms: float = 0.0
    last_quality_score: float | None = None
    last_error: str | None = None
    records_emitted: int = 0
    # True while a run_once() call is actively executing (past the market-hours
    # /circuit-breaker gates). queue_length counts concurrent run_once() callers
    # currently waiting their turn — e.g. a manual /run request arriving while a
    # scheduled run is still in flight; normally 0 since APScheduler's own
    # max_instances=1 already prevents scheduled overlap.
    in_flight: bool = False
    queue_length: int = 0
    extras: dict = field(default_factory=dict)

    @property
    def failure_rate(self) -> float:
        return self.failure_count / self.run_count if self.run_count else 0.0


class BaseCollector(ABC):
    """Standard collector interface. No collector may bypass this lifecycle."""

    name: str = "base"
    category: CollectorCategory = CollectorCategory.MARKET_DATA
    source: str = "unknown"
    interval_seconds: int = 60
    priority: int = 100  # lower runs earlier when schedules collide
    requires_auth: bool = False
    market_hours_only: bool = False  # scheduled runs skip outside NSE hours
    after_hours_only: bool = False  # scheduled runs skip DURING NSE hours --
    # for sources that only publish once, after close (bhavcopy, FII/DII
    # flow reports): running while the market is open just checks for data
    # that provably isn't published yet.
    depends_on: tuple[str, ...] = ()  # collector names this one consumes data from

    def __init__(self) -> None:
        self.logger = get_logger(f"collector.{self.name}")
        self.health = CollectorHealthStatus(name=self.name, category=self.category.value)
        self._initialized = False
        # Error-handling escalation chain (Chapter 13): after repeated
        # failures, fail fast instead of hammering a down dependency every
        # scheduled interval. Threshold mirrors the existing "failed" status
        # cutoff below. alerts is injected by CollectorRegistry.register().
        self.circuit_breaker = CircuitBreaker(name=f"collector.{self.name}")
        self.alerts: AlertService | None = None
        self._run_lock = asyncio.Lock()

    async def _fire_alert(self, severity: AlertSeverity, message: str, **context) -> None:
        if self.alerts is not None:
            await self.alerts.fire(f"collector.{self.name}", severity, message, **context)
        else:
            self.logger.error(message, extra=context)

    # --- lifecycle hooks (override as needed) ---------------------------------

    async def initialize(self) -> None:  # noqa: B027
        """One-time setup (connections, instrument lists)."""

    async def authenticate(self) -> None:  # noqa: B027
        """Establish/refresh credentials. Only called when requires_auth."""

    @abstractmethod
    async def collect(self) -> list[CollectorOutput]:
        """Fetch raw data and map it into the standard output schema."""

    async def validate(self, records: list[CollectorOutput]) -> list[CollectorOutput]:
        """Drop or repair invalid records. Raise ValidationError if unusable."""
        return records

    async def normalize(self, records: list[CollectorOutput]) -> list[CollectorOutput]:
        """Post-process normalized values (already schema-shaped by collect)."""
        return records

    async def calculate_confidence(
        self, records: list[CollectorOutput]
    ) -> list[CollectorOutput]:
        """Set per-record confidence. Default: leave collector-provided values."""
        return records

    async def cleanup(self) -> None:  # noqa: B027
        """Release resources on shutdown."""

    # --- orchestration ---------------------------------------------------------

    async def run_once(
        self, pipeline: "CollectorPipeline", force: bool = False
    ) -> list[CollectorOutput]:
        """Execute one full lifecycle pass. Never raises.

        Scheduled runs of market-hours-only collectors are skipped outside
        NSE trading hours; after-hours-only collectors are skipped during
        them. Pass ``force=True`` (manual /run) to bypass either gate.
        """
        if self.market_hours_only and not force and not is_nse_market_open():
            self.health.extras["skipped_market_closed"] = (
                self.health.extras.get("skipped_market_closed", 0) + 1
            )
            return []
        if self.after_hours_only and not force and is_nse_market_open():
            self.health.extras["skipped_market_open"] = (
                self.health.extras.get("skipped_market_open", 0) + 1
            )
            return []
        if not self.circuit_breaker.allow_request():
            self.health.status = "circuit_open"
            self.health.extras["circuit_skipped"] = self.health.extras.get("circuit_skipped", 0) + 1
            self.health.extras["circuit_breaker"] = self.circuit_breaker.to_dict()
            return []

        self.health.queue_length += 1
        async with self._run_lock:
            self.health.queue_length -= 1
            self.health.in_flight = True
            try:
                return await self._execute(pipeline)
            finally:
                self.health.in_flight = False

    async def _collect_with_retry(self) -> list[CollectorOutput]:
        """A small, flat-delay retry around collect() (Chapter 20's
        retry_count, Chapter 13's escalation chain applied per-collector).

        Deliberately not max_retry's exponential network backoff: most
        sources already retry internally (AngelOneAdapter, NseSession), so
        this only needs to smooth over a blip they didn't catch, not stack a
        second multi-second backoff on top of theirs. A persistent failure
        still escalates to degraded/failed + the circuit breaker exactly as
        before, just after one quick extra attempt.
        """
        settings = get_settings()
        attempts = max(settings.collector_retry_attempts, 0)
        last_exc: Exception = CollectionError("collect() never ran")
        for attempt in range(1, attempts + 2):
            try:
                return await self.collect()
            except Exception as exc:
                last_exc = exc
                if attempt > attempts:
                    raise
                self.health.retry_count += 1
                self.logger.warning(
                    "collect() failed; retrying",
                    extra={"collector": self.name, "attempt": attempt, "error": str(exc)},
                )
                await asyncio.sleep(settings.collector_retry_delay_seconds)
        raise last_exc  # unreachable: the loop always returns or raises above

    async def _execute(self, pipeline: "CollectorPipeline") -> list[CollectorOutput]:
        started = time.perf_counter()
        self.health.last_run = datetime.now(UTC)
        self.health.run_count += 1
        try:
            if not self._initialized:
                await self.initialize()
                self._initialized = True
            if self.requires_auth:
                await self.authenticate()

            records = await self._collect_with_retry()
            collected_count = len(records)
            records = await self.validate(records)
            self.health.extras["last_run_collected"] = collected_count
            self.health.extras["last_run_validation_dropped"] = (
                collected_count - len(records)
            )
            records = await self.normalize(records)
            records = await self.calculate_confidence(records)

            latency_ms = (time.perf_counter() - started) * 1000
            for record in records:
                record.latency_ms = record.latency_ms or latency_ms

            records = await pipeline.process(self, records, latency_ms)

            self.health.status = "ok"
            self.health.last_success = datetime.now(UTC)
            self.health.consecutive_failures = 0
            self.health.records_emitted += len(records)
            self.health.last_error = None
            self._update_latency(latency_ms)
            if self.circuit_breaker.record_success():
                self.health.extras["circuit_breaker"] = self.circuit_breaker.to_dict()
                await self._fire_alert(
                    AlertSeverity.INFO, f"collector {self.name} circuit breaker recovered"
                )
            return records
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            self._update_latency(latency_ms)
            self.health.failure_count += 1
            self.health.consecutive_failures += 1
            self.health.status = "failed" if self.health.consecutive_failures >= 3 else "degraded"
            self.health.last_error = f"{type(exc).__name__}: {exc}"
            self.logger.error(
                "collector run failed",
                extra={"collector": self.name, "error": str(exc)},
            )
            if self.circuit_breaker.record_failure(str(exc)):
                self.health.extras["circuit_breaker"] = self.circuit_breaker.to_dict()
                await self._fire_alert(
                    AlertSeverity.CRITICAL,
                    f"collector {self.name} circuit breaker opened after repeated failures",
                    error=str(exc),
                )
            await pipeline.record_failure(self, exc)
            return []

    def _update_latency(self, latency_ms: float) -> None:
        prev = self.health.avg_latency_ms
        n = self.health.run_count
        self.health.avg_latency_ms = prev + (latency_ms - prev) / max(n, 1)


class CollectorPipeline(ABC):
    """Post-collection processing: quality gate, storage, event publishing.

    Injected into ``run_once`` so collectors stay decoupled from the quality
    engine, database, and event bus (and are trivially testable).
    """

    @abstractmethod
    async def process(
        self, collector: BaseCollector, records: list[CollectorOutput], latency_ms: float
    ) -> list[CollectorOutput]: ...

    @abstractmethod
    async def record_failure(self, collector: BaseCollector, error: Exception) -> None: ...
