"""Angel One SmartAPI adapter tests with fully mocked broker responses."""

import json
from datetime import datetime

import httpx
import pytest

from app.core.circuit_breaker import CircuitBreaker, CircuitState
from app.core.config import Settings
from app.market.angel_one import CANDLE_PATH, LOGIN_PATH, QUOTE_PATH, AngelOneAdapter
from app.market.broker import BrokerError

SETTINGS = Settings(
    angel_one_api_key="test-key",
    angel_one_client_id="C123",
    angel_one_pin="0000",
    angel_one_totp_secret="JBSWY3DPEHPK3PXP",
)


def make_adapter(handler) -> AngelOneAdapter:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://test")
    return AngelOneAdapter(SETTINGS, client=client, max_retries=1)


async def test_login_stores_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == LOGIN_PATH
        body = json.loads(request.content)
        assert body["clientcode"] == "C123"
        assert len(body["totp"]) == 6
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {"jwtToken": "jwt-1", "refreshToken": "r-1", "feedToken": "f-1"},
            },
        )

    adapter = make_adapter(handler)
    await adapter.connect()
    assert await adapter.is_connected()


async def test_quote_parses_full_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == LOGIN_PATH:
            return httpx.Response(
                200, json={"status": True, "data": {"jwtToken": "jwt", "refreshToken": "r"}}
            )
        assert request.url.path == QUOTE_PATH
        assert request.headers["Authorization"] == "Bearer jwt"
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {
                    "fetched": [
                        {
                            "tradingSymbol": "NIFTY",
                            "ltp": 25000.5,
                            "open": 24900.0,
                            "high": 25100.0,
                            "low": 24850.0,
                            "close": 24950.0,
                            "tradeVolume": 123456,
                            "avgPrice": 24990.0,
                            "depth": {
                                "buy": [{"price": 25000.0, "quantity": 50}],
                                "sell": [{"price": 25001.0, "quantity": 75}],
                            },
                        }
                    ]
                },
            },
        )

    adapter = make_adapter(handler)
    await adapter.connect()
    quote = await adapter.get_quote("NIFTY")
    assert quote.last_price == 25000.5
    assert quote.bid == 25000.0
    assert quote.ask == 25001.0
    assert quote.vwap == 24990.0
    assert quote.volume == 123456


async def test_candles_parse_and_map_interval() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == LOGIN_PATH:
            return httpx.Response(
                200, json={"status": True, "data": {"jwtToken": "jwt", "refreshToken": "r"}}
            )
        assert request.url.path == CANDLE_PATH
        body = json.loads(request.content)
        assert body["interval"] == "FIVE_MINUTE"
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": [
                    ["2026-07-03T09:15:00+05:30", 100.0, 101.0, 99.5, 100.5, 1000],
                    ["2026-07-03T09:20:00+05:30", 100.5, 102.0, 100.0, 101.5, 1500],
                ],
            },
        )

    adapter = make_adapter(handler)
    await adapter.connect()
    candles = await adapter.get_historical(
        "NIFTY", "5m", datetime(2026, 7, 3, 9), datetime(2026, 7, 3, 16)
    )
    assert len(candles) == 2
    assert candles[0].open == 100.0
    assert candles[1].close == 101.5


async def test_api_error_raises_broker_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": False, "errorcode": "AB1004", "message": "invalid totp"}
        )

    adapter = make_adapter(handler)
    with pytest.raises(BrokerError, match="AB1004"):
        await adapter.connect()


async def test_retry_then_success() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500)
        return httpx.Response(
            200, json={"status": True, "data": {"jwtToken": "jwt", "refreshToken": "r"}}
        )

    adapter = make_adapter(handler)
    await adapter.connect()
    assert calls["n"] == 2
    assert await adapter.is_connected()


async def test_unsupported_interval_rejected() -> None:
    adapter = make_adapter(lambda request: httpx.Response(200, json={"status": True}))
    with pytest.raises(BrokerError, match="unsupported interval"):
        await adapter.get_historical("NIFTY", "2m", datetime(2026, 1, 1), datetime(2026, 1, 2))


async def test_market_depth_defaults_to_quote_depth() -> None:
    """BrokerInterface.get_market_depth's default reuses get_quote's depth
    field so adapters that only expose it there (Angel One) need no override."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == LOGIN_PATH:
            return httpx.Response(
                200, json={"status": True, "data": {"jwtToken": "jwt", "refreshToken": "r"}}
            )
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {
                    "fetched": [
                        {
                            "tradingSymbol": "NIFTY",
                            "ltp": 25000.5,
                            "depth": {
                                "buy": [{"price": 25000.0, "quantity": 50}],
                                "sell": [{"price": 25001.0, "quantity": 75}],
                            },
                        }
                    ]
                },
            },
        )

    adapter = make_adapter(handler)
    await adapter.connect()
    depth = await adapter.get_market_depth("NIFTY")
    assert depth["buy"][0]["price"] == 25000.0
    assert depth["sell"][0]["price"] == 25001.0


async def test_repeated_network_failures_open_circuit_breaker() -> None:
    """Chapter 13: Retry -> Backoff exhausted enough times trips the breaker,
    and further calls fail fast without hitting the network at all."""
    calls = {"n": 0}

    def always_down(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    breaker = CircuitBreaker(name="broker.angel_one", failure_threshold=2)
    transport = httpx.MockTransport(always_down)
    client = httpx.AsyncClient(transport=transport, base_url="https://test")
    adapter = AngelOneAdapter(SETTINGS, client=client, max_retries=0, circuit_breaker=breaker)

    # First failure: retries exhausted, breaker still closed (below threshold).
    with pytest.raises(BrokerError, match="failed after retries"):
        await adapter.connect()
    assert breaker.state == CircuitState.CLOSED
    assert calls["n"] == 1

    # Second failure hits the threshold and trips the breaker.
    with pytest.raises(BrokerError, match="failed after retries"):
        await adapter.connect()
    assert breaker.state == CircuitState.OPEN
    assert calls["n"] == 2

    # Third call fails fast: no additional network request at all.
    with pytest.raises(BrokerError, match="circuit breaker open"):
        await adapter.connect()
    assert calls["n"] == 2
