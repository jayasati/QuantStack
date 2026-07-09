"""NSE option-chain source (real feed for Prompt 2.4).

Fetches the public NSE option-chain API and maps it into the shape expected
by ``OptionsIntelligenceCollector``. NSE requires a browser-like cookie
handshake: we warm the session against the option-chain page first and
re-warm whenever the API rejects us.

Previous-day spot (needed for buildup classification) comes from our own
``ohlcv_candles`` daily bars when available — never fabricated.
"""

import asyncio
import time
from typing import Any

import httpx

from app.collectors.base import CollectionError
from app.collectors.domains.options import OptionsChainSource
from app.core.logging import get_logger

logger = get_logger(__name__)

BASE = "https://www.nseindia.com"
WARMUP_PATH = "/option-chain"
INDICES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}

HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "referer": f"{BASE}{WARMUP_PATH}",
}


class NseOptionChainSource(OptionsChainSource):
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        min_fetch_interval_seconds: float = 30.0,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=BASE, headers=HEADERS, timeout=20.0, follow_redirects=True
        )
        self._warmed = False
        self._min_interval = min_fetch_interval_seconds
        self._last_fetch: dict[str, float] = {}
        self._last_chain: dict[str, dict[str, Any]] = {}

    async def _warm_up(self) -> None:
        response = await self._client.get(WARMUP_PATH)
        response.raise_for_status()
        self._warmed = True

    async def _get_json(self, path: str) -> dict[str, Any]:
        if not self._warmed:
            await self._warm_up()
        for attempt in (1, 2):
            response = await self._client.get(path)
            if response.status_code in (401, 403) and attempt == 1:
                logger.info("nse session rejected; re-warming cookies")
                await self._warm_up()
                await asyncio.sleep(0.5)
                continue
            response.raise_for_status()
            try:
                return response.json()
            except ValueError as exc:  # HTML error page
                if attempt == 1:
                    self._warmed = False
                    continue
                raise CollectionError(f"nse returned non-json response: {exc}") from exc
        raise CollectionError("nse option chain request failed after retry")

    async def fetch_chain(self, instrument: str) -> dict[str, Any]:
        symbol = instrument.upper()
        # Respect NSE by reusing the last chain within the fetch interval.
        now = time.time()
        last = self._last_fetch.get(symbol)
        if last is not None and now - last < self._min_interval and symbol in self._last_chain:
            return self._last_chain[symbol]

        # v3 API: expiries come from contract-info; the chain needs one expiry.
        info = await self._get_json(f"/api/option-chain-contract-info?symbol={symbol}")
        expiries = info.get("expiryDates") or []
        if not expiries:
            raise CollectionError(f"nse returned no expiries for {symbol}")
        nearest_expiry = expiries[0]

        kind = "Indices" if symbol in INDICES else "Equities"
        payload = await self._get_json(
            f"/api/option-chain-v3?type={kind}&symbol={symbol}&expiry={nearest_expiry}"
        )
        chain = map_nse_chain(payload, expiry=nearest_expiry)
        chain["prev_spot"] = await self._prev_close(symbol)
        await self._enrich_with_greeks(symbol, nearest_expiry, chain)
        self._last_fetch[symbol] = now
        self._last_chain[symbol] = chain
        return chain

    async def _enrich_with_greeks(
        self, symbol: str, expiry: str, chain: dict[str, Any]
    ) -> None:
        """Merge broker option Greeks into chain legs (optional enrichment).

        The broker endpoint only serves data during market hours; failures or
        empty responses simply leave the legs without Greeks.
        """
        try:
            from app.core.container import container
            from app.market.broker import BrokerInterface

            broker = container.resolve(BrokerInterface)
            greeks = await broker.get_option_greeks(symbol, _smartapi_expiry(expiry))
        except Exception as exc:
            logger.debug(
                "greeks enrichment unavailable",
                extra={"symbol": symbol, "error": str(exc)},
            )
            return
        if not greeks:
            return
        enriched = 0
        for strike_row in chain["strikes"]:
            strike = float(strike_row["strike"])
            for side, leg_key in (("CE", "call"), ("PE", "put")):
                entry = greeks.get((strike, side))
                if entry:
                    strike_row[leg_key].update(
                        {k: v for k, v in entry.items()
                         if k in ("delta", "gamma", "theta", "vega")}
                    )
                    enriched += 1
        chain["greeks_enriched_legs"] = enriched

    async def _prev_close(self, symbol: str) -> float | None:
        """Previous daily close from our own candle store (None if unavailable)."""
        try:
            from sqlalchemy import desc, select

            from app.database.session import get_session_factory
            from app.database.tables import OhlcvCandle

            sessions = get_session_factory()
            async with sessions() as session:
                result = await session.execute(
                    select(OhlcvCandle.close)
                    .where(OhlcvCandle.symbol == symbol, OhlcvCandle.timeframe == "D")
                    .order_by(desc(OhlcvCandle.ts))
                    .offset(1)  # latest bar may be today; take the one before
                    .limit(1)
                )
                value = result.scalar()
            return float(value) if value is not None else None
        except Exception:
            return None

    async def close(self) -> None:
        await self._client.aclose()


def map_nse_chain(payload: dict[str, Any], expiry: str | None = None) -> dict[str, Any]:
    """Map an NSE option-chain response into the collector's chain shape.

    Handles both the v3 response (rows already filtered to the requested
    expiry; rows carry an ``expiryDates`` list) and the legacy response
    (rows for all expiries; each row carries a single ``expiryDate``).
    """
    records = payload.get("records") or {}
    rows = records.get("data") or []
    spot = records.get("underlyingValue")
    if spot is None or not rows:
        raise CollectionError("nse option chain response is missing data")

    expiries = records.get("expiryDates") or []
    target_expiry = expiry or (expiries[0] if expiries else None)
    strikes: list[dict[str, Any]] = []
    for row in rows:
        row_expiry = row.get("expiryDate")
        if row_expiry is not None and target_expiry is not None and row_expiry != target_expiry:
            continue  # legacy multi-expiry response: keep nearest expiry only
        strikes.append(
            {
                "strike": row.get("strikePrice"),
                "call": _map_leg(row.get("CE")),
                "put": _map_leg(row.get("PE")),
            }
        )
    if not strikes:
        raise CollectionError(f"no strikes found for expiry {target_expiry}")
    return {
        "spot": float(spot),
        "strikes": strikes,
        "expiry": target_expiry,
        "as_of": records.get("timestamp"),
    }


def _map_leg(leg: dict[str, Any] | None) -> dict[str, Any]:
    if not leg:
        return {"oi": 0, "oi_change": 0, "iv": None, "volume": 0, "ltp": None}
    iv = leg.get("impliedVolatility")
    return {
        "oi": leg.get("openInterest") or 0,
        "oi_change": leg.get("changeinOpenInterest") or 0,
        # NSE reports 0 for strikes without a computable IV — treat as missing.
        "iv": float(iv) if iv else None,
        "volume": leg.get("totalTradedVolume") or 0,
        "ltp": leg.get("lastPrice") or None,
    }


_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _smartapi_expiry(nse_expiry: str) -> str:
    """Convert an NSE expiry (07-Jul-2026) to SmartAPI format (07JUL2026)."""
    day, month, year = nse_expiry.split("-")
    month_upper = month.upper()[:3]
    if month_upper not in _MONTHS:
        raise ValueError(f"unrecognized expiry month: {nse_expiry}")
    return f"{day}{month_upper}{year}"
