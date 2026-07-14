import asyncio

import fakeredis.aioredis
import pytest

from app.core.cache import CacheService


def make_cache() -> CacheService:
    return CacheService(client=fakeredis.aioredis.FakeRedis(decode_responses=True))


class _BrokenRedisClient:
    """Stands in for a Redis client whose connection is down -- every
    method raises the same ConnectionError a real redis-py client would
    raise when it can't reach the server (IRR-2026-07-11 finding #8: this
    documented degrade path had never actually been exercised by a test
    that simulates a real connection failure)."""

    async def get(self, key: str) -> None:
        raise ConnectionError("redis unavailable")

    async def set(self, *args, **kwargs) -> None:
        raise ConnectionError("redis unavailable")


async def test_set_get_invalidate() -> None:
    cache = make_cache()
    await cache.set("quotes:NIFTY", {"ltp": 25000}, ttl_seconds=60)
    assert await cache.get("quotes:NIFTY") == {"ltp": 25000}
    await cache.invalidate("quotes:NIFTY")
    assert await cache.get("quotes:NIFTY") is None


async def test_get_or_set_fetches_once() -> None:
    cache = make_cache()
    calls = {"n": 0}

    async def fetch() -> dict:
        calls["n"] += 1
        return {"value": 42}

    first = await cache.get_or_set("k", fetch, ttl_seconds=60)
    second = await cache.get_or_set("k", fetch, ttl_seconds=60)
    assert first == second == {"value": 42}
    assert calls["n"] == 1
    assert cache.metrics()["hits"] == 1
    assert cache.metrics()["misses"] == 1


async def test_stale_while_revalidate_serves_stale_and_refreshes() -> None:
    cache = make_cache()
    calls = {"n": 0}

    async def fetch() -> dict:
        calls["n"] += 1
        return {"version": calls["n"]}

    await cache.get_or_set("k", fetch, ttl_seconds=1, stale_ttl_seconds=60)
    # Simulate freshness expiry without waiting: drop the marker key.
    await cache._client.delete("qs:k:fresh")

    stale = await cache.get_or_set("k", fetch, ttl_seconds=1, stale_ttl_seconds=60)
    assert stale == {"version": 1}  # stale value served immediately
    assert cache.stale_hits == 1
    await asyncio.sleep(0.05)  # let background refresh run
    assert calls["n"] == 2  # refreshed in background


async def test_rate_limited() -> None:
    cache = make_cache()
    key = "angel_one"
    results = [await cache.rate_limited(key, max_calls=3, window_seconds=60) for _ in range(5)]
    assert results == [False, False, False, True, True]


async def test_invalidate_prefix() -> None:
    cache = make_cache()
    await cache.set("quotes:A", 1)
    await cache.set("quotes:B", 2)
    await cache.set("macro:X", 3)
    deleted = await cache.invalidate_prefix("quotes:")
    assert deleted == 2
    assert await cache.get("macro:X") == 3


# --- Redis outage degrade path (IRR-2026-07-11 finding #8) ------------------

async def test_get_safe_returns_none_on_a_real_connection_failure() -> None:
    cache = CacheService(client=_BrokenRedisClient())
    assert await cache.get_safe("quotes:NIFTY") is None


async def test_set_safe_returns_false_on_a_real_connection_failure() -> None:
    cache = CacheService(client=_BrokenRedisClient())
    assert await cache.set_safe("quotes:NIFTY", {"ltp": 25000}) is False


async def test_raw_get_and_set_still_propagate_the_outage() -> None:
    """The _safe variants are the documented degrade boundary -- the raw
    get()/set() intentionally still raise, so a caller that actually needs
    to know Redis is down (rather than silently treating a miss as a
    cache-empty) isn't lied to."""
    cache = CacheService(client=_BrokenRedisClient())
    with pytest.raises(ConnectionError):
        await cache.get("quotes:NIFTY")
    with pytest.raises(ConnectionError):
        await cache.set("quotes:NIFTY", {"ltp": 25000})
