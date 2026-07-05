"""Collector Registry (Volume 2, Prompt 2.12).

Collectors are never hardcoded. The registry discovers BaseCollector
subclasses in ``app.collectors.domains``, registers their metadata, wires
their schedules into APScheduler, supports enable/disable and priority
ordering, and exposes runtime status for the health API.
"""

import importlib
import inspect
import pkgutil
from dataclasses import asdict

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.collectors.base import BaseCollector, CollectorPipeline
from app.core.logging import get_logger

logger = get_logger(__name__)

DISCOVERY_PACKAGES = ["app.collectors.domains", "app.collectors.market_data"]


class CollectorRegistry:
    def __init__(self, pipeline: CollectorPipeline) -> None:
        self._pipeline = pipeline
        self._collectors: dict[str, BaseCollector] = {}
        self._disabled: set[str] = set()

    # --- discovery & registration -------------------------------------------------

    def discover(self, packages: list[str] | None = None) -> int:
        """Import collector modules and register every concrete BaseCollector."""
        found = 0
        for package_name in packages or DISCOVERY_PACKAGES:
            try:
                module = importlib.import_module(package_name)
            except ModuleNotFoundError:
                continue
            modules = [module]
            if hasattr(module, "__path__"):  # package: walk submodules
                for info in pkgutil.iter_modules(module.__path__):
                    modules.append(
                        importlib.import_module(f"{package_name}.{info.name}")
                    )
            for mod in modules:
                for _, cls in inspect.getmembers(mod, inspect.isclass):
                    if (
                        issubclass(cls, BaseCollector)
                        and cls is not BaseCollector
                        and not inspect.isabstract(cls)
                        and cls.__module__ == mod.__name__
                    ):
                        try:
                            self.register(cls())
                            found += 1
                        except Exception as exc:
                            logger.error(
                                "failed to instantiate collector",
                                extra={"collector": cls.__name__, "error": str(exc)},
                            )
        return found

    def register(self, collector: BaseCollector) -> None:
        if collector.name in self._collectors:
            logger.warning("collector already registered", extra={"collector": collector.name})
            return
        self._collectors[collector.name] = collector
        logger.info(
            "collector registered",
            extra={
                "collector": collector.name,
                "category": collector.category.value,
                "interval_seconds": collector.interval_seconds,
                "priority": collector.priority,
            },
        )

    # --- lifecycle -----------------------------------------------------------------

    def enable(self, name: str) -> None:
        self._disabled.discard(name)
        if name in self._collectors:
            self._collectors[name].health.enabled = True
            self._collectors[name].health.status = "idle"

    def disable(self, name: str) -> None:
        self._disabled.add(name)
        if name in self._collectors:
            self._collectors[name].health.enabled = False
            self._collectors[name].health.status = "disabled"

    async def run_collector(self, name: str) -> None:
        collector = self._collectors.get(name)
        if collector is None or name in self._disabled:
            return
        await collector.run_once(self._pipeline)

    def schedule_all(self, scheduler: AsyncIOScheduler) -> int:
        """Wire every registered collector onto its own interval schedule."""
        scheduled = 0
        for collector in sorted(self._collectors.values(), key=lambda c: c.priority):
            scheduler.add_job(
                self.run_collector,
                trigger="interval",
                seconds=collector.interval_seconds,
                args=[collector.name],
                id=f"collector.{collector.name}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            scheduled += 1
        return scheduled

    async def shutdown(self) -> None:
        for collector in self._collectors.values():
            try:
                await collector.cleanup()
            except Exception as exc:
                logger.error(
                    "collector cleanup failed",
                    extra={"collector": collector.name, "error": str(exc)},
                )

    # --- observability ---------------------------------------------------------------

    def list_collectors(self) -> list[dict]:
        return [
            {
                "name": c.name,
                "category": c.category.value,
                "source": c.source,
                "interval_seconds": c.interval_seconds,
                "priority": c.priority,
                "enabled": c.name not in self._disabled,
                "status": c.health.status,
            }
            for c in sorted(self._collectors.values(), key=lambda c: c.priority)
        ]

    def health_of(self, name: str) -> dict | None:
        collector = self._collectors.get(name)
        if collector is None:
            return None
        health = asdict(collector.health)
        health["failure_rate"] = collector.health.failure_rate
        return health

    def get(self, name: str) -> BaseCollector | None:
        return self._collectors.get(name)
