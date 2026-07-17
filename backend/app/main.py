"""QuantStack backend application entry point."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from app.api.collectors import router as collectors_router
from app.api.dashboard import router as dashboard_router
from app.api.features import router as features_router
from app.api.health import router as health_router
from app.api.intelligence import router as intelligence_router
from app.api.prediction import router as prediction_router
from app.collectors.registry import CollectorRegistry
from app.core.config import get_settings
from app.core.container import container, wire_default_services
from app.core.logging import get_logger, setup_logging
from app.database.session import dispose_engine
from app.features.breadth import BreadthFeatureEngine
from app.features.events import EventRiskEngine
from app.features.institutional_flow import InstitutionalFlowFeatureEngine
from app.features.intraday_risk import IntradayRiskFeatureEngine
from app.features.liquidity import LiquidityFeatureEngine
from app.features.macro import MacroFeatureEngine
from app.features.news import NewsFeatureEngine
from app.features.options import OptionsFeatureEngine
from app.features.price import PriceFeatureEngine
from app.features.relative import RelativeStrengthEngine
from app.features.risk import RiskFeatureEngine
from app.features.sector import SectorFeatureEngine
from app.features.structure import MarketStructureEngine
from app.features.timefeat import TimeFeatureEngine
from app.features.volatility import VolatilityFeatureEngine
from app.features.volume import VolumeFeatureEngine
from app.intelligence.composite import CompositeMarketIntelligenceEngine
from app.intelligence.report import MarketStateReportEngine
from app.market.broker import BrokerInterface
from app.prediction.candidates import CandidateGenerationEngine
from app.prediction.lifecycle import OpportunityLifecycleManager
from app.scheduler.service import start_scheduler

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    wire_default_services()

    # Resolved eagerly (not lazily on first lifecycle API call) so a
    # deployment that adds --workers N without updating
    # Settings.deployment_workers fails loudly at boot, not silently in
    # production the first time two workers race a transition.
    container.resolve(OpportunityLifecycleManager)

    broker = container.resolve(BrokerInterface)
    try:
        await broker.connect()
    except Exception as exc:
        logger.error("broker connect failed at startup", extra={"error": str(exc)})

    registry = container.resolve(CollectorRegistry)
    discovered = registry.discover()

    scheduler = start_scheduler()
    # Resolvable so an operator can pause/resume every background job
    # (collectors, feature engines, sweeps) without stopping the process --
    # see /health/scheduler/{pause,resume,status} -- useful for isolating
    # request-path latency from background contention during an incident
    # or a live perf comparison.
    container.register(AsyncIOScheduler, lambda: scheduler)
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
        container.resolve(RiskFeatureEngine),
        container.resolve(IntradayRiskFeatureEngine),
        container.resolve(InstitutionalFlowFeatureEngine),
        container.resolve(MacroFeatureEngine),
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

    async def feature_selection_sweep() -> None:
        """Recommended feature set per watchlist symbol (Prompt 3.16,
        DEBT-9) -- previously reachable only via POST /features/selection,
        never scheduled, so feature_usage had zero live rows despite the
        engine's own code being correct.

        Skips entirely during market hours: live-measured 2026-07-16, a
        full 25-symbol sweep takes ~3min of real CPU (even after fixing
        select_features()'s O(features^2) redundancy scan) and degraded
        /prediction/candidates from its ~4.7s steady state to 36s/10.7s/
        14.6s while running. feature_selection_interval's 21600s divides
        24h exactly, so an unguarded interval trigger would hit the same
        time-of-day, inside market hours, every single day -- and there's
        no upside to running mid-session anyway: this operates on
        timeframe="D", which only updates once/day at midnight (DEBT-1).
        """
        from app.collectors.base import is_nse_market_open
        from app.database.session import get_session_factory
        from app.features.selection import FeatureSelectionEngine

        if is_nse_market_open():
            return
        engine = FeatureSelectionEngine(get_session_factory())
        for symbol in settings.watchlist:
            try:
                await engine.select(symbol)
            except Exception as exc:
                logger.error(
                    "feature selection sweep failed",
                    extra={"symbol": symbol, "error": str(exc)},
                )

    scheduler.add_job(
        feature_selection_sweep,
        trigger="interval",
        seconds=settings.feature_selection_interval,
        id="features.selection_sweep",
        replace_existing=True,
        next_run_time=datetime.now(UTC),
    )

    async def ensemble_training_sweep() -> None:
        """Train (or reuse an already-cached) ensemble and persist a fresh
        prediction per watchlist symbol (Prompt 5.6, DEBT-13) -- previously
        reachable only via POST /prediction/ensemble/{symbol}/train, never
        scheduled, so ensemble_prediction.result had zero live rows despite
        the engine's own code being correct (first live train() call found
        and fixed a real bug: the feature-coverage gate required 60% across
        30 features, 14 of which only started existing ~1-2 days ago, so no
        historical label could ever qualify -- see DEBT-13/DEBT.md).

        Uses the container-resolved singleton, not a fresh instance, so a
        trained ensemble stays cached in memory (EnsemblePredictionEngine's
        own in-memory-only design, no blob store in this codebase) for
        every subsequent predict() call this process serves, whether from
        this sweep, the API, or downstream Volume 5 stages.

        Same after-hours gate and reasoning as feature_selection_sweep:
        training fits against timeframe="D" labels/features that only
        change once/day, live-measured ~3.5s/symbol -- no upside to running
        or re-running mid-session, and the CPU cost is real at 25-symbol
        scale (matches this box's already-documented capacity ceiling,
        DEBT-7/DEBT-8/DEBT-9)."""
        from app.collectors.base import is_nse_market_open
        from app.prediction.ensemble import EnsemblePredictionEngine

        if is_nse_market_open():
            return
        engine = container.resolve(EnsemblePredictionEngine)
        for symbol in settings.watchlist:
            try:
                await engine.predict(symbol)
            except Exception as exc:
                logger.error(
                    "ensemble training sweep failed",
                    extra={"symbol": symbol, "error": str(exc)},
                )

    scheduler.add_job(
        ensemble_training_sweep,
        trigger="interval",
        seconds=settings.ensemble_training_interval,
        id="prediction.ensemble_training_sweep",
        replace_existing=True,
        next_run_time=datetime.now(UTC),
    )

    # CandidateGenerationEngine.generate() reads each watchlist symbol's
    # Market State Report via report_as_of() -- a persisted-read, not a live
    # compute -- so without a scheduled writer that report (and therefore
    # market_confidence) silently stays None forever in an unattended
    # deployment where nobody polls GET /intelligence/state/{symbol}.
    report_engine = container.resolve(MarketStateReportEngine)

    # perf-audit-2026-07-14 finding 5: market_intelligence_sweep,
    # composite_intelligence_sweep, and candidate_generation all fired on
    # the same 300s tick -- APScheduler's IntervalTrigger with no
    # next_run_time counts from job-add time, and all three jobs are added
    # within milliseconds of each other here at startup, so every tick
    # forever piles ~300 background engine executions onto the same event
    # loop at once, with nothing preventing that from also colliding with a
    # concurrent user request. APScheduler's max_instances=1 (the default)
    # only stops a job overlapping *itself*, not job-vs-job. A lock shared
    # by these three background sweeps (never held by the request path)
    # serializes them against each other; each job's own next_run_time
    # below staggers their first fire so steady-state ticks land apart
    # instead of on top of each other.
    background_sweep_lock = asyncio.Lock()

    async def market_intelligence_sweep() -> None:
        """Regenerate and persist a Market State Report for every watchlist
        symbol, keeping candidate generation's report_as_of() reads fresh."""
        async with background_sweep_lock:
            for symbol in settings.watchlist:
                try:
                    await report_engine.generate(symbol)
                except Exception as exc:
                    logger.error(
                        "market intelligence sweep failed",
                        extra={"symbol": symbol, "error": str(exc)},
                    )

    scheduler.add_job(
        market_intelligence_sweep,
        trigger="interval",
        seconds=settings.market_intelligence_interval,
        id="intelligence.market_state_sweep",
        replace_existing=True,
        next_run_time=datetime.now(UTC),
    )

    # CompositeMarketIntelligenceEngine had no scheduled cycle at all --
    # reachable only on-demand via GET /intelligence/composite/{symbol}.
    # Scheduling it here also feeds the Bayesian regime detector (Ch15) for
    # every one of its 11 components and records each one's explainability
    # (Ch16) -- see CompositeMarketIntelligenceEngine.assess() -- neither of
    # which happens as a side effect of market_intelligence_sweep above
    # (that calls MarketStateReportEngine.generate(), which reuses the pure
    # assess_composite() function directly rather than this engine).
    composite_engine = container.resolve(CompositeMarketIntelligenceEngine)

    async def composite_intelligence_sweep() -> None:
        """Regenerate the Composite Market Intelligence Score for every
        watchlist symbol, feeding regime beliefs and explainability records
        for all 11 components along the way."""
        async with background_sweep_lock:
            for symbol in settings.watchlist:
                try:
                    await composite_engine.assess(symbol)
                except Exception as exc:
                    logger.error(
                        "composite intelligence sweep failed",
                        extra={"symbol": symbol, "error": str(exc)},
                    )

    scheduler.add_job(
        composite_intelligence_sweep,
        trigger="interval",
        seconds=settings.market_intelligence_interval,
        id="intelligence.composite_sweep",
        replace_existing=True,
        next_run_time=datetime.now(UTC)
        + timedelta(seconds=settings.market_intelligence_interval / 3),
    )

    # CandidateGenerationEngine.generate() already calls scan() as its first
    # step (persisting opportunity.detected + trade_candidate.generated
    # together), so only this one job is scheduled -- a separate
    # OpportunityDetectionEngine.scan() job would duplicate the same scan.
    candidate_engine = container.resolve(CandidateGenerationEngine)

    async def candidate_generation_sweep() -> None:
        async with background_sweep_lock:
            await candidate_engine.generate()

    scheduler.add_job(
        candidate_generation_sweep,
        trigger="interval",
        seconds=settings.feature_engine_interval,
        id="prediction.candidate_generation",
        replace_existing=True,
        next_run_time=datetime.now(UTC)
        + timedelta(seconds=2 * settings.feature_engine_interval / 3),
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
    app.include_router(intelligence_router)
    app.include_router(prediction_router)
    app.include_router(dashboard_router)
    return app


app = create_app()
