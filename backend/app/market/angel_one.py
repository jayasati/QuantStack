"""Angel One SmartAPI adapter (Volume 2, Prompt 2.1).

Real REST implementation of the broker abstraction: TOTP login, automatic
token refresh, quotes, historical candles, rate limiting, exponential
backoff, and structured error reporting. The rest of the system depends only
on ``BrokerInterface`` and never imports SmartAPI specifics.
"""

import asyncio
from datetime import datetime
from typing import Any

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.market.broker import BrokerError, BrokerInterface, Candle, Quote

logger = get_logger(__name__)

BASE_URL = "https://apiconnect.angelone.in"
LOGIN_PATH = "/rest/auth/angelbroking/user/v1/loginByPassword"
REFRESH_PATH = "/rest/auth/angelbroking/jwt/v1/generateTokens"
QUOTE_PATH = "/rest/secure/angelbroking/market/v1/quote/"
CANDLE_PATH = "/rest/secure/angelbroking/historical/v1/getCandleData"

INTERVAL_MAP = {
    "1m": "ONE_MINUTE",
    "3m": "THREE_MINUTE",
    "5m": "FIVE_MINUTE",
    "15m": "FIFTEEN_MINUTE",
    "30m": "THIRTY_MINUTE",
    "1H": "ONE_HOUR",
    "D": "ONE_DAY",
}


class AngelOneAdapter(BrokerInterface):
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        max_retries: int | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(base_url=BASE_URL, timeout=15.0)
        self._max_retries = max_retries if max_retries is not None else settings.max_retry
        self._jwt_token: str | None = None
        self._refresh_token: str | None = None
        self._feed_token: str | None = None
        self._rate_semaphore = asyncio.Semaphore(settings.rate_limits.angel_one_per_second)

    # --- auth --------------------------------------------------------------------

    def _totp(self) -> str:
        import pyotp

        if not self._settings.angel_one_totp_secret:
            raise BrokerError("angel_one_totp_secret is not configured")
        return pyotp.TOTP(self._settings.angel_one_totp_secret).now()

    def _headers(self, authenticated: bool = True) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": self._settings.angel_one_api_key or "",
        }
        if authenticated and self._jwt_token:
            headers["Authorization"] = f"Bearer {self._jwt_token}"
        return headers

    async def connect(self) -> None:
        if not (self._settings.angel_one_api_key and self._settings.angel_one_client_id):
            logger.warning("angel one credentials not configured; adapter runs disconnected")
            return
        payload = {
            "clientcode": self._settings.angel_one_client_id,
            "password": self._settings.angel_one_pin,
            "totp": self._totp(),
        }
        data = await self._request(
            "POST", LOGIN_PATH, json=payload, authenticated=False
        )
        tokens = data.get("data") or {}
        self._jwt_token = tokens.get("jwtToken")
        self._refresh_token = tokens.get("refreshToken")
        self._feed_token = tokens.get("feedToken")
        if not self._jwt_token:
            raise BrokerError(f"login succeeded but no jwt token returned: {data}")
        logger.info("angel one adapter connected")

    async def refresh_session(self) -> None:
        if not self._refresh_token:
            await self.connect()
            return
        data = await self._request(
            "POST",
            REFRESH_PATH,
            json={"refreshToken": self._refresh_token},
            authenticated=True,
            allow_refresh=False,
        )
        tokens = data.get("data") or {}
        self._jwt_token = tokens.get("jwtToken") or self._jwt_token
        self._refresh_token = tokens.get("refreshToken") or self._refresh_token
        logger.info("angel one session refreshed")

    async def disconnect(self) -> None:
        self._jwt_token = None
        self._refresh_token = None
        await self._client.aclose()

    async def is_connected(self) -> bool:
        return self._jwt_token is not None

    def stream_credentials(self) -> dict[str, str] | None:
        """Credentials for the SmartAPI WebSocket feed (None until connected)."""
        if not (self._jwt_token and self._feed_token and self._settings.angel_one_api_key):
            return None
        return {
            "jwt_token": self._jwt_token,
            "api_key": self._settings.angel_one_api_key,
            "client_code": self._settings.angel_one_client_id or "",
            "feed_token": self._feed_token,
        }

    # --- transport with retry / backoff / token refresh ---------------------------

    async def _request(
        self,
        method: str,
        path: str,
        json: dict | None = None,
        authenticated: bool = True,
        allow_refresh: bool = True,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 2):
            try:
                async with self._rate_semaphore:
                    response = await self._client.request(
                        method, path, json=json, headers=self._headers(authenticated)
                    )
                if response.status_code == 401 and allow_refresh and authenticated:
                    await self.refresh_session()
                    continue
                response.raise_for_status()
                body = response.json()
                if body.get("status") is False or body.get("success") is False:
                    raise BrokerError(
                        f"smartapi error [{body.get('errorcode')}]: {body.get('message')}"
                    )
                return body
            except BrokerError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt <= self._max_retries:
                    delay = 0.5 * (2 ** (attempt - 1))
                    logger.warning(
                        "broker request failed; retrying",
                        extra={"path": path, "attempt": attempt, "error": str(exc)},
                    )
                    await asyncio.sleep(delay)
        raise BrokerError(f"broker request failed after retries: {last_error}") from last_error

    # --- market data ---------------------------------------------------------------

    async def get_quote(self, symbol: str, exchange: str = "NSE") -> Quote:
        data = await self._request(
            "POST",
            QUOTE_PATH,
            json={"mode": "FULL", "exchangeTokens": {exchange: [symbol]}},
        )
        fetched = (data.get("data") or {}).get("fetched") or []
        if not fetched:
            raise BrokerError(f"no quote returned for {exchange}:{symbol}")
        row = fetched[0]
        depth = row.get("depth") or {}
        best_buy = (depth.get("buy") or [{}])[0]
        best_sell = (depth.get("sell") or [{}])[0]
        return Quote(
            symbol=row.get("tradingSymbol", symbol),
            exchange=exchange,
            last_price=float(row.get("ltp", 0.0)),
            open=_opt_float(row.get("open")),
            high=_opt_float(row.get("high")),
            low=_opt_float(row.get("low")),
            close=_opt_float(row.get("close")),
            volume=int(row.get("tradeVolume") or 0),
            vwap=_opt_float(row.get("avgPrice")),
            bid=_opt_float(best_buy.get("price")),
            ask=_opt_float(best_sell.get("price")),
            bid_qty=int(best_buy.get("quantity") or 0),
            ask_qty=int(best_sell.get("quantity") or 0),
            depth=depth,
            timestamp=datetime.now().astimezone(),
        )

    async def get_historical(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        exchange: str = "NSE",
    ) -> list[Candle]:
        api_interval = INTERVAL_MAP.get(interval)
        if api_interval is None:
            raise BrokerError(f"unsupported interval: {interval}")
        data = await self._request(
            "POST",
            CANDLE_PATH,
            json={
                "exchange": exchange,
                "symboltoken": symbol,
                "interval": api_interval,
                "fromdate": start.strftime("%Y-%m-%d %H:%M"),
                "todate": end.strftime("%Y-%m-%d %H:%M"),
            },
        )
        candles = []
        for row in data.get("data") or []:
            # SmartAPI candle row: [timestamp, open, high, low, close, volume]
            candles.append(
                Candle(
                    symbol=symbol,
                    interval=interval,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=int(row[5]),
                    timestamp=datetime.fromisoformat(row[0]),
                )
            )
        return candles


def _opt_float(value: Any) -> float | None:
    return float(value) if value is not None else None
