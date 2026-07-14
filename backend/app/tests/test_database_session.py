"""Tests for app/database/session.py (IRR-2026-07-11 finding #10: no
dedicated test file existed for the DB session module before this)."""

import app.database.session as session_module
from app.database.session import (
    dispose_engine,
    get_engine,
    get_session,
    get_session_factory,
)


async def _reset() -> None:
    await dispose_engine()


async def test_get_engine_is_a_singleton_until_disposed() -> None:
    await _reset()
    try:
        first = get_engine()
        second = get_engine()
        assert first is second
    finally:
        await _reset()


async def test_dispose_engine_clears_module_state_so_a_fresh_engine_is_built_next() -> None:
    await _reset()
    try:
        first = get_engine()
        await dispose_engine()
        assert session_module._engine is None
        assert session_module._session_factory is None
        second = get_engine()
        assert second is not first
    finally:
        await _reset()


async def test_get_session_factory_returns_the_same_factory_as_get_engine_wires_up() -> None:
    await _reset()
    try:
        get_engine()
        factory = get_session_factory()
        assert factory is session_module._session_factory
    finally:
        await _reset()


async def test_get_session_yields_a_usable_async_session_context() -> None:
    await _reset()
    try:
        agen = get_session()
        session = await agen.__anext__()
        try:
            assert session is not None
        finally:
            # Drain the generator to run its own cleanup (session close).
            try:
                await agen.__anext__()
            except StopAsyncIteration:
                pass
    finally:
        await _reset()
