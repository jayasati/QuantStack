from datetime import UTC, datetime, timedelta

import pytest

from app.collectors.sources.broker_sectors import BrokerSectorSource
from app.market.broker import Candle


class DeadBroker:
    async def get_historical(self, *args, **kwargs):
        raise RuntimeError("certificate verify failed")


class FakeYahoo:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch_daily(self, ticker: str, lookback: str = "5y") -> list[Candle]:
        self.calls.append(ticker)
        base = datetime(2026, 5, 1, tzinfo=UTC)
        return [
            Candle(symbol=ticker, interval="D", open=100 + i, high=101 + i,
                   low=99 + i, close=100.0 + i, volume=0,
                   timestamp=base + timedelta(days=i))
            for i in range(30)
        ]


async def test_returns_fall_back_to_yahoo_when_broker_dead() -> None:
    yahoo = FakeYahoo()
    source = BrokerSectorSource(broker=DeadBroker(), yahoo=yahoo)
    metrics = await source._returns_for("Metal", "99926030", "NSE")
    assert metrics is not None
    assert yahoo.calls == ["^CNXMETAL"]
    # Closes 100..129: last=129, 1d ref 128, 5d ref 124, 20d ref 109.
    assert metrics["return_1d"] == pytest.approx((129 / 128 - 1) * 100)
    assert metrics["return_5d"] == pytest.approx((129 / 124 - 1) * 100)
    assert metrics["return_20d"] == pytest.approx((129 / 109 - 1) * 100)
    assert metrics["volume_ratio"] == 1.0  # neutral until NSE volume applies


async def test_unmapped_sector_returns_none_instead_of_guessing() -> None:
    source = BrokerSectorSource(broker=DeadBroker(), yahoo=FakeYahoo())
    assert await source._returns_from_yahoo("Nonexistent Sector") is None
