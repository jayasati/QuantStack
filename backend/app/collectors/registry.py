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
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.collectors.base import BaseCollector, CollectorPipeline
from app.core.alerts import AlertService
from app.core.logging import get_logger

logger = get_logger(__name__)

DISCOVERY_PACKAGES = ["app.collectors.domains", "app.collectors.market_data"]


class CollectorRegistry:
    def __init__(self, pipeline: CollectorPipeline, alerts: AlertService | None = None) -> None:
        self._pipeline = pipeline
        self._alerts = alerts
        self._collectors: dict[str, BaseCollector] = {}
        self._disabled: set[str] = set()
        self._scheduler: AsyncIOScheduler | None = None

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
        if self._alerts is not None:
            collector.alerts = self._alerts
        self._collectors[collector.name] = collector
        logger.info(
            "collector registered",
            extra={
                "collector": collector.name,
                "category": collector.category.value,
                "interval_seconds": collector.interval_seconds,
                "priority": collector.priority,
                "depends_on": list(collector.depends_on),
            },
        )

    # --- dependency resolution -------------------------------------------------------

    def validate_dependencies(self) -> list[str]:
        """Warn about dependencies on unknown collectors; return the problems."""
        problems: list[str] = []
        for collector in self._collectors.values():
            for dependency in collector.depends_on:
                if dependency not in self._collectors:
                    problems.append(f"{collector.name} depends on unknown '{dependency}'")
        for problem in problems:
            logger.warning("dependency problem", extra={"detail": problem})
        return problems

    def resolution_order(self) -> list[BaseCollector]:
        """Collectors topologically sorted so dependencies come before their
        dependents; priority breaks ties. Raises on dependency cycles."""
        by_name = self._collectors
        ordered: list[BaseCollector] = []
        state: dict[str, int] = {}  # 0=unvisited 1=visiting 2=done

        def visit(name: str, chain: tuple[str, ...]) -> None:
            if state.get(name) == 2:
                return
            if state.get(name) == 1:
                cycle = " -> ".join((*chain, name))
                raise ValueError(f"collector dependency cycle: {cycle}")
            state[name] = 1
            collector = by_name[name]
            for dependency in sorted(
                (d for d in collector.depends_on if d in by_name),
                key=lambda d: by_name[d].priority,
            ):
                visit(dependency, (*chain, name))
            state[name] = 2
            ordered.append(collector)

        for name in sorted(by_name, key=lambda n: by_name[n].priority):
            visit(name, ())
        return ordered

    def effective_interval(self, collector: BaseCollector) -> int:
        """Configured override from settings, else the collector default."""
        from app.core.config import get_settings

        override = get_settings().collector_intervals.get(collector.name)
        return int(override) if override else collector.interval_seconds

    # --- lifecycle -----------------------------------------------------------------

    def enable(self, name: str) -> None:
        self._disabled.discard(name)
        if name in self._collectors:
            self._collectors[name].health.enabled = True
            self._collectors[name].health.status = "idle"

    def disable(self, name: str) -> list[str]:
        """Disable a collector; returns the names of still-enabled dependents."""
        self._disabled.add(name)
        if name in self._collectors:
            self._collectors[name].health.enabled = False
            self._collectors[name].health.status = "disabled"
        dependents = [
            c.name
            for c in self._collectors.values()
            if name in c.depends_on and c.name not in self._disabled
        ]
        if dependents:
            logger.warning(
                "disabled collector has active dependents",
                extra={"collector": name, "dependents": dependents},
            )
        return dependents

    async def run_collector(self, name: str, force: bool = False) -> None:
        collector = self._collectors.get(name)
        if collector is None or name in self._disabled:
            return
        await collector.run_once(self._pipeline, force=force)
        # APScheduler recomputes an interval job's next fire time before
        # invoking it, so this already reflects the upcoming run (Chapter 20).
        collector.health.next_run = self._next_run_time(name)

    def _next_run_time(self, name: str) -> datetime | None:
        if self._scheduler is None:
            return None
        job = self._scheduler.get_job(f"collector.{name}")
        if job is None:
            return None
        # An unstarted scheduler leaves next_run_time uninitialized (APScheduler
        # raises AttributeError on access, not None) — never let observability
        # code crash the caller over scheduler lifecycle timing.
        return getattr(job, "next_run_time", None)

    def schedule_all(self, scheduler: AsyncIOScheduler) -> int:
        """Wire every registered collector onto its own interval schedule.

        Dependencies are validated and scheduling follows the topological
        resolution order (dependencies first); intervals honour per-collector
        overrides from configuration.
        """
        self._scheduler = scheduler
        self.validate_dependencies()
        scheduled = 0
        for collector in self.resolution_order():
            scheduler.add_job(
                self.run_collector,
                trigger="interval",
                seconds=self.effective_interval(collector),
                args=[collector.name],
                id=f"collector.{collector.name}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            collector.health.next_run = self._next_run_time(collector.name)
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
        """Chapter 20's observability field set, one row per collector."""
        return [
            {
                "name": c.name,
                "category": c.category.value,
                "source": c.source,
                "interval_seconds": self.effective_interval(c),
                "default_interval_seconds": c.interval_seconds,
                "priority": c.priority,
                "depends_on": list(c.depends_on),
                "enabled": c.name not in self._disabled,
                "status": c.health.status,
                "last_run": c.health.last_run,
                "next_run": c.health.next_run,
                "avg_latency_ms": round(c.health.avg_latency_ms, 2),
                "failure_rate": round(c.health.failure_rate, 4),
                "retry_count": c.health.retry_count,
                "queue_length": c.health.queue_length,
                "last_quality_score": c.health.last_quality_score,
            }
            for c in self.resolution_order()
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

    def circuit_breakers(self) -> list[dict]:
        return [c.circuit_breaker.to_dict() for c in self._collectors.values()]
