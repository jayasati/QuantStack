from datetime import UTC, datetime, timedelta

import pytest

from app.collectors.base import BaseCollector, CollectorPipeline
from app.collectors.market_data import VixCollector
from app.collectors.registry import CollectorRegistry
from app.market.broker import BrokerInterface, Candle, Quote


class FakeBroker(BrokerInterface):
    def __init__(self) -> None:
        self.historical_calls: list[tuple[str, str]] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def is_connected(self) -> bool:
        return True

    async def get_quote(self, symbol: str, exchange: str = "NSE") -> Quote:
        raise NotImplementedError

    async def get_historical(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        exchange: str = "NSE",
    ) -> list[Candle]:
        self.historical_calls.append((symbol, interval))
        base = datetime(2026, 7, 1, tzinfo=UTC)
        return [
            Candle(
                symbol=symbol,
                interval=interval,
                open=14.0 + i * 0.1,
                high=14.5 + i * 0.1,
                low=13.5 + i * 0.1,
                close=14.2 + i * 0.1,
                volume=0,  # indices carry no volume
                timestamp=base + timedelta(days=i),
            )
            for i in range(3)
        ]


async def test_vix_collector_targets_configured_vix_symbol() -> None:
    collector = VixCollector(broker=FakeBroker())
    assert collector.symbols == ["INDIAVIX"]
    await collector.initialize()
    # INDIAVIX is a stable NSE index token, resolved without the scrip master.
    token, exchange, trading_symbol = collector._tokens["INDIAVIX"]
    assert token == "99926017"
    assert exchange == "NSE"
    assert trading_symbol == "India VIX"


async def test_vix_collector_fetches_all_timeframes() -> None:
    broker = FakeBroker()
    collector = VixCollector(broker=broker)
    collector._sessions = lambda: None  # type: ignore[method-assign]  # keep the test off the real DB
    await collector.initialize()
    records = await collector.collect()
    assert len(records) == len(collector.intervals)
    assert all(r.instrument == "INDIAVIX" for r in records)
    assert {interval for _, interval in broker.historical_calls} == set(collector.intervals)
    for record in records:
        assert record.metadata["bars_fetched"] == 3
        assert record.normalized_value == pytest.approx(14.4)  # last close


async def test_registry_discovers_vix_collector() -> None:
    class NullPipeline(CollectorPipeline):
        async def process(
            self, collector: BaseCollector, records: list, latency_ms: float
        ) -> list:
            return records

        async def record_failure(self, collector: BaseCollector, error: Exception) -> None:
            pass

    registry = CollectorRegistry(NullPipeline())
    registry.discover(["app.collectors.market_data"])
    names = {c["name"] for c in registry.list_collectors()}
    assert "vix" in names
