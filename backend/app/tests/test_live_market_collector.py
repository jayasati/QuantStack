"""Tests for LiveMarketCollector, including the India VIX live-tick addition."""

from datetime import UTC, datetime

from app.collectors.market_data import LiveMarketCollector
from app.market.broker import BrokerInterface, Candle, Quote


class FakeBroker(BrokerInterface):
    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = prices
        self.quoted_tokens: list[str] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def is_connected(self) -> bool:
        return True

    async def get_quote(self, symbol: str, exchange: str = "NSE") -> Quote:
        self.quoted_tokens.append(symbol)
        return Quote(
            symbol=symbol,
            exchange=exchange,
            last_price=self.prices[symbol],
            timestamp=datetime.now(UTC),
            close=self.prices[symbol] - 1.0,
        )

    async def get_historical(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        exchange: str = "NSE",
    ) -> list[Candle]:
        raise NotImplementedError


async def test_symbols_include_india_vix_alongside_the_watchlist() -> None:
    from app.core.config import get_settings

    collector = LiveMarketCollector(broker=FakeBroker({}))
    assert collector.symbols == [*get_settings().watchlist, "INDIAVIX"]


async def test_india_vix_is_not_duplicated_if_already_in_watchlist(monkeypatch) -> None:
    class FakeSettings:
        watchlist = ["NIFTY", "INDIAVIX"]
        feature_vix_symbol = "INDIAVIX"

    monkeypatch.setattr(
        "app.collectors.market_data.get_settings", lambda: FakeSettings()
    )
    collector = LiveMarketCollector(broker=FakeBroker({}))
    assert collector.symbols == ["NIFTY", "INDIAVIX"]


async def test_collect_emits_a_live_quote_for_india_vix() -> None:
    # get_quote() is called with the resolved broker TOKEN, not the friendly
    # symbol — NIFTY/BANKNIFTY/INDIAVIX are stable index tokens from
    # instruments.INDEX_TOKENS, resolved without hitting the scrip master.
    broker = FakeBroker({"99926000": 24100.0, "99926009": 56700.0, "99926017": 14.68})
    collector = LiveMarketCollector(broker=broker)
    collector.symbols = ["NIFTY", "BANKNIFTY", "INDIAVIX"]
    collector._sessions = lambda: None  # type: ignore[method-assign]  # keep the test off the real DB
    await collector.initialize()
    records = await collector.collect()

    by_instrument = {r.instrument: r for r in records}
    assert by_instrument["INDIAVIX"].raw_value == 14.68
    assert by_instrument["INDIAVIX"].exchange == "NSE"
    assert by_instrument["INDIAVIX"].metadata["trading_symbol"] == "India VIX"
    # Same 15s live-tick path as every tradable symbol — no special casing.
    assert "99926017" in broker.quoted_tokens


def test_collector_is_market_hours_only() -> None:
    # Quotes/LTP freeze the instant the market closes -- polling every 15s
    # regardless would be pointless (see test_collector_framework.py for the
    # shared market_hours_only gate mechanics).
    assert LiveMarketCollector.market_hours_only is True


class FakeTickAggregator:
    def __init__(self) -> None:
        self.batches: list[list[dict]] = []

    async def ingest_batch(self, ticks: list[dict]) -> None:
        self.batches.append(ticks)


async def test_collect_feeds_every_tick_to_the_live_candle_aggregator() -> None:
    """The whole point of the 2026-07-16 real-time aggregation layer:
    every tick this collector already gathers each 15s cycle must reach
    the aggregator, not just get persisted to raw_ticks."""
    broker = FakeBroker({"99926000": 24100.0, "99926009": 56700.0, "99926017": 14.68})
    aggregator = FakeTickAggregator()
    collector = LiveMarketCollector(broker=broker, tick_aggregator=aggregator)
    collector.symbols = ["NIFTY", "BANKNIFTY", "INDIAVIX"]
    collector._sessions = lambda: None  # type: ignore[method-assign]  # keep the test off the real DB
    await collector.initialize()
    await collector.collect()

    assert len(aggregator.batches) == 1
    fed_symbols = {tick["symbol"] for tick in aggregator.batches[0]}
    assert fed_symbols == {"NIFTY", "BANKNIFTY", "INDIAVIX"}
    vix_tick = next(t for t in aggregator.batches[0] if t["symbol"] == "INDIAVIX")
    assert vix_tick["ltp"] == 14.68
