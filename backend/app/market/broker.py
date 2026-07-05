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
    """Contract every broker adapter must implement."""

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
