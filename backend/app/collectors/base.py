"""Base collector (Volume 2, Chapters 1, 3, 4).

Every collector follows exactly the same lifecycle:

    initialize -> authenticate -> collect -> validate -> normalize
        -> calculate_confidence -> quality gate -> store -> publish -> sleep

Collectors are independent, restartable, own their schedule, expose health
metrics, produce the standard output schema, and must never crash the system:
``run_once`` isolates every failure and records it instead of raising.
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.collectors.schema import CollectorCategory, CollectorOutput
from app.core.logging import get_logger


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

    def __init__(self) -> None:
        self.logger = get_logger(f"collector.{self.name}")
        self.health = CollectorHealthStatus(name=self.name, category=self.category.value)
        self._initialized = False

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

    async def run_once(self, pipeline: "CollectorPipeline") -> list[CollectorOutput]:
        """Execute one full lifecycle pass. Never raises."""
        started = time.perf_counter()
        self.health.last_run = datetime.now(UTC)
        self.health.run_count += 1
        try:
            if not self._initialized:
                await self.initialize()
                self._initialized = True
            if self.requires_auth:
                await self.authenticate()

            records = await self.collect()
            records = await self.validate(records)
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
