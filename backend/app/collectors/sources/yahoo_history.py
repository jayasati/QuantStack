"""Yahoo Finance daily OHLCV history source.

Deep daily backfill for instruments whose broker history is shallow — Angel
One returns only a couple of daily bars for INDIAVIX, while Yahoo carries
years of ^INDIAVIX history.

Timestamp convention: Angel One stamps a daily bar at midnight IST of the
session date (18:30 UTC of the previous calendar day); Yahoo stamps it at the
session open (09:15 IST). Candles here are normalized to the Angel One
convention so the feature engines' timestamp joins line up.
"""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.core.logging import get_logger
from app.market.broker import Candle

logger = get_logger(__name__)

BASE_URL = "https://query1.finance.yahoo.com"
CHART_PATH = "/v8/finance/chart/{ticker}"

HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "accept": "application/json",
}

IST = ZoneInfo("Asia/Kolkata")

YAHOO_TICKERS = {
    "INDIAVIX": "^INDIAVIX",
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "SENSEX": "^BSESN",
}


def yahoo_ticker(symbol: str) -> str:
    """Map a friendly symbol to its Yahoo ticker; unmapped symbols are treated
    as NSE equities (.NS suffix)."""
    upper = symbol.upper()
    return YAHOO_TICKERS.get(upper, f"{upper}.NS")


class YahooDailyHistory:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=BASE_URL, headers=HEADERS, timeout=20.0
        )

    async def fetch_daily(self, symbol: str, lookback: str = "5y") -> list[Candle]:
        ticker = yahoo_ticker(symbol)
        response = await self._client.get(
            CHART_PATH.format(ticker=ticker),
            params={"range": lookback, "interval": "1d"},
        )
        response.raise_for_status()
        return self._parse(symbol, response.json())

    @staticmethod
    def _parse(symbol: str, data: dict[str, Any]) -> list[Candle]:
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return []
        timestamps = result[0].get("timestamp") or []
        quotes = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
        opens = quotes.get("open") or []
        highs = quotes.get("high") or []
        lows = quotes.get("low") or []
        closes = quotes.get("close") or []
        volumes = quotes.get("volume") or []

        candles: list[Candle] = []
        for idx, epoch in enumerate(timestamps):
            try:
                open_, high, low, close = opens[idx], highs[idx], lows[idx], closes[idx]
            except IndexError:
                break
            if None in (open_, high, low, close):
                continue
            session_open = datetime.fromtimestamp(epoch, tz=IST)
            bar_ts = session_open.replace(hour=0, minute=0, second=0, microsecond=0)
            volume = volumes[idx] if idx < len(volumes) and volumes[idx] else 0
            candles.append(
                Candle(
                    symbol=symbol,
                    interval="D",
                    open=float(open_),
                    high=float(high),
                    low=float(low),
                    close=float(close),
                    volume=int(volume),
                    timestamp=bar_ts,
                )
            )
        return candles

    async def close(self) -> None:
        await self._client.aclose()
