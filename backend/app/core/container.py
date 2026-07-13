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
    from app.core.alerts import AlertService, AlertSink, EventBusAlertSink, LoggingAlertSink
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
    from app.intelligence.composite import CompositeMarketIntelligenceEngine
    from app.intelligence.confidence import MarketConfidenceEngine
    from app.intelligence.correlation import CorrelationIntelligenceEngine
    from app.intelligence.events import EventIntelligenceEngine
    from app.intelligence.institutional_flow import InstitutionalFlowIntelligenceEngine
    from app.intelligence.liquidity import LiquidityIntelligenceEngine
    from app.intelligence.macro import MacroIntelligenceEngine
    from app.intelligence.regime import BayesianRegimeDetector
    from app.intelligence.relative import RelativeStrengthIntelligenceEngine
    from app.intelligence.report import MarketStateReportEngine
    from app.intelligence.sector import SectorIntelligenceEngine
    from app.intelligence.structure import MarketStructureIntelligenceEngine
    from app.intelligence.transitions import RegimeTransitionEngine
    from app.intelligence.trend import TrendIntelligenceEngine
    from app.intelligence.volatility import VolatilityIntelligenceEngine
    from app.market.angel_one import AngelOneAdapter
    from app.market.broker import BrokerInterface
    from app.prediction.agreement import ModelAgreementEngine
    from app.prediction.alpha_research import AlphaResearchEngine
    from app.prediction.calibration import ProbabilityCalibrationEngine
    from app.prediction.candidates import CandidateGenerationEngine
    from app.prediction.conviction import ConvictionEngine
    from app.prediction.duplicate import DuplicateSignalEngine
    from app.prediction.ensemble import EnsemblePredictionEngine
    from app.prediction.explainability import ExplainabilityReportEngine
    from app.prediction.historical_similarity import HistoricalSimilarityEngine
    from app.prediction.labeling import TripleBarrierLabelingEngine
    from app.prediction.lifecycle import OpportunityLifecycleManager
    from app.prediction.market_context import MarketContextAdjustmentEngine
    from app.prediction.multi_horizon import MultiHorizonPredictionEngine
    from app.prediction.opportunity import OpportunityDetectionEngine
    from app.prediction.priority import SignalPriorityEngine
    from app.prediction.qualification import TradeQualificationEngine
    from app.prediction.snapshot import FeatureSnapshotEngine
    from app.telegram.sink import TelegramAlertSink

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

    def _build_alert_sinks() -> list[AlertSink]:
        sinks: list[AlertSink] = [LoggingAlertSink(), EventBusAlertSink(container.resolve(EventBus))]
        if settings.telegram_token and settings.telegram_chat_id:
            sinks.append(TelegramAlertSink(settings.telegram_token, settings.telegram_chat_id))
        return sinks

    container.register(AlertService, lambda: AlertService(sinks=_build_alert_sinks()))
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
        lambda: TrendIntelligenceEngine(
            session_factory=get_session_factory(), bus=container.resolve(EventBus),
        ),
    )
    container.register(
        BreadthIntelligenceEngine,
        lambda: BreadthIntelligenceEngine(
            session_factory=get_session_factory(), bus=container.resolve(EventBus),
        ),
    )
    container.register(
        SectorIntelligenceEngine,
        lambda: SectorIntelligenceEngine(
            session_factory=get_session_factory(), bus=container.resolve(EventBus),
        ),
    )
    container.register(
        MarketStructureIntelligenceEngine,
        lambda: MarketStructureIntelligenceEngine(
            session_factory=get_session_factory(), bus=container.resolve(EventBus),
        ),
    )
    container.register(
        RelativeStrengthIntelligenceEngine,
        lambda: RelativeStrengthIntelligenceEngine(
            session_factory=get_session_factory(), bus=container.resolve(EventBus),
        ),
    )
    container.register(
        InstitutionalFlowIntelligenceEngine,
        lambda: InstitutionalFlowIntelligenceEngine(
            session_factory=get_session_factory(), bus=container.resolve(EventBus),
        ),
    )
    container.register(
        LiquidityIntelligenceEngine,
        lambda: LiquidityIntelligenceEngine(
            session_factory=get_session_factory(), bus=container.resolve(EventBus),
        ),
    )
    container.register(
        EventIntelligenceEngine,
        lambda: EventIntelligenceEngine(
            session_factory=get_session_factory(), bus=container.resolve(EventBus),
        ),
    )
    container.register(
        VolatilityIntelligenceEngine,
        lambda: VolatilityIntelligenceEngine(
            session_factory=get_session_factory(), bus=container.resolve(EventBus),
        ),
    )
    container.register(
        MacroIntelligenceEngine,
        lambda: MacroIntelligenceEngine(
            session_factory=get_session_factory(), bus=container.resolve(EventBus),
        ),
    )
    container.register(
        CorrelationIntelligenceEngine,
        lambda: CorrelationIntelligenceEngine(
            session_factory=get_session_factory(), bus=container.resolve(EventBus),
        ),
    )
    container.register(
        RegimeTransitionEngine,
        lambda: RegimeTransitionEngine(
            session_factory=get_session_factory(), bus=container.resolve(EventBus),
        ),
    )
    container.register(
        HistoricalAnalogEngine,
        lambda: HistoricalAnalogEngine(
            session_factory=get_session_factory(), bus=container.resolve(EventBus),
        ),
    )
    container.register(
        MarketConfidenceEngine,
        lambda: MarketConfidenceEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            regime_transition_engine=container.resolve(RegimeTransitionEngine),
            breadth_engine=container.resolve(BreadthIntelligenceEngine),
            institutional_flow_engine=container.resolve(InstitutionalFlowIntelligenceEngine),
            correlation_engine=container.resolve(CorrelationIntelligenceEngine),
        ),
    )
    container.register(
        MarketStateReportEngine,
        lambda: MarketStateReportEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            trend_engine=container.resolve(TrendIntelligenceEngine),
            volatility_engine=container.resolve(VolatilityIntelligenceEngine),
            breadth_engine=container.resolve(BreadthIntelligenceEngine),
            liquidity_engine=container.resolve(LiquidityIntelligenceEngine),
            macro_engine=container.resolve(MacroIntelligenceEngine),
            sector_engine=container.resolve(SectorIntelligenceEngine),
            institutional_flow_engine=container.resolve(InstitutionalFlowIntelligenceEngine),
            correlation_engine=container.resolve(CorrelationIntelligenceEngine),
            market_structure_engine=container.resolve(MarketStructureIntelligenceEngine),
            event_engine=container.resolve(EventIntelligenceEngine),
            confidence_engine=container.resolve(MarketConfidenceEngine),
            analog_engine=container.resolve(HistoricalAnalogEngine),
        ),
    )
    container.register(
        CompositeMarketIntelligenceEngine,
        lambda: CompositeMarketIntelligenceEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            trend_engine=container.resolve(TrendIntelligenceEngine),
            volatility_engine=container.resolve(VolatilityIntelligenceEngine),
            breadth_engine=container.resolve(BreadthIntelligenceEngine),
            liquidity_engine=container.resolve(LiquidityIntelligenceEngine),
            macro_engine=container.resolve(MacroIntelligenceEngine),
            sector_engine=container.resolve(SectorIntelligenceEngine),
            institutional_flow_engine=container.resolve(InstitutionalFlowIntelligenceEngine),
            correlation_engine=container.resolve(CorrelationIntelligenceEngine),
            market_structure_engine=container.resolve(MarketStructureIntelligenceEngine),
            event_engine=container.resolve(EventIntelligenceEngine),
        ),
    )
    container.register(
        BayesianRegimeDetector,
        lambda: BayesianRegimeDetector(
            session_factory=get_session_factory(), bus=container.resolve(EventBus),
        ),
    )
    container.register(
        OpportunityDetectionEngine,
        lambda: OpportunityDetectionEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            trend_engine=container.resolve(TrendIntelligenceEngine),
            market_structure_engine=container.resolve(MarketStructureIntelligenceEngine),
            institutional_flow_engine=container.resolve(InstitutionalFlowIntelligenceEngine),
            relative_strength_engine=container.resolve(RelativeStrengthIntelligenceEngine),
            volatility_engine=container.resolve(VolatilityIntelligenceEngine),
            event_engine=container.resolve(EventIntelligenceEngine),
            regime_detector=container.resolve(BayesianRegimeDetector),
            regime_transition_engine=container.resolve(RegimeTransitionEngine),
            report_engine=container.resolve(MarketStateReportEngine),
        ),
    )
    container.register(
        FeatureSnapshotEngine,
        lambda: FeatureSnapshotEngine(
            session_factory=get_session_factory(),
            cache=container.resolve(CacheService),
            bus=container.resolve(EventBus),
        ),
    )
    container.register(
        CandidateGenerationEngine,
        lambda: CandidateGenerationEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            detector=container.resolve(OpportunityDetectionEngine),
            snapshot_engine=container.resolve(FeatureSnapshotEngine),
        ),
    )
    container.register(
        MultiHorizonPredictionEngine,
        lambda: MultiHorizonPredictionEngine(
            session_factory=get_session_factory(),
            bus=container.resolve(EventBus),
            snapshot_engine=container.resolve(FeatureSnapshotEngine),
        ),
    )
    container.register(
        TripleBarrierLabelingEngine,
        lambda: TripleBarrierLabelingEngine(
            session_factory=get_session_factory(),
            cache=container.resolve(CacheService),
            bus=container.resolve(EventBus),
        ),
    )
    container.register(
        EnsemblePredictionEngine,
        lambda: EnsemblePredictionEngine(
            session_factory=get_session_factory(),
            cache=container.resolve(CacheService),
            bus=container.resolve(EventBus),
            labeling_engine=container.resolve(TripleBarrierLabelingEngine),
            snapshot_engine=container.resolve(FeatureSnapshotEngine),
        ),
    )
    container.register(
        ProbabilityCalibrationEngine,
        lambda: ProbabilityCalibrationEngine(
            session_factory=get_session_factory(),
            cache=container.resolve(CacheService),
            bus=container.resolve(EventBus),
            ensemble_engine=container.resolve(EnsemblePredictionEngine),
        ),
    )
    container.register(
        ModelAgreementEngine,
        lambda: ModelAgreementEngine(
            session_factory=get_session_factory(),
            cache=container.resolve(CacheService),
            bus=container.resolve(EventBus),
            ensemble_engine=container.resolve(EnsemblePredictionEngine),
        ),
    )
    container.register(
        HistoricalSimilarityEngine,
        lambda: HistoricalSimilarityEngine(
            session_factory=get_session_factory(),
            cache=container.resolve(CacheService),
            bus=container.resolve(EventBus),
            analog_engine=container.resolve(HistoricalAnalogEngine),
            candidate_engine=container.resolve(CandidateGenerationEngine),
        ),
    )
    container.register(
        MarketContextAdjustmentEngine,
        lambda: MarketContextAdjustmentEngine(
            session_factory=get_session_factory(),
            cache=container.resolve(CacheService),
            bus=container.resolve(EventBus),
            calibration_engine=container.resolve(ProbabilityCalibrationEngine),
            market_confidence_engine=container.resolve(MarketConfidenceEngine),
            liquidity_engine=container.resolve(LiquidityIntelligenceEngine),
            event_engine=container.resolve(EventIntelligenceEngine),
            regime_transition_engine=container.resolve(RegimeTransitionEngine),
            institutional_flow_engine=container.resolve(InstitutionalFlowIntelligenceEngine),
            volatility_engine=container.resolve(VolatilityIntelligenceEngine),
        ),
    )
    container.register(
        ConvictionEngine,
        lambda: ConvictionEngine(
            session_factory=get_session_factory(),
            cache=container.resolve(CacheService),
            bus=container.resolve(EventBus),
            calibration_engine=container.resolve(ProbabilityCalibrationEngine),
            market_context_engine=container.resolve(MarketContextAdjustmentEngine),
            historical_similarity_engine=container.resolve(HistoricalSimilarityEngine),
            institutional_flow_engine=container.resolve(InstitutionalFlowIntelligenceEngine),
            market_structure_engine=container.resolve(MarketStructureIntelligenceEngine),
            liquidity_engine=container.resolve(LiquidityIntelligenceEngine),
            relative_strength_engine=container.resolve(RelativeStrengthIntelligenceEngine),
            agreement_engine=container.resolve(ModelAgreementEngine),
            candidate_engine=container.resolve(CandidateGenerationEngine),
        ),
    )
    container.register(
        TradeQualificationEngine,
        lambda: TradeQualificationEngine(
            session_factory=get_session_factory(),
            cache=container.resolve(CacheService),
            bus=container.resolve(EventBus),
            liquidity_engine=container.resolve(LiquidityIntelligenceEngine),
            event_engine=container.resolve(EventIntelligenceEngine),
            agreement_engine=container.resolve(ModelAgreementEngine),
            market_confidence_engine=container.resolve(MarketConfidenceEngine),
            historical_similarity_engine=container.resolve(HistoricalSimilarityEngine),
            candidate_engine=container.resolve(CandidateGenerationEngine),
        ),
    )
    container.register(
        SignalPriorityEngine,
        lambda: SignalPriorityEngine(
            session_factory=get_session_factory(),
            cache=container.resolve(CacheService),
            bus=container.resolve(EventBus),
            candidate_engine=container.resolve(CandidateGenerationEngine),
            qualification_engine=container.resolve(TradeQualificationEngine),
            conviction_engine=container.resolve(ConvictionEngine),
            liquidity_engine=container.resolve(LiquidityIntelligenceEngine),
            relative_strength_engine=container.resolve(RelativeStrengthIntelligenceEngine),
            historical_similarity_engine=container.resolve(HistoricalSimilarityEngine),
        ),
    )
    container.register(
        DuplicateSignalEngine,
        lambda: DuplicateSignalEngine(
            session_factory=get_session_factory(),
            cache=container.resolve(CacheService),
            bus=container.resolve(EventBus),
            priority_engine=container.resolve(SignalPriorityEngine),
        ),
    )
    container.register(
        OpportunityLifecycleManager,
        lambda: OpportunityLifecycleManager(
            session_factory=get_session_factory(), bus=container.resolve(EventBus),
        ),
    )
    container.register(
        ExplainabilityReportEngine,
        lambda: ExplainabilityReportEngine(
            session_factory=get_session_factory(),
            cache=container.resolve(CacheService),
            bus=container.resolve(EventBus),
            ensemble_engine=container.resolve(EnsemblePredictionEngine),
            calibration_engine=container.resolve(ProbabilityCalibrationEngine),
            market_context_engine=container.resolve(MarketContextAdjustmentEngine),
            conviction_engine=container.resolve(ConvictionEngine),
            agreement_engine=container.resolve(ModelAgreementEngine),
            historical_similarity_engine=container.resolve(HistoricalSimilarityEngine),
            snapshot_engine=container.resolve(FeatureSnapshotEngine),
            report_engine=container.resolve(MarketStateReportEngine),
            qualification_engine=container.resolve(TradeQualificationEngine),
        ),
    )
    container.register(
        AlphaResearchEngine,
        lambda: AlphaResearchEngine(
            session_factory=get_session_factory(),
            cache=container.resolve(CacheService),
            bus=container.resolve(EventBus),
            labeling_engine=container.resolve(TripleBarrierLabelingEngine),
            ensemble_engine=container.resolve(EnsemblePredictionEngine),
        ),
    )
