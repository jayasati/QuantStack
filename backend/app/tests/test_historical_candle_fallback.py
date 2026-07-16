"""Orchestration tests for HistoricalCandleCollector._fetch_with_fallback --
source ordering, today-only vs backfill dispatch, and fallback-on-empty vs
fallback-on-exception, using fake sources injected through the DI-friendly
constructor (DEBT-2, 2026-07-15)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.collectors.market_data import IST, HistoricalCandleCollector
from app.market.broker import BrokerInterface, Candle, Quote

TODAY = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)  # 15:30 IST


def _candle(symbol: str, interval: str, close: float, ts: datetime = TODAY) -> Candle:
    return Candle(
        symbol=symbol, interval=interval, open=close, high=close, low=close,
        close=close, volume=0, timestamp=ts,
    )


class FakeBroker(BrokerInterface):
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result if result is not None else []
        self.error = error
        self.calls = 0

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def is_connected(self) -> bool:
        return True

    async def get_quote(self, symbol: str, exchange: str = "NSE") -> Quote:
        raise NotImplementedError

    async def get_historical(self, symbol, interval, start, end, exchange="NSE"):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class FakeSource:
    """Stands in for NseCandleSource / BseCandleSource / YahooDailyHistory --
    all three expose the same shape from the collector's point of view."""

    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result if result is not None else []
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def fetch_today(self, symbol: str, interval: str):
        self.calls.append((symbol, interval))
        if self.error is not None:
            raise self.error
        return self.result

    async def fetch_intraday(self, symbol: str, interval: str, range_: str = "1d"):
        return await self.fetch_today(symbol, interval)

    async def close(self) -> None:
        pass


def make_collector(broker=None, nse=None, bse=None, yahoo=None) -> HistoricalCandleCollector:
    collector = HistoricalCandleCollector(
        broker=broker or FakeBroker(), nse_candles=nse, bse_candles=bse, yahoo=yahoo,
    )
    collector._sessions = lambda: None  # type: ignore[method-assign]
    return collector


async def test_today_only_intraday_tries_broker_first_not_nse(monkeypatch=None) -> None:
    """2026-07-16: NSE/BSE demoted to last-resort now that
    TickCandleAggregator builds today's intraday candles live from ticks --
    NSE's index endpoint also turned out to forward-pad the rest of the
    session with placeholder bars when it was still tried first."""
    nse = FakeSource(result=[_candle("HDFCBANK", "1m", 1500.0)])
    broker = FakeBroker(result=[_candle("HDFCBANK", "1m", 1400.0)])
    collector = make_collector(broker=broker, nse=nse)
    candles = await collector._fetch_with_fallback(
        "HDFCBANK", "1m", TODAY - timedelta(minutes=5), TODAY, "tok", "NSE", TODAY.astimezone(IST).date(),
    )
    assert candles[0].close == 1400.0
    assert broker.calls == 1
    assert nse.calls == []  # broker succeeded, NSE (last resort) never tried


async def test_today_only_intraday_falls_through_to_nse_only_as_last_resort() -> None:
    nse = FakeSource(result=[_candle("HDFCBANK", "1m", 1500.0)])
    broker = FakeBroker(error=RuntimeError("broker down"))
    yahoo = FakeSource(error=RuntimeError("yahoo down"))
    collector = make_collector(broker=broker, nse=nse, yahoo=yahoo)
    candles = await collector._fetch_with_fallback(
        "HDFCBANK", "1m", TODAY - timedelta(minutes=5), TODAY, "tok", "NSE", TODAY.astimezone(IST).date(),
    )
    assert candles[0].close == 1500.0
    assert nse.calls == [("HDFCBANK", "1m")]


async def test_today_only_intraday_tries_bse_last_for_a_bse_listed_symbol() -> None:
    bse = FakeSource(result=[_candle("SENSEX", "1m", 77000.0)])
    broker = FakeBroker(error=RuntimeError("broker down"))
    nse = FakeSource(result=[_candle("SENSEX", "1m", 1.0)])
    collector = make_collector(broker=broker, nse=nse, bse=bse)
    candles = await collector._fetch_with_fallback(
        "SENSEX", "1m", TODAY - timedelta(minutes=5), TODAY, "tok", "BSE", TODAY.astimezone(IST).date(),
    )
    assert candles[0].close == 77000.0
    assert bse.calls == [("SENSEX", "1m")]
    assert nse.calls == []  # SENSEX is BSE-listed, NSE is never even tried


async def test_falls_through_to_yahoo_when_broker_raises() -> None:
    broker = FakeBroker(error=RuntimeError("broker down"))
    yahoo = FakeSource(result=[_candle("HDFCBANK", "1m", 1600.0)])
    collector = make_collector(broker=broker, yahoo=yahoo)
    candles = await collector._fetch_with_fallback(
        "HDFCBANK", "1m", TODAY - timedelta(minutes=5), TODAY, "tok", "NSE", TODAY.astimezone(IST).date(),
    )
    assert candles[0].close == 1600.0


