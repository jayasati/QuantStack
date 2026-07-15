"""NSE intraday candle source -- today-only fallback for
HistoricalCandleCollector when the broker's own candle endpoint is slow or
degraded (perf-audit/DEBT-2, 2026-07-14/15: Angel One's historical-candle
backend fell ~5.5h behind real time for hours while its own live tick feed
stayed fresh; the broker's *aggregated candle* pipeline can silently lag
independent of raw market data availability).

NSE's public quote-page API exposes today's intraday price action as a raw
tick series (~60s cadence, not pre-built OHLC bars) via two different
endpoints depending on instrument type -- discovered from a live captured
browser session (2026-07-15), verified by direct probe against
nseindia.com for both shapes:

- Equities: `getSymbolChartData&symbol=<TICKER>EQN&days=1D`, response is
  `{"grapthData": [[epoch_ms, price, phase, change, changepct], ...]}`
  (flat, not nested).
- Indices: `getGraphChart&&type=<NAME>&flag=1D`, response is
  `{"data": {"grapthData": [[epoch_ms, price, ...], ...]}}` (nested under
  "data" -- a different response shape from the equity endpoint, not just a
  different symbol convention).

Only covers what NSE itself lists (equities + NSE-native indices); BSE-listed
instruments (Sensex) have zero NSE data by design -- see bse_candles.py.
Deliberately today-only: NSE's public site does not expose a reliable
historical *intraday* range API (verified 2026-07-15: the old
`/api/historical/cm/equity` and `/api/chart-databyindex` endpoints this
codebase's `nse_options.py`-era sources used are dead -- 503/empty -- NSE
has since migrated to this Next.js API surface). Multi-day intraday backfill
still has to come from the broker or Yahoo.
"""

from datetime import UTC, datetime
from typing import Any

from app.collectors.sources.candle_aggregate import bucket_ticks_into_candles
from app.collectors.sources.nse_client import NseSession
from app.core.logging import get_logger
from app.market.broker import Candle

logger = get_logger(__name__)

# NSE's own display name for each index, as required by getGraphChart's
# `type` param -- verified live 2026-07-15, do not guess further entries
# without the same live-probe discipline the rest of this module used.
NSE_INDEX_NAMES: dict[str, str] = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
}


def _parse_grapth_data(rows: list[list[Any]]) -> list[tuple[datetime, float]]:
    ticks: list[tuple[datetime, float]] = []
    for row in rows:
        if not row or row[0] is None or row[1] is None:
            continue
        try:
            ts = datetime.fromtimestamp(row[0] / 1000, tz=UTC)
            price = float(row[1])
        except (TypeError, ValueError, OSError):
            continue
        ticks.append((ts, price))
    return ticks


class NseCandleSource:
    def __init__(self, session: NseSession | None = None) -> None:
        self._session = session or NseSession(warmup_path="/get-quote/equity/HDFCBANK/HDFC-Bank-Limited")

    async def fetch_today(self, symbol: str, interval: str) -> list[Candle]:
        """Today's `interval`-bucketed candles for `symbol`, or [] if this
        symbol isn't NSE-native, the request fails, or NSE returns no data
        (e.g. before market open) -- never raises, matching every other
        source in this collectors/sources package."""
        upper = symbol.upper()
        try:
            if upper in NSE_INDEX_NAMES:
                raw = await self._session.get_json(
                    "/api/NextApi/apiClient"
                    f"?functionName=getGraphChart&&type={NSE_INDEX_NAMES[upper]}&flag=1D"
                )
                rows = ((raw.get("data") or {}).get("grapthData")) or []
            else:
                raw = await self._session.get_json(
                    "/api/NextApi/apiClient/GetQuoteApi"
                    f"?functionName=getSymbolChartData&symbol={upper}EQN&days=1D"
                )
                rows = raw.get("grapthData") or []
        except Exception as exc:
            logger.warning(
                "nse candle fetch failed",
                extra={"symbol": symbol, "interval": interval, "error": str(exc)},
            )
            return []

        ticks = _parse_grapth_data(rows)
        return bucket_ticks_into_candles(symbol, interval, ticks)

    async def close(self) -> None:
        await self._session.close()
