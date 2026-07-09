"""Broker abstraction layer.

Business logic never knows which broker is used. All broker access goes
through this interface; concrete adapters (Angel One today, Zerodha or
Interactive Brokers later) implement it. Only the adapter may know
SDK/HTTP specifics.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class BrokerError(Exception):
    """Structured broker failure — the only exception adapters may raise."""


@dataclass(frozen=True)
class Quote:
    symbol: str
    exchange: str
    last_price: float
    timestamp: datetime
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int = 0
    vwap: float | None = None
    bid: float | None = None
    ask: float | None = None
    bid_qty: int = 0
    ask_qty: int = 0
    depth: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Candle:
    symbol: str
    interval: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    timestamp: datetime


class BrokerInterface(ABC):
    """Contract every broker adapter must implement.

    ``get_option_greeks`` and ``get_market_depth`` are concrete (not
    abstract): not every broker exposes per-strike Greeks or a full depth
    ladder, so callers get a documented, polymorphic method instead of
    duck-typing via ``getattr(broker, "get_option_greeks", None)``, but a new
    adapter isn't forced to implement capabilities it doesn't have. The
    multi-strike option *chain* (OI/volume/LTP across all strikes) is
    deliberately not part of this interface — that data is public NSE
    market data, not broker-specific, and is modeled as an injectable
    ``OptionsChainSource`` instead (see ``app.collectors.domains.options``).
    """

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def is_connected(self) -> bool: ...

    @abstractmethod
    async def get_quote(self, symbol: str, exchange: str = "NSE") -> Quote: ...

    @abstractmethod
    async def get_historical(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        exchange: str = "NSE",
    ) -> list[Candle]: ...

    async def get_option_greeks(
        self, name: str, expiry: str
    ) -> dict[tuple[float, str], dict[str, float]]:
        """Per-strike Greeks keyed by (strike, "CE"/"PE"). Empty if unsupported."""
        return {}

    async def get_market_depth(self, symbol: str, exchange: str = "NSE") -> dict[str, Any]:
        """Order book (bid/ask ladder) for ``symbol``. Empty if unsupported.

        Default implementation reuses ``get_quote``'s depth field so adapters
        that only expose depth as part of the quote payload (Angel One) don't
        need a second HTTP round trip.
        """
        quote = await self.get_quote(symbol, exchange)
        return quote.depth
