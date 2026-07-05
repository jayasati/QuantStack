"""Broker-backed sector rotation source (real feed for Prompt 2.6).

Window returns for the benchmark and every tracked NSE sectoral index are
computed from broker daily candles, cached for a few hours per index.

NSE index candles report zero traded volume (indices do not trade), so
``volume_ratio`` is fixed at a neutral 1.0 — it contributes no signal rather
than a fabricated one.
"""

import asyncio
import time
from typing import Any

from app.collectors.base import CollectionError
from app.collectors.domains.sector import SectorSource
from app.core.logging import get_logger

logger = get_logger(__name__)

BENCHMARK_TOKEN = ("99926000", "NSE")  # Nifty 50

# Sector name (as tracked by the collector) -> Angel One REST index token.
SECTOR_TOKENS: dict[str, tuple[str, str]] = {
    "Banking": ("99926009", "NSE"),        # Nifty Bank
    "IT": ("99926008", "NSE"),             # Nifty IT
    "Auto": ("99926029", "NSE"),           # Nifty Auto
    "Energy": ("99926020", "NSE"),         # Nifty Energy
    "Pharma": ("99926023", "NSE"),         # Nifty Pharma
    "FMCG": ("99926021", "NSE"),           # Nifty FMCG
    "PSU": ("99926024", "NSE"),            # Nifty PSE
    "PSU Bank": ("99926025", "NSE"),       # Nifty PSU Bank
    "Private Bank": ("99926047", "NSE"),   # Nifty Pvt Bank
    "Realty": ("99926018", "NSE"),         # Nifty Realty
    "Metal": ("99926030", "NSE"),          # Nifty Metal
    "Infrastructure": ("99926019", "NSE"), # Nifty Infra
}

CACHE_TTL_SECONDS = 4 * 3600
HISTORY_DAYS = 60
NEUTRAL_VOLUME_RATIO = 1.0  # index candles carry no volume


def window_returns(closes: list[float]) -> dict[str, float] | None:
    """Percentage returns over 1/5/20 trading days from a daily close series."""
    if len(closes) < 21:
        return None
    last = closes[-1]
    return {
        "return_1d": (last / closes[-2] - 1.0) * 100.0,
        "return_5d": (last / closes[-6] - 1.0) * 100.0,
        "return_20d": (last / closes[-21] - 1.0) * 100.0,
        "volume_ratio": NEUTRAL_VOLUME_RATIO,
    }


class BrokerSectorSource(SectorSource):
    def __init__(self, broker: Any = None) -> None:
        self._broker = broker
        self._cache: tuple[float, dict] | None = None

    def _get_broker(self):
        if self._broker is None:
            from app.core.container import container
            from app.market.broker import BrokerInterface

            self._broker = container.resolve(BrokerInterface)
        return self._broker

    async def fetch_sectors(self) -> dict:
        if self._cache is not None and time.time() - self._cache[0] < CACHE_TTL_SECONDS:
            return self._cache[1]

        benchmark = await self._returns_for(*BENCHMARK_TOKEN)
        if benchmark is None:
            raise CollectionError("could not compute benchmark returns")

        sectors: dict[str, dict[str, float]] = {}
        failed: list[str] = []
        for name, (token, exchange) in SECTOR_TOKENS.items():
            metrics = await self._returns_for(token, exchange)
            if metrics is None:
                failed.append(name)
                continue
            sectors[name] = metrics
        if failed:
            # The collector requires every tracked sector — fail loudly rather
            # than silently narrowing the sector set.
            raise CollectionError(f"sector history unavailable for: {', '.join(failed)}")

        payload = {"benchmark": benchmark, "sectors": sectors}
        self._cache = (time.time(), payload)
        return payload

    async def _returns_for(self, token: str, exchange: str) -> dict[str, float] | None:
        from datetime import UTC, datetime, timedelta

        try:
            candles = await self._get_broker().get_historical(
                token,
                "D",
                datetime.now(UTC) - timedelta(days=HISTORY_DAYS),
                datetime.now(UTC),
                exchange=exchange,
            )
        except Exception as exc:
            logger.warning(
                "sector history fetch failed", extra={"token": token, "error": str(exc)}
            )
            return None
        await asyncio.sleep(0.35)  # stay well inside broker rate limits
        return window_returns([candle.close for candle in candles])
