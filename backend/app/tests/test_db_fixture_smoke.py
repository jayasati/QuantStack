"""Smoke test for the test_session_factory fixture itself -- if this fails,
every other `db`-marked test's failure is noise until this is fixed."""

import pytest
from sqlalchemy import select

from app.database.tables import MarketEvent

pytestmark = pytest.mark.db


async def test_can_write_and_read_back_a_row(test_session_factory) -> None:
    async with test_session_factory() as session:
        session.add(MarketEvent(event_type="smoke.test", source="conftest", data={"x": 1}))
        await session.commit()

    async with test_session_factory() as session:
        result = await session.execute(
            select(MarketEvent).where(MarketEvent.event_type == "smoke.test")
        )
        row = result.scalar_one()
    assert row.source == "conftest"
    assert row.data == {"x": 1}


async def test_rolls_back_between_tests_isolation_check(test_session_factory) -> None:
    """If SAVEPOINT isolation is broken, this sees the previous test's row."""
    async with test_session_factory() as session:
        result = await session.execute(
            select(MarketEvent).where(MarketEvent.event_type == "smoke.test")
        )
        assert result.scalar_one_or_none() is None
