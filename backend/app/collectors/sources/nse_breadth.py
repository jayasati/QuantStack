"""NSE index-constituents breadth source (real feed for Prompt 2.5).

Live per-stock quotes come from NSE's equity-stockIndices API (one request
per run): last price, previous close, 52-week high/low, volume, and free-float
market cap. EMAs (20/50/100/200) cannot be read from any live endpoint — they
are computed from broker daily candles and cached for a day per symbol.

Stocks whose EMA history cannot be fetched are excluded from the universe
(coverage is reported in the payload) — values are never fabricated.
"""

import asyncio
import time
from typing import Any

from app.collectors.base import CollectionError
from app.collectors.domains.breadth import BreadthSource
from app.collectors.sources.nse_client import NseSession
from app.core.logging import get_logger

logger = get_logger(__name__)

EMA_WINDOWS = (20, 50, 100, 200)
EMA_CACHE_TTL_SECONDS = 20 * 3600  # refresh roughly daily
HISTORY_DAYS = 400  # enough closes to seed a 200-day EMA


def compute_emas(closes: list[float]) -> dict[str, float] | None:
    """Standard EMAs seeded with the SMA of the first window."""
    if len(closes) < max(EMA_WINDOWS):
        return None
    emas: dict[str, float] = {}
    for window in EMA_WINDOWS:
        k = 2.0 / (window + 1)
        ema = sum(closes[:window]) / window
        for close in closes[window:]:
            ema = close * k + ema * (1 - k)
        emas[f"ema{window}"] = ema
    return emas


class NseBreadthSource(BreadthSource):
    def __init__(
        self,
        index: str = "NIFTY 50",
        session: Any = None,
        broker: Any = None,
        instruments: Any = None,
        cache: Any = None,
    ) -> None:
        self._index = index
        self._session = session or NseSession(warmup_path="/market-data/live-equity-market")
        self._broker = broker
        self._instruments = instruments
        self._cache = cache
        self._ema_cache: dict[str, tuple[float, dict[str, float]]] = {}

    def _get_cache(self):
        """Shared Redis cache (Prompt 2.14) — survives process restarts so a
        rebuild does not refetch ~50 EMA histories from the broker."""
        if self._cache is None:
            from app.core.cache import CacheService
            from app.core.container import container

            try:
                self._cache = container.resolve(CacheService)
            except Exception:
                self._cache = False  # container not wired (tests/scripts)
        return self._cache or None

    # --- lazy platform services (avoid import cycles, allow test injection) ------

    def _get_broker(self):
        if self._broker is None:
            from app.core.container import container
            from app.market.broker import BrokerInterface

            self._broker = container.resolve(BrokerInterface)
        return self._broker

    def _get_instruments(self):
        if self._instruments is None:
            from app.market.instruments import InstrumentService

            self._instruments = InstrumentService()
        return self._instruments

    # --- source interface ----------------------------------------------------------

    async def fetch_universe(self) -> list[dict]:
        quotes = await self._fetch_index_quotes()
        if not quotes:
            raise CollectionError(f"nse returned no constituents for {self._index}")

        rows: list[dict] = []
        skipped: list[str] = []
        for quote in quotes:
            emas = await self._emas_for(quote["symbol"])
            if emas is None:
                skipped.append(quote["symbol"])
                continue
            rows.append({**quote, **emas})
        if skipped:
            logger.warning(
                "breadth universe missing EMA history for some symbols",
                extra={"skipped": len(skipped), "symbols": skipped[:10]},
            )
        if not rows:
            raise CollectionError("no breadth constituents have EMA history")
        return rows

    async def _fetch_index_quotes(self) -> list[dict]:
        payload = await self._session.get_json(
            f"/api/equity-stock-indices?index={self._index.replace(' ', '%20')}"
        )
        rows = payload.get("data") or []
        quotes: list[dict] = []
        for row in rows:
            symbol = row.get("symbol")
            # The first row is the index itself — skip anything without EQ metadata.
            if not symbol or row.get("priority") == 1 or symbol == self._index:
                continue
            last = row.get("lastPrice")
            prev = row.get("previousClose")
            if last is None or prev is None:
                continue
            quotes.append(
                {
                    "symbol": symbol,
                    "last": float(last),
                    "prev_close": float(prev),
                    "high_252": float(row.get("yearHigh") or 0.0),
                    "low_252": float(row.get("yearLow") or 0.0),
                    "volume": float(row.get("totalTradedVolume") or 0.0),
                    "mcap": float(row.get("ffmc") or 0.0),
                }
            )
        return quotes

    async def _emas_for(self, symbol: str) -> dict[str, float] | None:
        cached = self._ema_cache.get(symbol)
        now = time.time()
        if cached is not None and now - cached[0] < EMA_CACHE_TTL_SECONDS:
            return cached[1]
        redis_cache = self._get_cache()
        if redis_cache is not None:
            stored = await redis_cache.get_safe(f"breadth:emas:{symbol}")
            if stored is not None:
                restored = {str(k): float(v) for k, v in stored.items()}
                self._ema_cache[symbol] = (now, restored)
                return restored
        try:
            token, exchange, _ = self._get_instruments().resolve(symbol)
        except KeyError:
            return None
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
                "ema history fetch failed", extra={"symbol": symbol, "error": str(exc)}
            )
            return None
        emas = compute_emas([candle.close for candle in candles])
        if emas is not None:
            self._ema_cache[symbol] = (now, emas)
            redis_cache = self._get_cache()
            if redis_cache is not None:
                await redis_cache.set_safe(
                    f"breadth:emas:{symbol}", emas, ttl_seconds=EMA_CACHE_TTL_SECONDS
                )
        await asyncio.sleep(0.35)  # stay well inside broker rate limits
        return emas

    async def close(self) -> None:
        await self._session.close()
