"""QuantStack backend application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.collectors import router as collectors_router
from app.api.features import router as features_router
from app.api.health import router as health_router
from app.collectors.registry import CollectorRegistry
from app.core.config import get_settings
from app.core.container import container, wire_default_services
from app.core.logging import get_logger, setup_logging
from app.database.session import dispose_engine
from app.features.breadth import BreadthFeatureEngine
from app.features.events import EventRiskEngine
from app.features.liquidity import LiquidityFeatureEngine
from app.features.news import NewsFeatureEngine
from app.features.options import OptionsFeatureEngine
from app.features.price import PriceFeatureEngine
from app.features.relative import RelativeStrengthEngine
from app.features.sector import SectorFeatureEngine
from app.features.structure import MarketStructureEngine
from app.features.timefeat import TimeFeatureEngine
from app.features.volatility import VolatilityFeatureEngine
from app.features.volume import VolumeFeatureEngine
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

    feature_engines = [
        container.resolve(PriceFeatureEngine),
        container.resolve(VolumeFeatureEngine),
        container.resolve(VolatilityFeatureEngine),
        container.resolve(LiquidityFeatureEngine),
        container.resolve(OptionsFeatureEngine),
        container.resolve(BreadthFeatureEngine),
        container.resolve(SectorFeatureEngine),
        container.resolve(RelativeStrengthEngine),
        container.resolve(MarketStructureEngine),
        container.resolve(NewsFeatureEngine),
        container.resolve(EventRiskEngine),
        container.resolve(TimeFeatureEngine),
    ]
    for engine in feature_engines:
        try:
            await engine.sync_registry()
        except Exception as exc:
            logger.error(
                "feature registry sync failed",
                extra={"engine": engine.name, "error": str(exc)},
            )
        scheduler.add_job(
            engine.run_all,
            trigger="interval",
            seconds=settings.feature_engine_interval,
            id=f"features.{engine.category}",
            replace_existing=True,
        )

    async def feature_health_sweep() -> None:
        """Quality scores and drift detection across every stored feature."""
        from app.database.session import get_session_factory
        from app.features.drift import FeatureDriftEngine
        from app.features.quality import FeatureQualityEngine

        sessions = get_session_factory()
        await FeatureQualityEngine(sessions).evaluate_all()
        await FeatureDriftEngine(sessions).detect_all()

    scheduler.add_job(
        feature_health_sweep,
        trigger="interval",
        seconds=settings.feature_health_interval,
        id="features.health",
        replace_existing=True,
    )

    logger.info(
        "application started",
        extra={
            "app": settings.app_name,
            "environment": settings.environment,
            "collectors_discovered": discovered,
            "collectors_scheduled": scheduled,
            "features_registered": sum(
                len(engine.registry.list_definitions()) for engine in feature_engines
            ),
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
    app.include_router(features_router)
    return app


app = create_app()
