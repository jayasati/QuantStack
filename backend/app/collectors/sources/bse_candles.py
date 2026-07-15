"""BSE intraday candle source -- today-only fallback for
HistoricalCandleCollector, the BSE-listed counterpart to nse_candles.py
(same DEBT-2 motivation: the broker's own candle pipeline can silently lag
real time for hours). NSE has zero data for BSE-listed instruments (Sensex),
so this is the only exchange-native fallback for them -- see
RoutingOptionsChainSource's identical NSE/BSE split for the precedent.

Discovered from a live captured browser session (2026-07-15), verified by
direct probe against api.bseindia.com: `SensexGraphData/w?index=16&flag=0
&sector=&seriesid=R&frd=null&tod=null` returns today's per-minute tick
series for the given BSE *index* code. Note this "index code" (16 for
Sensex) is a different numbering namespace from bse_options.py's
`SCRIP_CODES` ("1" for Sensex) -- confirmed live, these are genuinely
different BSE API families, not a typo.

Response shape is unusual: the HTTP body is itself a JSON *string* (not an
object) containing two JSON array literals joined by the literal delimiter
`#@#` -- a snapshot-summary array, then the actual per-minute tick array.
Decoding requires json.loads() twice: once for the outer string envelope,
once per inner array after splitting on the delimiter.

Same anti-bot posture as bse_options.py: header-less/cookie-less requests
get redirected instead of served JSON, so the same warm-up-then-retry
pattern is required here too.
"""

import asyncio
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.collectors.base import CollectionError
from app.collectors.sources.candle_aggregate import bucket_ticks_into_candles
from app.core.logging import get_logger
from app.market.broker import Candle

logger = get_logger(__name__)

SITE_BASE = "https://www.bseindia.com"
API_BASE = "https://api.bseindia.com"

HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": SITE_BASE,
}

IST = ZoneInfo("Asia/Kolkata")

# BSE's own numeric "index code" per underlying, as required by
# SensexGraphData's `index` param -- verified live 2026-07-15. Distinct
# from bse_options.py's SCRIP_CODES; do not merge the two without
# independently verifying each new entry against both API families.
BSE_INDEX_CODES: dict[str, str] = {"SENSEX": "16"}


def _parse_sensex_graph_response(body: str) -> list[tuple[datetime, float]]:
    """body is the JSON-decoded-once response: a string containing two
    JSON array literals joined by "#@#". The second array is the tick
    series: [{"date": "Wed Jul 15 2026 09:00:59", "value1": "77579.95"}, ...],
    date in IST (BSE's own site timezone), naive (no tz suffix)."""
    parts = body.split("#@#")
    if len(parts) < 2:
        return []
    try:
        rows = json.loads(parts[1])
    except (ValueError, TypeError):
        return []
    ticks: list[tuple[datetime, float]] = []
    for row in rows:
        date_raw = row.get("date")
        value_raw = row.get("value1")
        if not date_raw or value_raw is None:
            continue
        try:
            naive = datetime.strptime(date_raw, "%a %b %d %Y %H:%M:%S")
            ts = naive.replace(tzinfo=IST)
            price = float(value_raw)
        except (ValueError, TypeError):
            continue
        ticks.append((ts, price))
    return ticks


class BseCandleSource:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        warm_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=API_BASE, headers=HEADERS, timeout=20.0, follow_redirects=False,
        )
        self._warm_client = warm_client or httpx.AsyncClient(
            base_url=SITE_BASE, headers=HEADERS, timeout=20.0, follow_redirects=True,
        )
        self._warmed = False

    async def _warm_up(self, index_code: str) -> None:
        response = await self._warm_client.get(f"/sensex/code/{index_code}")
        response.raise_for_status()
        self._client.cookies.update(self._warm_client.cookies)
        self._warmed = True

    async def fetch_today(self, symbol: str, interval: str) -> list[Candle]:
        """Today's `interval`-bucketed candles for `symbol`, or [] if this
        symbol has no known BSE index code, the request fails, or BSE
        returns no data -- never raises, matching every other source in
        this package."""
        index_code = BSE_INDEX_CODES.get(symbol.upper())
        if index_code is None:
            return []

        try:
            if not self._warmed:
                await self._warm_up(index_code)
            body: Any = None
            for attempt in (1, 2):
                response = await self._client.get(
                    "/BseIndiaAPI/api/SensexGraphData/w",
                    params={
                        "index": index_code, "flag": "0", "sector": "",
                        "seriesid": "R", "frd": "null", "tod": "null",
                    },
                )
                if response.status_code in (301, 302, 401, 403) and attempt == 1:
                    logger.info("bse candle session rejected; re-warming cookies")
                    await self._warm_up(index_code)
                    await asyncio.sleep(0.5)
                    continue
                response.raise_for_status()
                body = response.json()
                break
            if not isinstance(body, str):
                raise CollectionError(f"unexpected bse candle response shape: {type(body)}")
        except Exception as exc:
            logger.warning(
                "bse candle fetch failed",
                extra={"symbol": symbol, "interval": interval, "error": str(exc)},
            )
            return []

        ticks = _parse_sensex_graph_response(body)
        return bucket_ticks_into_candles(symbol, interval, ticks)

    async def close(self) -> None:
        await self._client.aclose()
        await self._warm_client.aclose()
