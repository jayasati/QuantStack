"""Angel One SmartAPI adapter (skeleton).

Volume 1 wires the adapter through the broker abstraction and dependency
injection only. The real SmartAPI integration (auth, quotes, historical OHLC,
rate limiting, retry/backoff) is implemented in Volume 2.
"""

from datetime import datetime

from app.core.config import Settings
from app.core.logging import get_logger
from app.market.broker import BrokerInterface, Candle, Quote

logger = get_logger(__name__)


class AngelOneAdapter(BrokerInterface):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._connected = False

    async def connect(self) -> None:
        if not self._settings.angel_one_api_key:
            logger.warning("angel one credentials not configured; adapter runs disconnected")
            return
        # Volume 2: SmartAPI session establishment goes here.
        self._connected = True
        logger.info("angel one adapter connected")

    async def disconnect(self) -> None:
        self._connected = False

    async def is_connected(self) -> bool:
        return self._connected

    async def get_quote(self, symbol: str, exchange: str = "NSE") -> Quote:
        raise NotImplementedError("Implemented in Volume 2 (Data Collection Layer)")

    async def get_historical(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        exchange: str = "NSE",
    ) -> list[Candle]:
        raise NotImplementedError("Implemented in Volume 2 (Data Collection Layer)")
