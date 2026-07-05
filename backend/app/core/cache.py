"""Redis-backed caching for collector outputs (Volume 2, Prompt 2.14).

Features: configurable TTLs, explicit invalidation, stale-while-revalidate,
rate-limit protection for upstream APIs, and cache metrics.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class CacheService:
    def __init__(self, client: aioredis.Redis | None = None, prefix: str = "qs") -> None:
        self._client = client or aioredis.from_url(
            get_settings().redis_url, decode_responses=True
        )
        self._prefix = prefix
        self.hits = 0
        self.misses = 0
        self.stale_hits = 0

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    async def get(self, key: str) -> Any | None:
        raw = await self._client.get(self._key(key))
        if raw is None:
            self.misses += 1
            return None
        self.hits += 1
        return json.loads(raw)

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else get_settings().cache_timeout
        await self._client.set(self._key(key), json.dumps(value, default=str), ex=ttl)

    async def invalidate(self, key: str) -> None:
        await self._client.delete(self._key(key))

    async def invalidate_prefix(self, prefix: str) -> int:
        """Delete all keys under a namespace, e.g. invalidate_prefix('quotes')."""
        pattern = self._key(f"{prefix}*")
        deleted = 0
        async for found in self._client.scan_iter(match=pattern):
            await self._client.delete(found)
            deleted += 1
        return deleted

    async def get_or_set(
        self,
        key: str,
        fetch: Callable[[], Awaitable[Any]],
        ttl_seconds: int | None = None,
        stale_ttl_seconds: int | None = None,
    ) -> Any:
        """Return cached value; on miss, fetch and cache.

        Stale-while-revalidate: values live in Redis for ttl + stale_ttl. A
        value older than ttl (tracked via a freshness marker key) is returned
        immediately while a background task refreshes it.
        """
        ttl = ttl_seconds if ttl_seconds is not None else get_settings().cache_timeout
        stale_ttl = stale_ttl_seconds if stale_ttl_seconds is not None else ttl
        fresh_marker = self._key(f"{key}:fresh")

        raw = await self._client.get(self._key(key))
        if raw is not None:
            is_fresh = await self._client.exists(fresh_marker)
            if is_fresh:
                self.hits += 1
                return json.loads(raw)
            # Stale: serve immediately, refresh in the background.
            self.stale_hits += 1

            async def _refresh() -> None:
                try:
                    value = await fetch()
                    await self._store(key, value, ttl, stale_ttl)
                except Exception as exc:
                    logger.warning(
                        "stale-while-revalidate refresh failed",
                        extra={"key": key, "error": str(exc)},
                    )

            asyncio.ensure_future(_refresh())  # noqa: RUF006
            return json.loads(raw)

        self.misses += 1
        value = await fetch()
        await self._store(key, value, ttl, stale_ttl)
        return value

    async def _store(self, key: str, value: Any, ttl: int, stale_ttl: int) -> None:
        await self._client.set(
            self._key(key), json.dumps(value, default=str), ex=ttl + stale_ttl
        )
        await self._client.set(self._key(f"{key}:fresh"), "1", ex=ttl)

    async def rate_limited(self, key: str, max_calls: int, window_seconds: int) -> bool:
        """Return True when the caller should back off (limit reached)."""
        counter_key = self._key(f"ratelimit:{key}")
        current = await self._client.incr(counter_key)
        if current == 1:
            await self._client.expire(counter_key, window_seconds)
        return current > max_calls

    def metrics(self) -> dict[str, int | float]:
        total = self.hits + self.misses + self.stale_hits
        return {
            "hits": self.hits,
            "misses": self.misses,
            "stale_hits": self.stale_hits,
            "hit_rate": (self.hits + self.stale_hits) / total if total else 0.0,
        }

    async def close(self) -> None:
        await self._client.aclose()
