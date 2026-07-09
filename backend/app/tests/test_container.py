from datetime import datetime

from app.core.config import Settings
from app.core.container import Container
from app.market.angel_one import AngelOneAdapter
from app.market.broker import BrokerInterface, Candle, Quote


class MinimalBroker(BrokerInterface):
    """A broker adapter implementing only the required abstract methods,
    to confirm get_option_greeks/get_market_depth degrade gracefully."""

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def is_connected(self) -> bool:
        return True

    async def get_quote(self, symbol: str, exchange: str = "NSE") -> Quote:
        return Quote(symbol=symbol, exchange=exchange, last_price=100.0, timestamp=datetime.now())

    async def get_historical(
        self, symbol, interval, start, end, exchange="NSE"
    ) -> list[Candle]:
        return []


async def test_broker_interface_default_greeks_and_depth_degrade_gracefully() -> None:
    broker = MinimalBroker()
    assert await broker.get_option_greeks("NIFTY", "07JUL2026") == {}
    assert await broker.get_market_depth("NIFTY") == {}  # Quote.depth defaults to {}


def test_container_resolves_broker_via_interface() -> None:
    container = Container()
    container.register(BrokerInterface, lambda: AngelOneAdapter(Settings()))

    broker = container.resolve(BrokerInterface)
    assert isinstance(broker, AngelOneAdapter)
    # Singleton behaviour: same instance on second resolve
    assert container.resolve(BrokerInterface) is broker


async def test_adapter_without_credentials_stays_disconnected() -> None:
    adapter = AngelOneAdapter(Settings(angel_one_api_key=None))
    await adapter.connect()
    assert await adapter.is_connected() is False
