from app.core.config import Settings
from app.core.container import Container
from app.market.angel_one import AngelOneAdapter
from app.market.broker import BrokerInterface


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
