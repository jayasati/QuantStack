"""Broker abstraction layer.

Business logic never knows which broker is used. All broker access goes
through this interface; concrete adapters (Angel One today, Zerodha or
Interactive Brokers later) implement it.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Quote:
    symbol: str
    exchange: str
    last_price: float
    timestamp: datetime


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
