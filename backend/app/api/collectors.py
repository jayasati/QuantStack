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
    registry.disable(name)
    return {"collector": name, "enabled": False}


@router.post("/{name}/run")
async def run_collector_now(name: str) -> dict:
    registry = container.resolve(CollectorRegistry)
    collector = registry.get(name)
    if collector is None:
        raise HTTPException(status_code=404, detail=f"unknown collector: {name}")
    await registry.run_collector(name, force=True)
    return registry.health_of(name) or {}
