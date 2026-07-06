"""Broker-backed sector rotation source (real feed for Prompt 2.6).

Window returns for the benchmark and every tracked NSE sectoral index are
computed from broker daily candles, cached for a few hours per index.

Relative volume: broker index candles report zero traded volume, so today's
index volume comes from NSE's per-sector constituents API, and the ratio is
computed against our own stored end-of-day volume history (persisted through
each sector record's raw_value). Until at least MIN_VOLUME_HISTORY_DAYS of
history exist the ratio stays at a neutral 1.0 — never fabricated.
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

# NSE index page names for per-sector volume lookups.
NSE_INDEX_NAMES: dict[str, str] = {
    "BENCHMARK": "NIFTY 50",
    "Banking": "NIFTY BANK",
    "IT": "NIFTY IT",
    "Auto": "NIFTY AUTO",
    "Energy": "NIFTY ENERGY",
    "Pharma": "NIFTY PHARMA",
    "FMCG": "NIFTY FMCG",
    "PSU": "NIFTY PSE",
    "PSU Bank": "NIFTY PSU BANK",
    "Private Bank": "NIFTY PRIVATE BANK",
    "Realty": "NIFTY REALTY",
    "Metal": "NIFTY METAL",
    "Infrastructure": "NIFTY INFRASTRUCTURE",
}

CACHE_TTL_SECONDS = 4 * 3600
VOLUME_CACHE_TTL_SECONDS = 600  # intraday volume refresh
HISTORY_DAYS = 60
NEUTRAL_VOLUME_RATIO = 1.0
MIN_VOLUME_HISTORY_DAYS = 3
VOLUME_RATIO_CLAMP = (0.1, 10.0)


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
    def __init__(self, broker: Any = None, nse_session: Any = None) -> None:
        self._broker = broker
        self._nse = nse_session
        self._cache: tuple[float, dict] | None = None
        self._volume_cache: tuple[float, dict[str, float]] | None = None

    def _get_broker(self):
        if self._broker is None:
            from app.core.container import container
            from app.market.broker import BrokerInterface

            self._broker = container.resolve(BrokerInterface)
        return self._broker

    def _get_nse(self):
        if self._nse is None:
            from app.collectors.sources.nse_client import NseSession

            self._nse = NseSession(warmup_path="/market-data/live-equity-market")
        return self._nse

    async def fetch_sectors(self) -> dict:
        if self._cache is not None and time.time() - self._cache[0] < CACHE_TTL_SECONDS:
            payload = self._cache[1]
        else:
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
                # The collector requires every tracked sector — fail loudly
                # rather than silently narrowing the sector set.
                raise CollectionError(
                    f"sector history unavailable for: {', '.join(failed)}"
                )
            payload = {"benchmark": benchmark, "sectors": sectors}
            self._cache = (time.time(), payload)

        await self._apply_relative_volume(payload)
        return payload

    # --- relative volume (NSE index volume vs our own stored history) -----------

    async def _apply_relative_volume(self, payload: dict) -> None:
        volumes = await self._todays_volumes()
        if not volumes:
            return
        entries = [("BENCHMARK", payload["benchmark"])] + [
            (name, entry) for name, entry in payload["sectors"].items()
        ]
        for name, entry in entries:
            volume = volumes.get(name)
            if volume is None:
                continue
            entry["index_volume"] = volume
            history = await self._volume_history(name)
            if len(history) >= MIN_VOLUME_HISTORY_DAYS:
                average = sum(history) / len(history)
                if average > 0:
                    low, high = VOLUME_RATIO_CLAMP
                    entry["volume_ratio"] = min(max(volume / average, low), high)
                    entry["volume_history_days"] = len(history)

    async def _todays_volumes(self) -> dict[str, float]:
        """Current cumulative traded volume per tracked index, from NSE."""
        now = time.time()
        if (
            self._volume_cache is not None
            and now - self._volume_cache[0] < VOLUME_CACHE_TTL_SECONDS
        ):
            return self._volume_cache[1]
        volumes: dict[str, float] = {}
        for name, index_name in NSE_INDEX_NAMES.items():
            try:
                payload = await self._get_nse().get_json(
                    f"/api/equity-stock-indices?index={index_name.replace(' ', '%20')}"
                )
                for row in payload.get("data") or []:
                    if row.get("priority") == 1 or row.get("symbol") == index_name:
                        value = row.get("totalTradedVolume")
                        if value:
                            volumes[name] = float(value)
                        break
            except Exception as exc:
                logger.warning(
                    "sector volume fetch failed",
                    extra={"sector": name, "error": str(exc)},
                )
        if volumes:
            self._volume_cache = (now, volumes)
        return volumes

    async def _volume_history(self, name: str) -> list[float]:
        """End-of-day index volumes for prior days, from our stored records."""
        try:
            from sqlalchemy import text

            from app.database.session import get_session_factory

            instrument = "SECTORS" if name == "BENCHMARK" else name
            key = "benchmark" if name == "BENCHMARK" else None
            # Sector records persist metrics in raw_value; take the max
            # cumulative volume per prior day as that day's EOD volume.
            sql = (
                "SELECT MAX((data->'raw_value'->>'index_volume')::float) "
                "FROM market_events "
                "WHERE event_type = 'sector.observation' "
                "AND data->>'instrument' = :instrument "
                "AND data->'raw_value'->>'index_volume' IS NOT NULL "
                "AND created_at::date < CURRENT_DATE "
                "GROUP BY created_at::date "
                "ORDER BY created_at::date DESC LIMIT 5"
            )
            if key == "benchmark":
                sql = sql.replace(
                    "data->'raw_value'->>'index_volume'",
                    "data->'metadata'->'benchmark'->>'index_volume'",
                )
            sessions = get_session_factory()
            async with sessions() as session:
                result = await session.execute(text(sql), {"instrument": instrument})
                return [row[0] for row in result if row[0] is not None]
        except Exception:
            return []

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
