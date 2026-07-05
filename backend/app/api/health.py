"""Health-check endpoints.

- /health/live  — process is up (no external dependencies touched)
- /health/ready — PostgreSQL and Redis are reachable
"""

import redis.asyncio as aioredis
from fastapi import APIRouter, Response
from sqlalchemy import text

from app.core.config import get_settings
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
