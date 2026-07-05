import asyncio

import fakeredis.aioredis

from app.core.cache import CacheService


def make_cache() -> CacheService:
    return CacheService(client=fakeredis.aioredis.FakeRedis(decode_responses=True))


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
