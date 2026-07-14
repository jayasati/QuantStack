"""Exercises the real app.main.lifespan() end-to-end (IRR-2026-07-11 #3).

httpx.ASGITransport (used by every existing API test in this suite) does
NOT invoke ASGI lifespan startup/shutdown unless explicitly entered as a
context manager -- so wire_default_services(), the broker connection
attempt, and the ~16-engine/~45-registration startup sequence had never
actually run in any test. A wiring regression (wrong factory signature,
missing import, circular dependency) would previously only have surfaced
at real process startup. This drives lifespan() directly against a real,
migrated test Postgres.

Broker credentials are blanked so this never attempts a real Angel One
login even when a developer's local .env has real credentials -- connect()
falls back to its documented "no credentials -> stays disconnected"
branch (see test_container.py::test_adapter_without_credentials_stays_disconnected),
matching how the real app also behaves in an environment with no broker
credentials configured.
"""

import pytest
from fastapi import FastAPI

from app.core.config import get_settings
from app.core.container import container
from app.database import session as db_session_module
from app.intelligence.composite import CompositeMarketIntelligenceEngine
from app.main import lifespan
from app.market.broker import BrokerInterface
from app.prediction.lifecycle import OpportunityLifecycleManager

pytestmark = pytest.mark.db


async def test_lifespan_wires_all_default_services_against_a_real_db(
    postgres_test_url, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", postgres_test_url)
    monkeypatch.setenv("ANGEL_ONE_API_KEY", "")
    monkeypatch.setenv("ANGEL_ONE_CLIENT_ID", "")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    container.reset()

    app = FastAPI()
    try:
        async with lifespan(app):
            # If wire_default_services() or any of the ~45 registrations,
            # the eager OpportunityLifecycleManager resolve, the 16 feature
            # engines' sync_registry() DB writes, or scheduler/job setup
            # raised, we'd never reach this line.
            assert container.resolve(OpportunityLifecycleManager) is not None
            assert container.resolve(CompositeMarketIntelligenceEngine) is not None
            broker = container.resolve(BrokerInterface)
            assert broker is not None
            assert await broker.is_connected() is False  # blanked credentials -> never connected
    finally:
        container.reset()
        get_settings.cache_clear()
