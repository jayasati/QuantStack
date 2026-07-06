"""Collector observability API (Volume 2, Chapter 20)."""

from fastapi import APIRouter, HTTPException

from app.collectors.registry import CollectorRegistry
from app.core.container import container
from app.events.bus import EventBus

router = APIRouter(prefix="/collectors", tags=["collectors"])


@router.get("")
async def list_collectors() -> list[dict]:
    registry = container.resolve(CollectorRegistry)
    return registry.list_collectors()


@router.get("/events/metrics")
async def event_bus_metrics() -> dict:
    bus = container.resolve(EventBus)
    return bus.metrics()


@router.get("/{name}")
async def collector_health(name: str) -> dict:
    registry = container.resolve(CollectorRegistry)
    health = registry.health_of(name)
    if health is None:
        raise HTTPException(status_code=404, detail=f"unknown collector: {name}")
    return health


@router.get("/{name}/quality")
async def collector_quality_history(name: str, limit: int = 50) -> dict:
    """Persisted quality metrics for a collector (Prompt 2.11 monitoring)."""
    registry = container.resolve(CollectorRegistry)
    collector = registry.get(name)
    if collector is None:
        raise HTTPException(status_code=404, detail=f"unknown collector: {name}")
    from sqlalchemy import desc, select

    from app.database.session import get_session_factory
    from app.database.tables import CollectorHealth

    sessions = get_session_factory()
    async with sessions() as session:
        result = await session.execute(
            select(
                CollectorHealth.created_at,
                CollectorHealth.quality_score,
                CollectorHealth.data,
            )
            .where(CollectorHealth.collector_name == name)
            .order_by(desc(CollectorHealth.id))
            .limit(min(max(limit, 1), 500))
        )
        rows = result.all()
    return {
        "collector": name,
        "current_components": collector.health.extras.get("quality_components"),
        "history": [
            {
                "at": created_at.isoformat(),
                "quality_score": quality,
                **(data or {}),
            }
            for created_at, quality, data in rows
        ],
    }


@router.post("/{name}/enable")
async def enable_collector(name: str) -> dict:
    registry = container.resolve(CollectorRegistry)
    if registry.get(name) is None:
        raise HTTPException(status_code=404, detail=f"unknown collector: {name}")
    registry.enable(name)
    return {"collector": name, "enabled": True}


@router.post("/{name}/disable")
async def disable_collector(name: str) -> dict:
    registry = container.resolve(CollectorRegistry)
    if registry.get(name) is None:
        raise HTTPException(status_code=404, detail=f"unknown collector: {name}")
    dependents = registry.disable(name)
    return {"collector": name, "enabled": False, "active_dependents": dependents}


@router.post("/{name}/run")
async def run_collector_now(name: str) -> dict:
    registry = container.resolve(CollectorRegistry)
    collector = registry.get(name)
    if collector is None:
        raise HTTPException(status_code=404, detail=f"unknown collector: {name}")
    await registry.run_collector(name, force=True)
    return registry.health_of(name) or {}
