"""Dependency injection container.

Services never instantiate concrete classes directly. They ask the container
for an interface, so implementations (e.g. the broker adapter) can be swapped
without touching business logic.
"""

from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


class Container:
    """Minimal service registry: register factories, resolve singletons."""

    def __init__(self) -> None:
        self._factories: dict[type, Callable[[], Any]] = {}
        self._instances: dict[type, Any] = {}

    def register(self, interface: type[T], factory: Callable[[], T]) -> None:
        self._factories[interface] = factory
        self._instances.pop(interface, None)

    def resolve(self, interface: type[T]) -> T:
        if interface not in self._instances:
            if interface not in self._factories:
                raise KeyError(f"No factory registered for {interface.__name__}")
            self._instances[interface] = self._factories[interface]()
        return self._instances[interface]

    def reset(self) -> None:
        self._instances.clear()


container = Container()


def wire_default_services() -> None:
    """Register production implementations. Called once at application startup."""
    from app.collectors.pipeline import DefaultCollectorPipeline
    from app.collectors.registry import CollectorRegistry
    from app.core.alerts import AlertService, EventBusAlertSink, LoggingAlertSink
    from app.core.cache import CacheService
    from app.core.circuit_breaker import CircuitBreakerRegistry
    from app.core.config import get_settings
    from app.core.system_metrics import SystemMetricsSampler
    from app.database.session import get_session_factory
    from app.events.bus import EventBus
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
    from app.intelligence.analogs import HistoricalAnalogEngine
    from app.intelligence.breadth import BreadthIntelligenceEngine
    from app.intelligence.confidence import MarketConfidenceEngine
    from app.intelligence.institutional_flow import InstitutionalFlowIntelligenceEngine
    from app.intelligence.regime import BayesianRegimeDetector
    from app.intelligence.report import MarketStateReportEngine
    from app.intelligence.sector import SectorIntelligenceEngine
    from app.intelligence.trend import TrendIntelligenceEngine
    from app.market.angel_one import AngelOneAdapter
    from app.market.broker import BrokerInterface
    from app.prediction.candidates import CandidateGenerationEngine
    from app.prediction.opportunity import OpportunityDetectionEngine
    from app.prediction.snapshot import FeatureSnapshotEngine

    settings = get_settings()
    container.register(EventBus, EventBus)
    container.register(CacheService, CacheService)
    container.register(SystemMetricsSampler, SystemMetricsSampler)
    container.register(
        CircuitBreakerRegistry,
        lambda: CircuitBreakerRegistry(
            failure_threshold=settings.circuit_breaker_failure_threshold,
            recovery_timeout=settings.circuit_breaker_recovery_seconds,
        ),
    )
    container.register(
        AlertService,
        lambda: AlertService(
            sinks=[LoggingAlertSink(), EventBusAlertSink(container.resolve(EventBus))]
        ),
    )
    container.register(
        BrokerInterface,
        lambda: AngelOneAdapter(
            settings,
            circuit_breaker=container.resolve(CircuitBreakerRegistry).get("broker.angel_one"),
            alerts=container.resolve(AlertService),
        ),
    )
    container.register(
        CollectorRegistry,
        lambda: CollectorRegistry(
            DefaultCollectorPipeline(
                bus=container.resolve(EventBus),
                session_factory=get_session_factory(),
            ),
            alerts=container.resolve(AlertService),
        ),
    )
    container.register(
        PriceFeatureEngine,
        lambda: PriceFeatureEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            cache=container.resolve(CacheService),
        ),
    )
    container.register(
        VolumeFeatureEngine,
        lambda: VolumeFeatureEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            cache=container.resolve(CacheService),
        ),
    )
    container.register(
        VolatilityFeatureEngine,
        lambda: VolatilityFeatureEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            cache=container.resolve(CacheService),
        ),
    )
    container.register(
        LiquidityFeatureEngine,
        lambda: LiquidityFeatureEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            cache=container.resolve(CacheService),
        ),
    )
    container.register(
        OptionsFeatureEngine,
        lambda: OptionsFeatureEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            cache=container.resolve(CacheService),
        ),
    )
    container.register(
        BreadthFeatureEngine,
        lambda: BreadthFeatureEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            cache=container.resolve(CacheService),
        ),
    )
    container.register(
        SectorFeatureEngine,
        lambda: SectorFeatureEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            cache=container.resolve(CacheService),
        ),
    )
    container.register(
        RelativeStrengthEngine,
        lambda: RelativeStrengthEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            cache=container.resolve(CacheService),
        ),
    )
    container.register(
        MarketStructureEngine,
        lambda: MarketStructureEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            cache=container.resolve(CacheService),
        ),
    )
    container.register(
        NewsFeatureEngine,
        lambda: NewsFeatureEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            cache=container.resolve(CacheService),
        ),
    )
    container.register(
        EventRiskEngine,
        lambda: EventRiskEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            cache=container.resolve(CacheService),
        ),
    )
    container.register(
        TimeFeatureEngine,
        lambda: TimeFeatureEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            cache=container.resolve(CacheService),
        ),
    )
    container.register(
        InstitutionalFlowFeatureEngine,
        lambda: InstitutionalFlowFeatureEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            cache=container.resolve(CacheService),
        ),
    )
    container.register(
        MacroFeatureEngine,
        lambda: MacroFeatureEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            cache=container.resolve(CacheService),
        ),
    )
    container.register(
        RiskFeatureEngine,
        lambda: RiskFeatureEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            cache=container.resolve(CacheService),
        ),
    )
    container.register(
        IntradayRiskFeatureEngine,
        lambda: IntradayRiskFeatureEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            cache=container.resolve(CacheService),
        ),
    )
    container.register(
        TrendIntelligenceEngine,
        lambda: TrendIntelligenceEngine(session_factory=get_session_factory()),
    )
    container.register(
        BreadthIntelligenceEngine,
        lambda: BreadthIntelligenceEngine(session_factory=get_session_factory()),
    )
    container.register(
        SectorIntelligenceEngine,
        lambda: SectorIntelligenceEngine(session_factory=get_session_factory()),
    )
    container.register(
        InstitutionalFlowIntelligenceEngine,
        lambda: InstitutionalFlowIntelligenceEngine(session_factory=get_session_factory()),
    )
    container.register(
        HistoricalAnalogEngine,
        lambda: HistoricalAnalogEngine(session_factory=get_session_factory()),
    )
    container.register(
        MarketConfidenceEngine,
        lambda: MarketConfidenceEngine(session_factory=get_session_factory()),
    )
    container.register(
        MarketStateReportEngine,
        lambda: MarketStateReportEngine(session_factory=get_session_factory()),
    )
    container.register(
        BayesianRegimeDetector,
        lambda: BayesianRegimeDetector(session_factory=get_session_factory()),
    )
    container.register(
        OpportunityDetectionEngine,
        lambda: OpportunityDetectionEngine(session_factory=get_session_factory()),
    )
    container.register(
        FeatureSnapshotEngine,
        lambda: FeatureSnapshotEngine(
            session_factory=get_session_factory(), cache=container.resolve(CacheService)
        ),
    )
    container.register(
        CandidateGenerationEngine,
        lambda: CandidateGenerationEngine(
            session_factory=get_session_factory(),
            detector=container.resolve(OpportunityDetectionEngine),
            snapshot_engine=container.resolve(FeatureSnapshotEngine),
        ),
    )
