"""BSE option-chain source (Sensex/Bankex — real feed).

NSE's public option-chain feed has zero BSE data (different exchange, and
Sensex/Bankex options are BSE-listed, not NSE). Angel One SmartAPI has no
option-chain endpoint at all (confirmed against Angel One's own SmartAPI
forum: "We do not provide option chain data as of now, however we do
provide candle data and Greeks for options"), so there is no broker-native
alternative either. This mirrors ``NseOptionChainSource`` against BSE's own
public API instead, discovered and verified live (2026-07-13) from a real
browser session's captured network requests, then confirmed by direct probe
against ``api.bseindia.com``.

BSE's chain carries per-leg IV natively, but not Delta/Gamma/Theta/Vega —
and unlike NSE, there's no broker enrichment path either: SmartAPI's Option
Greek endpoint has zero data for BSE names (confirmed by direct probe,
2026-07-13 — NSE-only coverage). So instead of a broker-fetch enrichment
step, this computes Greeks via Black-Scholes from the real market IV BSE
already gives us (``app.market.black_scholes`` — standard practice, not
fabrication: Greeks are always IV-derived, there's no separate "true" value
an exchange could publish instead).

Same anti-bot posture as NSE: header-less/cookie-less requests get redirected
to a generic page instead of serving JSON, so a browser-like warm-up (visit
a real page first, carry its cookies) plus a re-warm-and-retry on rejection
is required here too.

Previous-day spot (buildup classification) reuses the same ohlcv_candles
read NseOptionChainSource uses — never fabricated.
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.collectors.base import CollectionError
from app.collectors.domains.options import OptionsChainSource
from app.core.logging import get_logger
from app.market.black_scholes import black_scholes_greeks

logger = get_logger(__name__)

SITE_BASE = "https://www.bseindia.com"
API_BASE = "https://api.bseindia.com"
WARMUP_PATH = "/markets/equity/EQReports/OptionChain.aspx"

HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "referer": f"{SITE_BASE}/",
    "origin": SITE_BASE,
}

# BSE's numeric scrip code per underlying. Only SENSEX is confirmed against a
# live response; other BSE indices (e.g. Bankex) are left unmapped rather
# than guessed until independently verified the same way.
SCRIP_CODES: dict[str, str] = {"SENSEX": "1"}
BSE_INSTRUMENTS = frozenset(SCRIP_CODES)

# Index option expiry cutoff is 15:30 IST; expressed in UTC (IST = UTC+5:30)
# since the rest of this codebase standardizes on UTC datetimes.
EXPIRY_CUTOFF_UTC_HOUR = 10


class BseOptionChainSource(OptionsChainSource):
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        warm_client: httpx.AsyncClient | None = None,
        min_fetch_interval_seconds: float = 30.0,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=API_BASE, headers=HEADERS, timeout=20.0, follow_redirects=False,
        )
        # A separate client for the (HTML) warm-up page: it 301-redirects,
        # which the API client deliberately does not follow (a redirect
        # there means BSE rejected the request, not a normal page load).
        self._warm_client = warm_client or httpx.AsyncClient(
            base_url=SITE_BASE, headers=HEADERS, timeout=20.0, follow_redirects=True,
        )
        self._warmed = False
        self._min_interval = min_fetch_interval_seconds
        self._last_fetch: dict[str, float] = {}
        self._last_chain: dict[str, dict[str, Any]] = {}

    async def _warm_up(self) -> None:
        response = await self._warm_client.get(WARMUP_PATH)
        response.raise_for_status()
        self._client.cookies.update(self._warm_client.cookies)
        self._warmed = True

    async def _get_json(self, path: str, params: dict[str, str]) -> Any:
        if not self._warmed:
            await self._warm_up()
        for attempt in (1, 2):
            response = await self._client.get(path, params=params)
            if response.status_code in (301, 302, 401, 403) and attempt == 1:
                logger.info("bse session rejected; re-warming cookies")
                await self._warm_up()
                await asyncio.sleep(0.5)
                continue
            response.raise_for_status()
            try:
                return response.json()
            except ValueError as exc:  # HTML error/redirect page
                if attempt == 1:
                    self._warmed = False
                    continue
                raise CollectionError(f"bse returned non-json response: {exc}") from exc
        raise CollectionError("bse option chain request failed after retry")

    async def fetch_chain(self, instrument: str) -> dict[str, Any]:
        symbol = instrument.upper()
        scrip_cd = SCRIP_CODES.get(symbol)
        if scrip_cd is None:
            raise CollectionError(f"no BSE scrip code mapped for {symbol}")

        # Respect BSE by reusing the last chain within the fetch interval.
        now = time.time()
        last = self._last_fetch.get(symbol)
        if last is not None and now - last < self._min_interval and symbol in self._last_chain:
            return self._last_chain[symbol]

        nearest_expiry = await self._nearest_expiry(scrip_cd)
        payload = await self._get_json(
            "/BseIndiaAPI/api/DerivOptionChain_IV/w",
            {"Expiry": nearest_expiry, "scrip_cd": scrip_cd, "strprice": "0"},
        )
        chain = map_bse_chain(payload, expiry=nearest_expiry)
        chain["prev_spot"] = await self._prev_close(symbol)
        enrich_with_computed_greeks(chain, expiry=nearest_expiry)
        self._last_fetch[symbol] = now
        self._last_chain[symbol] = chain
        return chain

    async def _nearest_expiry(self, scrip_cd: str) -> str:
        """BSE's own most-active-options widget always reflects the nearest
        live expiry per its Expiryofcontract field -- cheaper and more
        reliable than reverse-engineering a dedicated dropdown endpoint."""
        payload = await self._get_json("/BseIndiaAPI/api/SensexDeri/w", {"code": "16"})
        return pick_nearest_expiry(payload.get("Table") or [], scrip_cd)

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
        await self._warm_client.aclose()


def pick_nearest_expiry(rows: list[dict[str, Any]], scrip_cd: str) -> str:
    """Earliest Expiryofcontract for scrip_cd, compared as real dates -- e.g.
    '13 Aug 2026' sorts before '16 Jul 2026' lexicographically but is later
    chronologically, so a plain string min() would silently pick wrong."""
    expiries = [
        row.get("Expiryofcontract")
        for row in rows
        if str(row.get("Scrip_cd")) == scrip_cd and row.get("Expiryofcontract")
    ]
    if not expiries:
        raise CollectionError(f"bse returned no live expiries for scrip_cd {scrip_cd}")
    return min(expiries, key=lambda text: datetime.strptime(text, "%d %b %Y"))


def enrich_with_computed_greeks(chain: dict[str, Any], expiry: str) -> None:
    """Delta/Gamma/Theta/Vega via Black-Scholes from BSE's own real IV --
    see app.market.black_scholes for why this is the correct approach
    (neither BSE nor SmartAPI publish raw Greeks for BSE-listed contracts).
    Skips a leg entirely rather than writing a fabricated 0 when its IV is
    missing (untraded strike) or expiry has already passed."""
    expiry_date = datetime.strptime(expiry, "%d %b %Y").replace(tzinfo=UTC)
    expiry_at = expiry_date + timedelta(hours=EXPIRY_CUTOFF_UTC_HOUR)
    time_to_expiry_years = (expiry_at - datetime.now(UTC)).total_seconds() / (365 * 24 * 3600)
    spot = chain["spot"]
    enriched = 0
    for row in chain["strikes"]:
        strike = row["strike"]
        for leg_key, option_type in (("call", "CE"), ("put", "PE")):
            leg = row[leg_key]
            iv = leg.get("iv")
            if iv is None:
                continue
            greeks = black_scholes_greeks(
                spot=spot, strike=strike, iv_pct=iv,
                time_to_expiry_years=time_to_expiry_years, option_type=option_type,
            )
            if greeks is None:
                continue
            leg["delta"] = greeks.delta
            leg["gamma"] = greeks.gamma
            leg["theta"] = greeks.theta
            leg["vega"] = greeks.vega
            enriched += 1
    chain["greeks_enriched_legs"] = enriched
    chain["greeks_source"] = "computed_black_scholes"


def map_bse_chain(payload: dict[str, Any], expiry: str) -> dict[str, Any]:
    """Map a BSE DerivOptionChain_IV response into the collector's chain
    shape. One row per strike, call fields "C_"-prefixed, put fields bare."""
    rows = payload.get("Table") or []
    if not rows:
        raise CollectionError(f"bse option chain response is missing data for expiry {expiry}")
    spot = _to_float(rows[0].get("UlaValue"))
    if spot is None:
        raise CollectionError("bse option chain response is missing UlaValue (spot)")
    strikes = [
        {
            "strike": _to_float(row.get("Strike_Price1")),
            "call": _map_leg(row, "C_"),
            "put": _map_leg(row, ""),
        }
        for row in rows
        if row.get("Strike_Price1") is not None
    ]
    if not strikes:
        raise CollectionError(f"no strikes found for expiry {expiry}")
    return {"spot": spot, "strikes": strikes, "expiry": expiry}


def _map_leg(row: dict[str, Any], prefix: str) -> dict[str, Any]:
    return {
        "oi": _to_float(row.get(f"{prefix}Open_Interest")) or 0,
        "oi_change": _to_float(row.get(f"{prefix}Absolute_Change_OI")) or 0,
        "iv": _to_float(row.get(f"{prefix}IV")),
        "volume": _to_float(row.get(f"{prefix}Vol_Traded")) or 0,
        "ltp": _to_float(row.get(f"{prefix}Last_Trd_Price")),
    }


def _to_float(value: Any) -> float | None:
    """BSE returns numbers as strings, comma-formatted, empty ("") for
    strikes with no activity yet, or "-" as a placeholder -- all read as
    missing rather than fabricated as zero (except OI/volume, which default
    to 0 in ``_map_leg`` since "no trades yet" is a real zero for those)."""
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text or text == "-":
        return None
    try:
        return float(text)
    except ValueError:
        return None
