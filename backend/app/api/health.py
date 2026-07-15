"""Health-check endpoints.

- /health/live  — process is up (no external dependencies touched)
- /health/ready — PostgreSQL and Redis are reachable
"""

import redis.asyncio as aioredis
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import APIRouter, Response
from sqlalchemy import text

from app.core.config import get_settings
from app.core.container import container
from app.core.system_metrics import SystemMetricsSampler
from app.database.session import get_engine

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict:
    settings = get_settings()
    return {"status": "ok", "app": settings.app_name, "environment": settings.environment}


@router.get("/ready")
async def ready(response: Response) -> dict:
    checks: dict[str, str] = {}

    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # pragma: no cover - depends on infra
        checks["postgres"] = f"error: {exc}"

    try:
        client = aioredis.from_url(get_settings().redis_url)
        await client.ping()
        await client.aclose()
        checks["redis"] = "ok"
    except Exception as exc:  # pragma: no cover - depends on infra
        checks["redis"] = f"error: {exc}"

    healthy = all(v == "ok" for v in checks.values())
    if not healthy:
        response.status_code = 503
    return {"status": "ok" if healthy else "degraded", "checks": checks}


@router.get("/system")
async def system_metrics() -> dict:
    """Process/system CPU and memory (Chapter 12 monitoring)."""
    sampler = container.resolve(SystemMetricsSampler)
    return sampler.snapshot()


@router.get("/scheduler/status")
async def scheduler_status() -> dict:
    """Every background job (collectors, feature engines, the three
    intelligence sweeps) and whether the scheduler is currently running --
    pair with pause/resume below to isolate request-path latency from
    background contention."""
    scheduler = container.resolve(AsyncIOScheduler)
    return {
        "running": scheduler.running,
        "jobs": [
            {
                "id": job.id,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            }
            for job in scheduler.get_jobs()
        ],
    }


@router.post("/scheduler/pause")
async def scheduler_pause() -> dict:
    """Pause every scheduled job (collectors, feature engines, sweeps)
    without stopping the process -- the request path (API routes) keeps
    working normally. Use /scheduler/resume to undo; does NOT persist
    across a process restart (APScheduler's in-memory job store resets on
    boot, so a restart resumes normal scheduling). Intended for isolating
    request-path latency during an incident or a live perf comparison, not
    for routine use -- background collection stays off, and stops
    refreshing live data, for as long as this is left paused."""
    scheduler = container.resolve(AsyncIOScheduler)
    scheduler.pause()
    return {"status": "paused"}


@router.post("/scheduler/resume")
async def scheduler_resume() -> dict:
    scheduler = container.resolve(AsyncIOScheduler)
    scheduler.resume()
    return {"status": "resumed"}
