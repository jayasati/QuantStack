"""Process CPU/memory monitoring (Volume 1, Chapter 12: Monitoring exposes
Latency, Memory, CPU, Errors, Queue Length, API Failures, Retries, Health
Score).

QuantStack runs as a single asyncio process, so "every module exposes
memory/CPU" is implemented as one process-wide sampler shared by every
caller, rather than faking a per-function CPU reading that Python has no
reliable way to measure anyway. Latency/errors/retries stay per-collector
(``CollectorHealthStatus``); this covers the two fields nothing tracked.
"""

import os

import psutil

from app.core.logging import get_logger

logger = get_logger(__name__)


class SystemMetricsSampler:
    def __init__(self) -> None:
        self._process = psutil.Process(os.getpid())
        self._process.cpu_percent()  # first call always returns 0.0; primes the sampler

    def snapshot(self) -> dict:
        try:
            with self._process.oneshot():
                cpu_percent = self._process.cpu_percent()
                mem_info = self._process.memory_info()
                mem_percent = self._process.memory_percent()
                num_threads = self._process.num_threads()
            system_cpu_percent = psutil.cpu_percent()
            system_memory = psutil.virtual_memory()
            return {
                "process": {
                    "cpu_percent": cpu_percent,
                    "memory_rss_mb": round(mem_info.rss / (1024 * 1024), 2),
                    "memory_percent": round(mem_percent, 2),
                    "num_threads": num_threads,
                },
                "system": {
                    "cpu_percent": system_cpu_percent,
                    "memory_percent": system_memory.percent,
                    "memory_available_mb": round(system_memory.available / (1024 * 1024), 2),
                },
            }
        except Exception as exc:  # pragma: no cover - depends on OS/permissions
            logger.error("system metrics sample failed", extra={"error": str(exc)})
            return {"process": None, "system": None, "error": str(exc)}
