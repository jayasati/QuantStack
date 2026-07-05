"""Default collector pipeline: quality gate -> persistence -> event publishing.

Injected into every collector's ``run_once`` so collectors stay decoupled
from the quality engine, database, and event bus. A storage failure degrades
the run but never blocks event publishing — collector failures must never
cascade into unrelated components.
"""

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collectors.base import BaseCollector, CollectorPipeline
from app.collectors.quality import DataQualityEngine
from app.collectors.schema import CollectorOutput
from app.core.logging import get_logger
from app.database.tables import CollectorHealth, MarketEvent
from app.events.bus import Event, EventBus

logger = get_logger(__name__)


class DefaultCollectorPipeline(CollectorPipeline):
    def __init__(
        self,
        bus: EventBus,
        quality_engine: DataQualityEngine | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._bus = bus
        self._quality = quality_engine or DataQualityEngine()
        self._session_factory = session_factory

    async def process(
        self, collector: BaseCollector, records: list[CollectorOutput], latency_ms: float
    ) -> list[CollectorOutput]:
        assessment = self._quality.assess(collector, records, latency_ms)
        records = self._quality.apply(records, assessment)
        collector.health.last_quality_score = assessment.quality_score
        collector.health.extras["quality_components"] = assessment.components

        await self._persist(collector, records, assessment.quality_score)

        for record in records:
            await self._bus.publish(
                Event(
                    type=f"collector.{collector.category.value}.updated",
                    payload=record.model_dump(mode="json"),
                    source=collector.name,
                )
            )
        return records

    async def record_failure(self, collector: BaseCollector, error: Exception) -> None:
        await self._persist_health(collector, quality_score=0.0, error=str(error))
        await self._bus.publish(
            Event(
                type="collector.failed",
                payload={"collector": collector.name, "error": str(error)},
                source=collector.name,
            )
        )

    # --- persistence (tolerant: DB issues degrade, never crash) -----------------

    async def _persist(
        self, collector: BaseCollector, records: list[CollectorOutput], quality: float
    ) -> None:
        if self._session_factory is None or not records:
            await self._persist_health(collector, quality)
            return
        try:
            async with self._session_factory() as session:
                await session.execute(
                    insert(MarketEvent),
                    [
                        {
                            "event_type": f"{collector.category.value}.observation",
                            "source": collector.name,
                            "data": record.model_dump(mode="json"),
                        }
                        for record in records
                    ],
                )
                await session.commit()
        except Exception as exc:
            collector.health.status = "degraded"
            logger.error(
                "failed to persist collector records",
                extra={"collector": collector.name, "error": str(exc)},
            )
        await self._persist_health(collector, quality)

    async def _persist_health(
        self, collector: BaseCollector, quality_score: float, error: str | None = None
    ) -> None:
        if self._session_factory is None:
            return
        try:
            async with self._session_factory() as session:
                await session.execute(
                    insert(CollectorHealth),
                    [
                        {
                            "collector_name": collector.name,
                            "quality_score": quality_score,
                            "data": {
                                "status": collector.health.status,
                                "failure_rate": collector.health.failure_rate,
                                "avg_latency_ms": collector.health.avg_latency_ms,
                                "consecutive_failures": collector.health.consecutive_failures,
                                "error": error,
                            },
                        }
                    ],
                )
                await session.commit()
        except Exception as exc:
            logger.error(
                "failed to persist collector health",
                extra={"collector": collector.name, "error": str(exc)},
            )