async def test_falls_through_to_yahoo_when_broker_returns_empty_not_just_on_error() -> None:
    """The exact DEBT-2 failure mode: a source that returns zero bars
    without raising must not be treated as success."""
    broker = FakeBroker(result=[])
    yahoo = FakeSource(result=[_candle("HDFCBANK", "1m", 1600.0)])
    collector = make_collector(broker=broker, yahoo=yahoo)
    candles = await collector._fetch_with_fallback(
        "HDFCBANK", "1m", TODAY - timedelta(minutes=5), TODAY, "tok", "NSE", TODAY.astimezone(IST).date(),
    )
    assert candles[0].close == 1600.0


async def test_falls_through_to_nse_when_broker_and_yahoo_both_fail() -> None:
    nse = FakeSource(result=[_candle("HDFCBANK", "5m", 1500.0)])
    broker = FakeBroker(error=RuntimeError("broker down"))
    yahoo = FakeSource(error=RuntimeError("yahoo down"))
    collector = make_collector(broker=broker, nse=nse, yahoo=yahoo)
    candles = await collector._fetch_with_fallback(
        "HDFCBANK", "5m", TODAY - timedelta(minutes=5), TODAY, "tok", "NSE", TODAY.astimezone(IST).date(),
    )
    assert candles[0].close == 1500.0


async def test_all_sources_failing_returns_empty_list_not_an_exception() -> None:
    nse = FakeSource(error=RuntimeError("nse down"))
    broker = FakeBroker(error=RuntimeError("broker down"))
    yahoo = FakeSource(error=RuntimeError("yahoo down"))
    collector = make_collector(broker=broker, nse=nse, yahoo=yahoo)
    candles = await collector._fetch_with_fallback(
        "HDFCBANK", "5m", TODAY - timedelta(minutes=5), TODAY, "tok", "NSE", TODAY.astimezone(IST).date(),
    )
    assert candles == []


async def test_multi_day_backfill_skips_nse_and_bse_entirely() -> None:
    """NSE/BSE only ever expose today's session -- a window spanning
    multiple days must go straight to broker-then-Yahoo."""
    nse = FakeSource(result=[_candle("HDFCBANK", "1m", 1500.0)])
    broker = FakeBroker(result=[_candle("HDFCBANK", "1m", 1400.0)])
    collector = make_collector(broker=broker, nse=nse)
    candles = await collector._fetch_with_fallback(
        "HDFCBANK", "1m", TODAY - timedelta(days=2), TODAY, "tok", "NSE", TODAY.astimezone(IST).date(),
    )
    assert candles[0].close == 1400.0
    assert nse.calls == []


async def test_daily_interval_skips_nse_bse_and_yahoo_matching_original_broker_only_behavior() -> None:
    nse = FakeSource(result=[_candle("HDFCBANK", "D", 1500.0)])
    yahoo = FakeSource(result=[_candle("HDFCBANK", "D", 1600.0)])
    broker = FakeBroker(result=[])
    collector = make_collector(broker=broker, nse=nse, yahoo=yahoo)
    candles = await collector._fetch_with_fallback(
        "HDFCBANK", "D", TODAY - timedelta(minutes=5), TODAY, "tok", "NSE", TODAY.astimezone(IST).date(),
    )
    assert candles == []
    assert nse.calls == []
    assert yahoo.calls == []
    assert broker.calls == 1


async def test_cleanup_closes_every_constructed_source() -> None:
    nse, bse, yahoo = FakeSource(), FakeSource(), FakeSource()
    closed = []
    for name, source in (("nse", nse), ("bse", bse), ("yahoo", yahoo)):
        async def close(name=name):
            closed.append(name)
        source.close = close
    collector = make_collector(nse=nse, bse=bse, yahoo=yahoo)
    await collector.cleanup()
    assert set(closed) == {"nse", "bse", "yahoo"}


async def test_drops_candles_timestamped_after_the_fetch_end() -> None:
    """Found live 2026-07-16: NSE's index chart endpoint (getGraphChart,
    NIFTY/BANKNIFTY only) forward-pads its response with placeholder bars
    for the rest of the session, not just what's actually traded so far --
    no real source should ever legitimately report the future."""
    future_ts = TODAY + timedelta(hours=4)
    nse = FakeSource(result=[
        _candle("NIFTY", "5m", 24100.0, ts=TODAY - timedelta(minutes=5)),
        _candle("NIFTY", "5m", 24999.0, ts=future_ts),  # bogus forward-padded bar
    ])
    broker = FakeBroker(error=RuntimeError("broker down"))
    yahoo = FakeSource(error=RuntimeError("yahoo down"))
    collector = make_collector(broker=broker, nse=nse, yahoo=yahoo)
    candles = await collector._fetch_with_fallback(
        "NIFTY", "5m", TODAY - timedelta(minutes=5), TODAY, "tok", "NSE", TODAY.astimezone(IST).date(),
    )
    assert len(candles) == 1
    assert candles[0].close == 24100.0


async def test_all_candles_in_the_future_counts_as_no_bars_and_falls_through() -> None:
    future_ts = TODAY + timedelta(hours=4)
    broker = FakeBroker(result=[_candle("NIFTY", "5m", 24999.0, ts=future_ts)])
    yahoo = FakeSource(result=[_candle("NIFTY", "5m", 24100.0)])
    collector = make_collector(broker=broker, yahoo=yahoo)
    candles = await collector._fetch_with_fallback(
        "NIFTY", "5m", TODAY - timedelta(minutes=5), TODAY, "tok", "NSE", TODAY.astimezone(IST).date(),
    )
    assert candles[0].close == 24100.0
