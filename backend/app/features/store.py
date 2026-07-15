"""Feature Store (Volume 3, Chapter 4): online + offline storage.

Offline store: PostgreSQL feature_store table, one row per feature observation,
idempotent upserts keyed on (feature_name, feature_version, symbol, timeframe, ts).
This remains the sole READ path (latest/history/latest_ts_map all query
Postgres) -- every consumer (HistoricalReplayEngine, EnsemblePredictionEngine's
training join, FeatureSelectionEngine, FeatureQualityEngine, FeatureDriftEngine)
depends on it working exactly as it always has.

Parquet archival: a second, best-effort WRITE-ONLY sink under
data/feature_store_parquet/ (Chapter 4's "Parquet, PostgreSQL" pair) -- an
export destination, not a query path. Failures here degrade gracefully and
never block the Postgres/Redis writes, same as a storage failure never blocks
event publishing elsewhere in this codebase (collectors/pipeline.py).

Online store: Redis (via CacheService), latest value of every feature per
symbol/timeframe for fast live-model lookup. Redis outages degrade gracefully —
the offline store remains the source of truth.
"""

import asyncio
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.cache import CacheService
from app.core.config import REPO_ROOT
from app.core.logging import get_logger
from app.database.tables import FeatureStoreRow
from app.features.schema import FeatureValue

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession] | async_sessionmaker[AsyncSession]

_UPSERT_CHUNK = 500
PARQUET_ROOT = REPO_ROOT / "data" / "feature_store_parquet"


def _online_key(symbol: str, timeframe: str) -> str:
    return f"features:{symbol}:{timeframe}"


