"""Offline tests for the real breadth and sector sources (fixture-backed)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.collectors.base import CollectionError
from app.collectors.sources.broker_sectors import (
    SECTOR_TOKENS,
    BrokerSectorSource,
    window_returns,
)
from app.collectors.sources.nse_breadth import NseBreadthSource, compute_emas
from app.market.broker import Candle


def make_candles(closes: list[float], symbol: str = "X") -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Candle(
            symbol=symbol,
            interval="D",
            open=c,
            high=c,
            low=c,
            close=c,
            volume=0,
            timestamp=start + timedelta(days=i),
        )
        for i, c in enumerate(closes)
    ]


class FakeBroker:
    """Serves deterministic daily closes per token."""

    def __init__(self, closes_by_token: dict[str, list[float]]) -> None:
        self._closes = closes_by_token
        self.calls = 0

    async def get_historical(self, token, interval, start, end, exchange="NSE"):
        self.calls += 1
        closes = self._closes.get(token)
        if closes is None:
            raise RuntimeError(f"no data for {token}")
        return make_candles(closes)


# --- EMA / window return math ----------------------------------------------------


def test_compute_emas_needs_enough_history() -> None:
    assert compute_emas([100.0] * 150) is None  # fewer than 200 closes
    emas = compute_emas([100.0] * 250)
    assert emas is not None
    for key in ("ema20", "ema50", "ema100", "ema200"):
        assert emas[key] == pytest.approx(100.0)


def test_compute_emas_orders_windows_sensibly() -> None:
    # Steadily rising series: short EMA sits above long EMA.
    closes = [100.0 + i * 0.5 for i in range(300)]
    emas = compute_emas(closes)
    assert emas is not None
    assert emas["ema20"] > emas["ema50"] > emas["ema100"] > emas["ema200"]


def test_window_returns_math() -> None:
    closes = [100.0] * 30
    closes[-21] = 100.0
    closes[-6] = 104.0
    closes[-2] = 105.0
    closes[-1] = 110.0
    metrics = window_returns(closes)
    assert metrics is not None
    assert metrics["return_1d"] == pytest.approx((110 / 105 - 1) * 100)
    assert metrics["return_5d"] == pytest.approx((110 / 104 - 1) * 100)
    assert metrics["return_20d"] == pytest.approx((110 / 100 - 1) * 100)
    assert metrics["volume_ratio"] == 1.0


def test_window_returns_requires_history() -> None:
    assert window_returns([100.0] * 10) is None


# --- BrokerSectorSource -----------------------------------------------------------


async def test_sector_source_builds_full_payload() -> None:
    closes = {token: [100.0 + i for i in range(40)] for token, _ in SECTOR_TOKENS.values()}
    closes["99926000"] = [200.0 + i for i in range(40)]  # benchmark
    source = BrokerSectorSource(broker=FakeBroker(closes), cache=False)
    payload = await source.fetch_sectors()
    assert set(payload["sectors"]) == set(SECTOR_TOKENS)
    assert payload["benchmark"]["return_1d"] > 0
    # Cached on second call — no extra broker requests
    broker_calls = source._broker.calls
    await source.fetch_sectors()
    assert source._broker.calls == broker_calls


async def test_sector_source_fails_loudly_when_index_missing() -> None:
    class DeadYahoo:
        async def fetch_daily(self, ticker: str, lookback: str = "5y"):
            raise RuntimeError("yahoo unavailable")

    closes = {"99926000": [200.0 + i for i in range(40)]}  # benchmark only
    source = BrokerSectorSource(broker=FakeBroker(closes), cache=False, yahoo=DeadYahoo())
    with pytest.raises(CollectionError, match="sector history unavailable"):
        await source.fetch_sectors()


# --- NseBreadthSource -------------------------------------------------------------


class FakeNseSession:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def get_json(self, path: str) -> dict:
        return self._payload

    async def close(self) -> None:
        pass


class FakeInstruments:
    def resolve(self, symbol: str, exchange: str = "NSE"):
        if symbol == "UNKNOWN":
            raise KeyError(symbol)
        return (f"tok-{symbol}", "NSE", f"{symbol}-EQ")


NSE_INDEX_PAYLOAD = {
    "data": [
        {"symbol": "NIFTY 50", "priority": 1, "lastPrice": 24270.85, "previousClose": 24150.0},
        {
            "symbol": "RELIANCE",
            "lastPrice": 2900.0,
            "previousClose": 2850.0,
            "yearHigh": 3000.0,
            "yearLow": 2200.0,
            "totalTradedVolume": 5_000_000,
            "ffmc": 1_500_000.0,
        },
        {
            "symbol": "UNKNOWN",  # not resolvable -> excluded from universe
            "lastPrice": 100.0,
            "previousClose": 99.0,
            "yearHigh": 120.0,
            "yearLow": 80.0,
            "totalTradedVolume": 1000,
            "ffmc": 10.0,
        },
    ]
}


async def test_breadth_source_builds_rows_with_emas() -> None:
    broker = FakeBroker({"tok-RELIANCE": [2500.0 + i for i in range(300)]})
    source = NseBreadthSource(
        session=FakeNseSession(NSE_INDEX_PAYLOAD),
        broker=broker,
        instruments=FakeInstruments(),
        cache=False,
    )
    rows = await source.fetch_universe()
    assert len(rows) == 1  # index row skipped, UNKNOWN skipped
    row = rows[0]
    assert row["symbol"] == "RELIANCE"
    assert row["last"] == 2900.0
    assert row["high_252"] == 3000.0
    for key in ("ema20", "ema50", "ema100", "ema200"):
        assert key in row

    # EMA cache: second fetch does not re-hit the broker
    calls = broker.calls
    await source.fetch_universe()
    assert broker.calls == calls


async def test_breadth_source_fails_when_no_constituents() -> None:
    source = NseBreadthSource(
        session=FakeNseSession({"data": []}),
        broker=FakeBroker({}),
        instruments=FakeInstruments(),
        cache=False,
    )
    with pytest.raises(CollectionError, match="no constituents"):
        await source.fetch_universe()
