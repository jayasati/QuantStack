"""Tests for YahooDailyHistory.fetch_intraday -- the deepest fallback rung
in HistoricalCandleCollector's NSE -> BSE -> Angel One -> Yahoo chain."""

import json
from datetime import UTC, datetime

import httpx

from app.collectors.sources.yahoo_history import IST, YahooDailyHistory


def make_intraday_yahoo(bar_epochs_utc: list[datetime], closes: list[float]) -> tuple[YahooDailyHistory, list[httpx.Request]]:
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [int(ts.timestamp()) for ts in bar_epochs_utc],
                    "indicators": {
                        "quote": [
                            {
                                "open": [c - 0.1 for c in closes],
                                "high": [c + 0.2 for c in closes],
                                "low": [c - 0.2 for c in closes],
                                "close": closes,
                                "volume": [1000] * len(closes),
                            }
                        ]
                    },
                }
            ]
        }
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=json.dumps(payload))

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://query1.finance.yahoo.com",
    )
    return YahooDailyHistory(client=client), requests


async def test_fetch_intraday_keeps_each_bars_own_timestamp_not_session_midnight() -> None:
    bar1 = datetime(2026, 7, 15, 3, 45, tzinfo=UTC)  # 09:15 IST
    bar2 = datetime(2026, 7, 15, 3, 50, tzinfo=UTC)  # 09:20 IST
    yahoo, _ = make_intraday_yahoo([bar1, bar2], [1500.0, 1502.0])
    candles = await yahoo.fetch_intraday("HDFCBANK", "5m")
    assert len(candles) == 2
    assert candles[0].timestamp == bar1.astimezone(IST)
    assert candles[1].timestamp == bar2.astimezone(IST)
    assert candles[0].interval == "5m"
    assert candles[0].close == 1500.0
    assert candles[0].volume == 1000


async def test_fetch_intraday_maps_1h_to_yahoos_60m_interval_string() -> None:
    yahoo, requests = make_intraday_yahoo([datetime(2026, 7, 15, 3, 45, tzinfo=UTC)], [100.0])
    await yahoo.fetch_intraday("NIFTY", "1H")
    assert requests[0].url.params["interval"] == "60m"


async def test_fetch_intraday_passes_other_intervals_through_unchanged() -> None:
    yahoo, requests = make_intraday_yahoo([datetime(2026, 7, 15, 3, 45, tzinfo=UTC)], [100.0])
    await yahoo.fetch_intraday("NIFTY", "5m")
    assert requests[0].url.params["interval"] == "5m"


async def test_fetch_intraday_drops_bars_with_missing_ohlc() -> None:
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1, 2],
                    "indicators": {
                        "quote": [
                            {
                                "open": [100.0, None],
                                "high": [101.0, None],
                                "low": [99.0, None],
                                "close": [100.5, None],
                                "volume": [10, 10],
                            }
                        ]
                    },
                }
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps(payload))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://query1.finance.yahoo.com")
    yahoo = YahooDailyHistory(client=client)
    candles = await yahoo.fetch_intraday("HDFCBANK", "5m")
    assert len(candles) == 1
    assert candles[0].close == 100.5


async def test_fetch_intraday_returns_empty_when_result_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps({"chart": {"result": []}}))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://query1.finance.yahoo.com")
    yahoo = YahooDailyHistory(client=client)
    assert await yahoo.fetch_intraday("HDFCBANK", "5m") == []
