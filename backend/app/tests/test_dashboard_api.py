from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.api import dashboard as dashboard_api
from app.core.cache import CacheService
from app.core.container import container
from app.database.tables import OhlcvCandle


def make_client() -> TestClient:
    app = FastAPI()
    app.include_router(dashboard_api.router)
    return TestClient(app)


def test_intelligence_dashboard_page_serves_the_static_html() -> None:
    client = make_client()
    response = client.get("/dashboard/intelligence")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Market Intelligence Dashboard" in response.text


def test_candle_check_page_serves_the_static_html() -> None:
    client = make_client()
    response = client.get("/dashboard/candles")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Candle Fetch Check" in response.text


def test_candle_check_symbols_returns_the_configured_watchlist() -> None:
    from app.core.config import get_settings

    client = make_client()
    response = client.get("/dashboard/candles/symbols")
    assert response.status_code == 200
    assert response.json() == get_settings().watchlist


def test_candle_check_data_rejects_unknown_interval() -> None:
    client = make_client()
    response = client.get("/dashboard/candles/data?symbol=HDFCBANK&interval=2m&days=1")
    assert response.status_code == 400


class FakeCache:
    """No-op cache: every get() misses, set() records what it was given --
    isolates the endpoint test from needing a real Redis."""

    def __init__(self) -> None:
        self.set_calls: list[tuple[str, dict]] = []

    async def get(self, key: str):
        return None

    async def set(self, key: str, value, ttl_seconds: int | None = None) -> None:
        self.set_calls.append((key, value))


@pytest.mark.db
async def test_candle_check_data_returns_candles_within_the_requested_window(
    test_session_factory,
) -> None:
    """Uses httpx.AsyncClient(transport=ASGITransport(...)), not the sync
    starlette TestClient: TestClient drives the ASGI app from its own
    background thread with its own event loop, but test_session_factory's
    AsyncConnection is bound to *this* test's event loop -- asyncpg then
    raises "attached to a different loop" the moment a route handler touches
    it (see test_prediction_api.py's lifecycle tests for the same note)."""
    now = datetime.now(UTC)
    async with test_session_factory() as session:
        session.add_all([
            OhlcvCandle(
                symbol="HDFCBANK", timeframe="1m", ts=now - timedelta(minutes=1),
                open=100.0, high=101.0, low=99.0, close=100.5, volume=1000,
            ),
            # Outside the 1-day window -- must not appear in the response.
            OhlcvCandle(
                symbol="HDFCBANK", timeframe="1m", ts=now - timedelta(days=5),
                open=90.0, high=91.0, low=89.0, close=90.5, volume=500,
            ),
        ])
        await session.commit()

    import app.database.session as session_module

    original_get_session_factory = session_module.get_session_factory
    session_module.get_session_factory = lambda: test_session_factory
    fake_cache = FakeCache()
    container.register(CacheService, lambda: fake_cache)
    try:
        app = FastAPI()
        app.include_router(dashboard_api.router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.get(
                "/dashboard/candles/data?symbol=HDFCBANK&interval=1m&days=1"
            )
    finally:
        session_module.get_session_factory = original_get_session_factory

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["candles"][0]["close"] == 100.5
    # Response was written through the cache, matching the "route dashboard
    # reads through Redis" requirement.
    assert len(fake_cache.set_calls) == 1
