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
    from app.core.cache import CacheService
    from app.core.config import get_settings
    from app.database.session import get_session_factory
    from app.events.bus import EventBus
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
    from app.market.angel_one import AngelOneAdapter
    from app.market.broker import BrokerInterface

    settings = get_settings()
    container.register(EventBus, EventBus)
    container.register(BrokerInterface, lambda: AngelOneAdapter(settings))
    container.register(CacheService, CacheService)
    container.register(
        CollectorRegistry,
        lambda: CollectorRegistry(
            DefaultCollectorPipeline(
                bus=container.resolve(EventBus),
                session_factory=get_session_factory(),
            )
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
