"""Feature Store (Volume 3, Chapter 4): online + offline storage.

Offline store: PostgreSQL feature_store table, one row per feature observation,
idempotent upserts keyed on (feature_name, feature_version, symbol, timeframe, ts).
Online store: Redis (via CacheService), latest value of every feature per
symbol/timeframe for fast live-model lookup. Redis outages degrade gracefully —
the offline store remains the source of truth.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.cache import CacheService
from app.core.logging import get_logger
from app.database.tables import FeatureStoreRow
from app.features.schema import FeatureValue

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession] | async_sessionmaker[AsyncSession]

_UPSERT_CHUNK = 500


def _online_key(symbol: str, timeframe: str) -> str:
    return f"features:{symbol}:{timeframe}"


class FeatureStore:
    def __init__(
        self,
        session_factory: SessionFactory | None = None,
        cache: CacheService | None = None,
        online_ttl_seconds: int = 3600,
    ) -> None:
        self._sessions = session_factory
        self._cache = cache
        self._online_ttl = online_ttl_seconds

    async def write(self, values: list[FeatureValue]) -> dict[str, int]:
        offline = await self._write_offline(values)
        online = await self._write_online(values)
        return {"offline_rows": offline, "online_entries": online}

    async def _write_offline(self, values: list[FeatureValue]) -> int:
        if self._sessions is None or not values:
            return 0
        rows = [
            {
                "feature_name": v.feature_name,
                "feature_version": v.feature_version,
                "symbol": v.symbol,
                "timeframe": v.timeframe,
                "ts": v.ts,
                "value": v.value,
                "window_size": v.window,
            }
            for v in values
        ]
        async with self._sessions() as session:
            for start in range(0, len(rows), _UPSERT_CHUNK):
                chunk = rows[start:start + _UPSERT_CHUNK]
                stmt = pg_insert(FeatureStoreRow).values(chunk)
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[
                            "feature_name", "feature_version", "symbol", "timeframe", "ts"
                        ],
                        set_={"value": stmt.excluded.value},
                    )
                )
            await session.commit()
        return len(rows)

    async def _write_online(self, values: list[FeatureValue]) -> int:
        """Publish the latest observation of every feature to Redis.

        Merges into the existing entry — several engines share one key per
        symbol/timeframe, and a plain overwrite would drop every other
        engine's features.
        """
        if self._cache is None or not values:
            return 0
        latest: dict[tuple[str, str], dict[str, FeatureValue]] = {}
        for value in values:
            group = latest.setdefault((value.symbol, value.timeframe), {})
            current = group.get(value.feature_name)
            if current is None or value.ts >= current.ts:
                group[value.feature_name] = value

        written = 0
        for (symbol, timeframe), group in latest.items():
            key = _online_key(symbol, timeframe)
            payload: dict[str, Any] = await self._cache.get_safe(key) or {}
            payload.update(
                {
                    name: {
                        "value": v.value,
                        "version": v.feature_version,
                        "ts": v.ts.isoformat(),
                    }
                    for name, v in group.items()
                }
            )
            if await self._cache.set_safe(key, payload, ttl_seconds=self._online_ttl):
                written += len(group)
        return written

    async def latest(self, symbol: str, timeframe: str) -> dict[str, Any]:
        """Latest value per feature: Redis first, offline store as fallback."""
        if self._cache is not None:
            cached = await self._cache.get_safe(_online_key(symbol, timeframe))
            if cached:
                return cached
        if self._sessions is None:
            return {}
        async with self._sessions() as session:
            result = await session.execute(
                select(FeatureStoreRow)
                .where(
                    FeatureStoreRow.symbol == symbol,
                    FeatureStoreRow.timeframe == timeframe,
                )
                .order_by(desc(FeatureStoreRow.ts))
                .limit(5000)
            )
            rows = result.scalars().all()
        payload: dict[str, Any] = {}
        for row in rows:  # rows are ts-descending: first hit per feature is the latest
            if row.feature_name not in payload:
                payload[row.feature_name] = {
                    "value": row.value,
                    "version": row.feature_version,
                    "ts": row.ts.isoformat(),
                }
        return payload

    async def history(
        self,
        feature_name: str,
        symbol: str | None = None,
        timeframe: str | None = None,
        version: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if self._sessions is None:
            return []
        query = select(FeatureStoreRow).where(FeatureStoreRow.feature_name == feature_name)
        if symbol is not None:
            query = query.where(FeatureStoreRow.symbol == symbol)
        if timeframe is not None:
            query = query.where(FeatureStoreRow.timeframe == timeframe)
        if version is not None:
            query = query.where(FeatureStoreRow.feature_version == version)
        query = query.order_by(desc(FeatureStoreRow.ts)).offset(offset).limit(limit)
        async with self._sessions() as session:
            rows = (await session.execute(query)).scalars().all()
        return [
            {
                "feature_name": row.feature_name,
                "version": row.feature_version,
                "symbol": row.symbol,
                "timeframe": row.timeframe,
                "ts": row.ts.isoformat(),
                "value": row.value,
                "window": row.window_size,
            }
            for row in rows
        ]

    async def latest_ts_map(
        self,
        symbol: str,
        timeframe: str,
        feature_names: list[str] | None = None,
    ) -> dict[str, datetime]:
        """Most recent stored timestamp per feature — drives incremental runs.

        Per-feature (not per-engine) so a feature that starts producing values
        later than its siblings (e.g. VIX distance waiting on VIX data) still
        gets its full history stored.
        """
        if self._sessions is None:
            return {}
        query = (
            select(FeatureStoreRow.feature_name, func.max(FeatureStoreRow.ts))
            .where(
                FeatureStoreRow.symbol == symbol,
                FeatureStoreRow.timeframe == timeframe,
            )
            .group_by(FeatureStoreRow.feature_name)
        )
        if feature_names is not None:
            query = query.where(FeatureStoreRow.feature_name.in_(feature_names))
        async with self._sessions() as session:
            rows = (await session.execute(query)).all()
        return {feature_name: ts for feature_name, ts in rows}
