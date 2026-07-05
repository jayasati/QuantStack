"""Angel One SmartAPI WebSocket v2 feed (Volume 2, Prompt 2.2).

Streams live ticks over wss://smartapisocket.angelone.in/smart-stream using
the documented binary protocol. The LiveMarketCollector reads the latest tick
per symbol from this feed and falls back to REST polling for any symbol whose
stream is stale or disconnected.

Binary layout (little-endian):
  LTP mode (1), 51 bytes:
    [0]      subscription mode        int8
    [1]      exchange type            int8
    [2:27]   token                    ascii, null padded
    [27:35]  sequence number          int64
    [35:43]  exchange timestamp (ms)  int64
    [43:51]  last traded price        int64 (paise; divide by 100)
  Quote mode (2), 123 bytes: LTP fields plus
    [51:59]  last traded quantity     int64
    [59:67]  average traded price     int64 (paise)
    [67:75]  volume traded today      int64
    [75:83]  total buy quantity       double
    [83:91]  total sell quantity      double
    [91:99]  open                     int64 (paise)
    [99:107] high                     int64 (paise)
    [107:115] low                     int64 (paise)
    [115:123] close                   int64 (paise)
"""

import asyncio
import contextlib
import json
import struct
import time
from dataclasses import dataclass, field
from typing import Any

import websockets

from app.core.logging import get_logger

logger = get_logger(__name__)

WS_URL = "wss://smartapisocket.angelone.in/smart-stream"
HEARTBEAT_SECONDS = 25

MODE_LTP = 1
MODE_QUOTE = 2

EXCHANGE_TYPE = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4, "MCX": 5, "CDS": 13}


def ws_token(rest_token: str) -> str:
    """REST index tokens are '999' + websocket token (99926000 -> 26000)."""
    if rest_token.startswith("999") and len(rest_token) == 8:
        return rest_token[3:]
    return rest_token


def parse_packet(data: bytes) -> dict[str, Any] | None:
    """Parse a binary tick packet (LTP or Quote mode)."""
    if len(data) < 51:
        return None
    mode = data[0]
    token = data[2:27].split(b"\x00", 1)[0].decode("ascii", errors="ignore")
    sequence, exchange_ts, ltp_paise = struct.unpack_from("<qqq", data, 27)
    tick: dict[str, Any] = {
        "mode": mode,
        "token": token,
        "exchange_type": data[1],
        "sequence": sequence,
        "exchange_timestamp_ms": exchange_ts,
        "ltp": ltp_paise / 100.0,
        "received_at": time.time(),
    }
    if mode >= MODE_QUOTE and len(data) >= 123:
        (ltq, atp_paise, volume) = struct.unpack_from("<qqq", data, 51)
        (buy_qty, sell_qty) = struct.unpack_from("<dd", data, 75)
        (open_p, high_p, low_p, close_p) = struct.unpack_from("<qqqq", data, 91)
        tick.update(
            {
                "last_traded_quantity": ltq,
                "avg_traded_price": atp_paise / 100.0,
                "volume": volume,
                "total_buy_quantity": buy_qty,
                "total_sell_quantity": sell_qty,
                "open": open_p / 100.0,
                "high": high_p / 100.0,
                "low": low_p / 100.0,
                "close": close_p / 100.0,
            }
        )
    return tick


@dataclass
class FeedMetrics:
    packets: int = 0
    dropped_packets: int = 0
    reconnects: int = 0
    last_packet_at: float | None = None
    extras: dict = field(default_factory=dict)


class AngelWebSocketFeed:
    """Maintains a live tick buffer per token with reconnect and heartbeat."""

    def __init__(
        self,
        jwt_token: str,
        api_key: str,
        client_code: str,
        feed_token: str,
        mode: int = MODE_QUOTE,
        max_backoff_seconds: float = 60.0,
    ) -> None:
        self._headers = {
            "Authorization": jwt_token,
            "x-api-key": api_key,
            "x-client-code": client_code,
            "x-feed-token": feed_token,
        }
        self._mode = mode
        self._max_backoff = max_backoff_seconds
        self._subscriptions: dict[int, set[str]] = {}
        self._ticks: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task | None = None
        self._running = False
        self.connected = False
        self.metrics = FeedMetrics()

    def subscribe(self, exchange: str, rest_token: str) -> None:
        exchange_type = EXCHANGE_TYPE.get(exchange, 1)
        self._subscriptions.setdefault(exchange_type, set()).add(ws_token(rest_token))

    def latest(self, rest_token: str, max_age_seconds: float = 30.0) -> dict[str, Any] | None:
        tick = self._ticks.get(ws_token(rest_token))
        if tick is None:
            return None
        if time.time() - tick["received_at"] > max_age_seconds:
            return None
        return tick

    async def start(self) -> None:
        if self._task is None:
            self._running = True
            self._task = asyncio.create_task(self._run(), name="angel-ws-feed")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.connected = False

    # --- internals -----------------------------------------------------------------

    def _subscribe_message(self) -> str:
        token_list = [
            {"exchangeType": exchange_type, "tokens": sorted(tokens)}
            for exchange_type, tokens in self._subscriptions.items()
            if tokens
        ]
        return json.dumps(
            {
                "correlationID": "quantstack",
                "action": 1,
                "params": {"mode": self._mode, "tokenList": token_list},
            }
        )

    async def _run(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    WS_URL, additional_headers=self._headers, ping_interval=None
                ) as socket:
                    self.connected = True
                    backoff = 1.0
                    logger.info("websocket feed connected")
                    await socket.send(self._subscribe_message())
                    heartbeat = asyncio.create_task(self._heartbeat(socket))
                    try:
                        async for message in socket:
                            self._on_message(message)
                    finally:
                        heartbeat.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await heartbeat
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                if not self._running:
                    return
                self.metrics.reconnects += 1
                logger.warning(
                    "websocket feed disconnected; reconnecting",
                    extra={"error": str(exc), "backoff_seconds": backoff},
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
        self.connected = False

    async def _heartbeat(self, socket) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            with contextlib.suppress(Exception):
                await socket.send("ping")

    def _on_message(self, message: str | bytes) -> None:
        if isinstance(message, str):
            return  # pong / control frames
        tick = parse_packet(message)
        if tick is None:
            self.metrics.dropped_packets += 1
            return
        self.metrics.packets += 1
        self.metrics.last_packet_at = tick["received_at"]
        self._ticks[tick["token"]] = tick