def _write_parquet_sync(values: list[FeatureValue]) -> None:
    """Synchronous, blocking Parquet write -- always called via
    asyncio.to_thread, never directly on the event loop. pyarrow is imported
    lazily (same pattern as FinBertSentimentProvider's lazy transformers/torch
    import) so every process that imports this module doesn't pay pyarrow's
    import cost unless a write actually happens.

    Layout: Hive-style partitions by symbol/timeframe, one small immutable
    "part file" per write() call (Parquet has no native row-level upsert;
    real systems like Spark/Delta write exactly this way -- compacting many
    small part files is a separate, later concern, not needed for a v1
    archival sink).
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    groups: dict[tuple[str, str], list[FeatureValue]] = {}
    for v in values:
        groups.setdefault((v.symbol, v.timeframe), []).append(v)

    part_name = f"part-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.parquet"
    for (symbol, timeframe), group in groups.items():
        partition_dir = PARQUET_ROOT / f"symbol={symbol}" / f"timeframe={timeframe}"
        partition_dir.mkdir(parents=True, exist_ok=True)
        # symbol/timeframe are deliberately NOT repeated as stored columns --
        # they're already fully recoverable from the Hive partition path
        # above. Storing them again would conflict with any partition-aware
        # reader (pyarrow.dataset/pandas/Spark all infer partition columns
        # from the path and error on a duplicate, differently-typed column
        # of the same name inside the file).
        table = pa.Table.from_pylist([
            {
                "feature_name": v.feature_name,
                "feature_version": v.feature_version,
                "ts": v.ts,
                "value": v.value,
                "window": v.window,
            }
            for v in group
        ])
        pq.write_table(table, partition_dir / part_name)


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
        await self._write_parquet(values)
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

    async def _write_parquet(self, values: list[FeatureValue]) -> None:
        """Best-effort archival write (Chapter 4's "Parquet, PostgreSQL"
        pair) -- write-only, never a read source. Never raises: a Parquet
        hiccup must never block the Postgres/Redis writes above, same as a
        storage failure never blocks event publishing in
        collectors/pipeline.py. Blocking pyarrow file I/O is offloaded to a
        thread so it never runs on the event loop directly."""
        if not values:
            return
        try:
            await asyncio.to_thread(_write_parquet_sync, values)
        except Exception as exc:
            logger.error(
                "parquet archival write failed",
                extra={"error": str(exc), "rows": len(values)},
            )

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

    async def refresh_online_ttl(self, symbol: str, timeframe: str) -> bool:
        """Re-extend the online key's TTL without needing new values.

        An incremental run whose underlying data hasn't advanced past the
        watermark yet (e.g. a "D"-timeframe feature re-run on a schedule
        that ticks far more often than once a day) calls write([]), which
        writes nothing -- _write_online() above only refreshes the TTL as a
        side effect of writing new values, so a feature update slower than
        online_ttl_seconds would otherwise silently fall out of Redis and
        never come back (perf-audit-2026-07-14 finding 8: live Redis held
        13 keys vs ~45 expected). Callers should reach for this specifically
        when a run found nothing new, not on every run -- write() already
        refreshes the TTL when it has something to write."""
        if self._cache is None:
            return False
        key = _online_key(symbol, timeframe)
        payload = await self._cache.get_safe(key)
        if not payload:
            return False
        return await self._cache.set_safe(key, payload, ttl_seconds=self._online_ttl)

    async def latest(self, symbol: str, timeframe: str) -> dict[str, Any]:
        """Latest value per feature: Redis first, offline store as fallback."""
        if self._cache is not None:
            cached = await self._cache.get_safe(_online_key(symbol, timeframe))
            if cached:
                return cached
        if self._sessions is None:
            return {}
        async with self._sessions() as session:
            # DISTINCT ON (feature_name) ... ORDER BY feature_name, ts DESC
            # asks Postgres for exactly the ~150 rows needed instead of
            # fetching up to 5000 rows and keeping only the first hit per
            # feature in Python (~97% waste per call, confirmed live
            # 2026-07-14 -- this call runs ~264x per /prediction/candidates
            # request) -- the same pattern replay.py already uses for its
            # own point-in-time feature read. Also selects only the 4
            # columns actually used below, not the full ORM entity.
            result = await session.execute(
                select(
                    FeatureStoreRow.feature_name,
                    FeatureStoreRow.value,
                    FeatureStoreRow.feature_version,
                    FeatureStoreRow.ts,
                )
                .where(
                    FeatureStoreRow.symbol == symbol,
                    FeatureStoreRow.timeframe == timeframe,
                )
                .distinct(FeatureStoreRow.feature_name)
                .order_by(FeatureStoreRow.feature_name, desc(FeatureStoreRow.ts))
            )
            rows = result.all()
        return {
            row.feature_name: {
                "value": row.value,
                "version": row.feature_version,
                "ts": row.ts.isoformat(),
            }
            for row in rows
        }

    async def history(
        self,
        feature_name: str,
        symbol: str | None = None,
        timeframe: str | None = None,
        version: str | None = None,
        limit: int = 100,
        offset: int = 0,
        since: datetime | None = None,
        raw_ts: bool = False,
    ) -> list[dict[str, Any]]:
        """`since` bounds the query in SQL (a WHERE ts >= since filter) for a
        caller that only ever needs a recent window -- without it, `limit`
        is the only bound, which lets a fast-cadence feature's history
        stretch back arbitrarily far past however-many rows are needed
        (perf-audit-2026-07-14 finding 9: correlation.py fetching up to
        20,000 rows per series for a 60-day correlation, ~300x overfetch).
        `raw_ts=True` returns `ts` as the native datetime instead of an
        isoformat string, for a caller that would otherwise immediately
        re-parse it back out (same finding: ~840k needless
        serialize->reparse round trips per correlation run) -- default
        stays isoformat-string since every other caller expects that."""
        if self._sessions is None:
            return []
        # Same fix as latest() above: select only the columns the dict
        # comprehension below actually reads, not the full ORM entity.
        query = select(
            FeatureStoreRow.feature_name,
            FeatureStoreRow.feature_version,
            FeatureStoreRow.symbol,
            FeatureStoreRow.timeframe,
            FeatureStoreRow.ts,
            FeatureStoreRow.value,
            FeatureStoreRow.window_size,
        ).where(FeatureStoreRow.feature_name == feature_name)
        if symbol is not None:
            query = query.where(FeatureStoreRow.symbol == symbol)
        if timeframe is not None:
            query = query.where(FeatureStoreRow.timeframe == timeframe)
        if version is not None:
            query = query.where(FeatureStoreRow.feature_version == version)
        if since is not None:
            query = query.where(FeatureStoreRow.ts >= since)
        query = query.order_by(desc(FeatureStoreRow.ts)).offset(offset).limit(limit)
        async with self._sessions() as session:
            rows = (await session.execute(query)).all()
        return [
            {
                "feature_name": row.feature_name,
                "version": row.feature_version,
                "symbol": row.symbol,
                "timeframe": row.timeframe,
                "ts": row.ts if raw_ts else row.ts.isoformat(),
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
