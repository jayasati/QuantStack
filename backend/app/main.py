"""QuantStack backend application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.collectors import router as collectors_router
from app.api.health import router as health_router
from app.collectors.registry import CollectorRegistry
from app.core.config import get_settings
from app.core.container import container, wire_default_services
from app.core.logging import get_logger, setup_logging
from app.database.session import dispose_engine
from app.market.broker import BrokerInterface
from app.scheduler.service import start_scheduler

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    wire_default_services()

    broker = container.resolve(BrokerInterface)
    try:
        await broker.connect()
    except Exception as exc:
        logger.error("broker connect failed at startup", extra={"error": str(exc)})

    registry = container.resolve(CollectorRegistry)
    discovered = registry.discover()

    scheduler = start_scheduler()
    scheduled = registry.schedule_all(scheduler)
    logger.info(
        "application started",
        extra={
            "app": settings.app_name,
            "environment": settings.environment,
            "collectors_discovered": discovered,
            "collectors_scheduled": scheduled,
        },
    )
    yield
    scheduler.shutdown(wait=False)
    await registry.shutdown()
    await broker.disconnect()
    await dispose_engine()
    logger.info("application stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.2.0", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(collectors_router)
    return app


app = create_app()
