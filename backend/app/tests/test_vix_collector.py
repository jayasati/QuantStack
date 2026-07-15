import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.collectors.base import BaseCollector, CollectorPipeline
from app.collectors.market_data import VixCollector
from app.collectors.registry import CollectorRegistry
from app.collectors.sources.yahoo_history import IST, YahooDailyHistory
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


def make_yahoo(sessions_utc: list[datetime], closes: list[float]) -> YahooDailyHistory:
    """Yahoo chart stub returning one daily bar per session-open timestamp."""
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [int(ts.timestamp()) for ts in sessions_utc],
                    "indicators": {
                        "quote": [
                            {
                                "open": [c - 0.2 for c in closes],
                                "high": [c + 0.5 for c in closes],
                                "low": [c - 0.5 for c in closes],
                                "close": closes,
                                "volume": [None] * len(closes),
                            }
                        ]
                    },
                }
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "%5EINDIAVIX" in str(request.url) or "^INDIAVIX" in str(request.url)
        return httpx.Response(200, text=json.dumps(payload))

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://query1.finance.yahoo.com",
    )
    return YahooDailyHistory(client=client)


async def test_yahoo_daily_bars_align_to_broker_timestamp_convention() -> None:
    # Yahoo stamps the bar at the 09:15 IST session open (03:45 UTC).
    session_open = datetime(2026, 7, 3, 3, 45, tzinfo=UTC)
    yahoo = make_yahoo([session_open], [13.4])
    candles = await yahoo.fetch_daily("INDIAVIX")
    assert len(candles) == 1
    # Normalized to midnight IST of the session date = 18:30 UTC the day before,
    # matching Angel One's daily-bar timestamps so feature joins line up.
    assert candles[0].timestamp == datetime(2026, 7, 3, 0, 0, tzinfo=IST)
    assert candles[0].timestamp.astimezone(UTC) == datetime(2026, 7, 2, 18, 30, tzinfo=UTC)
    assert candles[0].interval == "D"
    assert candles[0].close == 13.4
    assert candles[0].volume == 0


async def test_deep_backfill_runs_while_daily_history_thin() -> None:
    collector = VixCollector(
        broker=FakeBroker(),
        yahoo=make_yahoo(
            [datetime(2026, 7, d, 3, 45, tzinfo=UTC) for d in (1, 2, 3)],
            [13.0, 13.5, 14.0],
        ),
    )
    stored_rows: list[Candle] = []

    async def fake_count(symbol: str) -> int:
        return 2  # far below DAILY_BACKFILL_TARGET

    async def fake_store(symbol: str, candles: list[Candle]) -> int:
        stored_rows.extend(candles)
        return len(candles)

    collector._sessions = lambda: None  # type: ignore[method-assign]  # keep the test off the real DB
    collector._daily_bar_count = fake_count  # type: ignore[method-assign]
    collector._store_candles = fake_store  # type: ignore[method-assign]
    await collector.initialize()
    records = await collector.collect()

    yahoo_records = [r for r in records if r.source == "yahoo"]
    assert len(yahoo_records) == 1
    assert yahoo_records[0].metadata["provider"] == "yahoo_deep_backfill"
    assert yahoo_records[0].metadata["bars_stored"] == 3
    # _store_candles also captured the broker bars; check the yahoo daily ones.
    daily = [c for c in stored_rows if c.interval == "D" and c.close in (13.0, 13.5, 14.0)]
    assert len(daily) == 3
    # Broker records for all timeframes still present alongside the backfill.
    assert len(records) == len(collector.intervals) + 1


async def test_deep_backfill_dormant_once_history_is_deep() -> None:
    collector = VixCollector(broker=FakeBroker())

    async def fake_count(symbol: str) -> int:
        return VixCollector.DAILY_BACKFILL_TARGET

    collector._sessions = lambda: None  # type: ignore[method-assign]  # keep the test off the real DB
    collector._daily_bar_count = fake_count  # type: ignore[method-assign]
    await collector.initialize()
    records = await collector.collect()
    assert all(r.source != "yahoo" for r in records)


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


def test_historical_candle_family_is_market_hours_only() -> None:
    # No new intraday bars form once the market's shut; inherited by
    # VixCollector and ReferenceIndexCollector too (see
    # test_collector_framework.py for the shared gate mechanics).
    from app.collectors.market_data import HistoricalCandleCollector, ReferenceIndexCollector

    assert HistoricalCandleCollector.market_hours_only is True
    assert VixCollector.market_hours_only is True
    assert ReferenceIndexCollector.market_hours_only is True
