"""Yahoo Finance macro source (real feed for Prompt 2.8).

Fetches daily history for each macro factor from Yahoo's public chart API and
computes {value, change_1d_pct, zscore_20d} per the MacroSource contract.

Coverage notes:
- INDIA10Y has no public Yahoo ticker and is omitted (the collector
  renormalizes weights over present factors); tracked in the backlog.
- CRYPTO_MCAP uses BTC-USD as its proxy — bitcoin dominates total crypto
  market cap and tracks its daily changes almost one-for-one; the factor is
  an optional risk-appetite signal, not a precise capitalization figure.
"""

import asyncio
import time
from statistics import fmean, pstdev
from typing import Any

import httpx

from app.collectors.base import CollectionError
from app.collectors.domains.macro import MacroFactorPayload, MacroSource
from app.core.logging import get_logger

logger = get_logger(__name__)

BASE_URL = "https://query1.finance.yahoo.com"
CHART_PATH = "/v8/finance/chart/{symbol}"

TICKERS: dict[str, str] = {
    "USDINR": "INR=X",
    "DXY": "DX-Y.NYB",
    "US10Y": "^TNX",
    "CRUDE": "CL=F",
    "GOLD": "GC=F",
    "SILVER": "SI=F",
    "NATGAS": "NG=F",
    "SPX": "^GSPC",
    "NDX": "^NDX",
    "NIKKEI": "^N225",
    "HANGSENG": "^HSI",
    "DAX": "^GDAXI",
    "CRYPTO_MCAP": "BTC-USD",  # documented proxy, see module docstring
}

HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "accept": "application/json",
}

CACHE_TTL_SECONDS = 240
MIN_FACTORS = 5  # fewer than this and the snapshot is too thin to be useful
ZSCORE_WINDOW = 20


def factor_metrics(
    closes: list[float | None], live_value: float | None
) -> MacroFactorPayload | None:
    """Compute {value, change_1d_pct, zscore_20d} from daily closes."""
    series = [c for c in closes if c is not None]
    if live_value is not None:
        if not series or abs(series[-1] - live_value) > 1e-9:
            series = [*series, live_value]
    if len(series) < 2:
        return None
    value = series[-1]
    prev = series[-2]
    change_1d = (value / prev - 1.0) * 100.0 if prev else None

    window = series[-ZSCORE_WINDOW:]
    zscore: float | None = None
    if len(window) >= 10:  # need a reasonable window for a meaningful z-score
        mean = fmean(window)
        std = pstdev(window)
        zscore = (value - mean) / std if std > 0 else 0.0
    return {"value": value, "change_1d_pct": change_1d, "zscore_20d": zscore}


class YahooMacroSource(MacroSource):
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=BASE_URL, headers=HEADERS, timeout=15.0
        )
        self._cache: tuple[float, dict[str, MacroFactorPayload]] | None = None
        self._semaphore = asyncio.Semaphore(4)

    async def fetch_macro(self) -> dict[str, MacroFactorPayload]:
        now = time.time()
        if self._cache is not None and now - self._cache[0] < CACHE_TTL_SECONDS:
            return self._cache[1]

        results = await asyncio.gather(
            *(self._fetch_factor(factor, symbol) for factor, symbol in TICKERS.items())
        )
        factors: dict[str, MacroFactorPayload] = {
            factor: payload for factor, payload in results if payload is not None
        }
        if len(factors) < MIN_FACTORS:
            raise CollectionError(
                f"only {len(factors)} macro factors available (need {MIN_FACTORS})"
            )
        self._cache = (now, factors)
        return factors

    async def _fetch_factor(
        self, factor: str, symbol: str
    ) -> tuple[str, MacroFactorPayload | None]:
        try:
            async with self._semaphore:
                response = await self._client.get(
                    CHART_PATH.format(symbol=symbol),
                    params={"range": "3mo", "interval": "1d"},
                )
            response.raise_for_status()
            payload = self._parse_chart(response.json())
            return factor, payload
        except Exception as exc:
            logger.warning(
                "macro factor fetch failed",
                extra={"factor": factor, "symbol": symbol, "error": str(exc)},
            )
            return factor, None

    @staticmethod
    def _parse_chart(data: dict[str, Any]) -> MacroFactorPayload | None:
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return None
        meta = result[0].get("meta") or {}
        quotes = (result[0].get("indicators") or {}).get("quote") or [{}]
        closes = quotes[0].get("close") or []
        return factor_metrics(closes, meta.get("regularMarketPrice"))

    async def close(self) -> None:
        await self._client.aclose()
