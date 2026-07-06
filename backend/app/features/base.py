"""Shared feature-engine machinery (Volume 3, Chapter 3).

Every domain feature engine (price, volume, ...) runs the same pipeline:
load raw candles -> calculate -> quality check -> store (online + offline)
-> publish feature event. Subclasses supply the feature definitions and the
pure calculation; everything else lives here.
"""

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from statistics import fmean, pstdev

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.cache import CacheService
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.database.tables import FeatureQualityRow, FeatureStatisticRow, OhlcvCandle
from app.events.bus import Event, EventBus
from app.features.registry import FeatureRegistry
from app.features.schema import Candle, FeatureDefinition, FeatureValue, Series
from app.features.store import FeatureStore

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession] | async_sessionmaker[AsyncSession]


class BaseFeatureEngine:
    name = "feature_engine"
    category = "feature"
    # Engines whose features regress against the benchmark symbol set this.
    uses_benchmark = False

    def __init__(
        self,
        session_factory: SessionFactory | None = None,
        bus: EventBus | None = None,
        cache: CacheService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._sessions = session_factory
        self._bus = bus
        self.windows = tuple(self._settings.feature_windows)
        self.benchmark_symbol = self._settings.feature_benchmark_symbol
        self.registry = FeatureRegistry()
        for definition in self._definitions():
            self.registry.register(definition)
        self.store = FeatureStore(session_factory=session_factory, cache=cache)

    def _definitions(self) -> list[FeatureDefinition]:
        raise NotImplementedError

    def _compute(
        self, candles: Sequence[Candle], benchmark: Sequence[Candle] | None = None
    ) -> dict[str, Series]:
        raise NotImplementedError

    def _reference_symbol(self, symbol: str) -> str | None:
        """Second symbol whose candles are passed to _compute (e.g. benchmark, VIX)."""
        if self.uses_benchmark and symbol != self.benchmark_symbol:
            return self.benchmark_symbol
        return None

    async def sync_registry(self) -> dict[str, int]:
        if self._sessions is None:
            return {"features": 0, "dependencies": 0}
        return await self.registry.sync_to_db(self._sessions)

    def build_values(
        self,
        symbol: str,
        timeframe: str,
        candles: Sequence[Candle],
        series: dict[str, Series],
        since: datetime | Mapping[str, datetime] | None = None,
    ) -> list[FeatureValue]:
        values: list[FeatureValue] = []
        for feature_name, feature_series in series.items():
            definition = self.registry.get(feature_name)
            version = definition.version if definition else "v1"
            window = definition.window if definition else None
            cutoff = since.get(feature_name) if isinstance(since, Mapping) else since
            for candle, value in zip(candles, feature_series, strict=True):
                if value is None or not math.isfinite(value):
                    continue
                if cutoff is not None and candle.ts <= cutoff:
                    continue
                values.append(
                    FeatureValue(
                        feature_name=feature_name,
                        feature_version=version,
                        symbol=symbol,
                        timeframe=timeframe,
                        ts=candle.ts,
                        value=value,
                        window=window,
                    )
                )
        return values

    async def run(self, symbol: str, timeframe: str = "D", full: bool = False) -> dict:
        """Compute and store features. `full=True` bypasses the incremental
        watermarks and re-upserts the whole history — use after raw-data
        backfills that add bars older than what is already stored."""
        candles = await self._load_candles(symbol, timeframe)
        if len(candles) < 2:
            logger.info(
                "feature run skipped: not enough candles",
                extra={
                    "engine": self.name,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "candles": len(candles),
                },
            )
            return {"symbol": symbol, "timeframe": timeframe, "stored": 0, "skipped": True}

        benchmark: list[Candle] | None = None
        reference = self._reference_symbol(symbol)
        if reference is not None:
            benchmark = await self._load_candles(reference, timeframe)

        series = self._compute(candles, benchmark)
        since: Mapping[str, datetime] | None = None
        if not full:
            since = await self.store.latest_ts_map(symbol, timeframe, feature_names=list(series))
        values = self.build_values(symbol, timeframe, candles, series, since=since)
        quality = self._quality_check(values)
        stored = await self.store.write(values)
        await self._persist_run_metadata(symbol, timeframe, values, quality)

        if self._bus is not None and values:
            await self._bus.publish(
                Event(
                    type=f"feature.{self.category}.updated",
                    payload={
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "features": len(series),
                        "values_stored": stored["offline_rows"],
                        "as_of": candles[-1].ts.isoformat(),
                    },
                    source=self.name,
                )
            )
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "features": len(series),
            "stored": stored["offline_rows"],
            "online_entries": stored["online_entries"],
            "quality": {name: round(score, 2) for name, (score, _) in quality.items()},
        }

    async def run_all(self) -> list[dict]:
        results: list[dict] = []
        for timeframe in self._settings.feature_timeframes:
            for symbol in self._settings.watchlist:
                try:
                    results.append(await self.run(symbol, timeframe))
                except Exception as exc:
                    logger.error(
                        "feature run failed",
                        extra={
                            "engine": self.name,
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "error": str(exc),
                        },
                    )
                    results.append({"symbol": symbol, "timeframe": timeframe, "error": str(exc)})
        return results

    def _quality_check(self, values: list[FeatureValue]) -> dict[str, tuple[float, int]]:
        """Per-feature sanity score: % of values inside the registered expected range."""
        grouped: dict[str, list[float]] = {}
        for value in values:
            grouped.setdefault(value.feature_name, []).append(value.value)
        quality: dict[str, tuple[float, int]] = {}
        for feature_name, feature_values in grouped.items():
            definition = self.registry.get(feature_name)
            if definition is None:
                continue
            low, high = definition.expected_range
            in_range = sum(
                1
                for v in feature_values
                if (low is None or v >= low) and (high is None or v <= high)
            )
            score = in_range / len(feature_values) * 100
            quality[feature_name] = (score, len(feature_values))
            if score < definition.quality_threshold:
                logger.warning(
                    "feature quality below threshold",
                    extra={
                        "feature": feature_name,
                        "score": round(score, 2),
                        "threshold": definition.quality_threshold,
                    },
                )
        return quality

    async def _persist_run_metadata(
        self,
        symbol: str,
        timeframe: str,
        values: list[FeatureValue],
        quality: dict[str, tuple[float, int]],
    ) -> None:
        if self._sessions is None or not values:
            return
        grouped: dict[str, list[float]] = {}
        for value in values:
            grouped.setdefault(value.feature_name, []).append(value.value)

        quality_rows = [
            FeatureQualityRow(
                feature_name=feature_name,
                symbol=symbol,
                timeframe=timeframe,
                quality_score=score,
                sample_count=count,
            )
            for feature_name, (score, count) in quality.items()
        ]
        statistic_rows = [
            FeatureStatisticRow(
                feature_name=feature_name,
                symbol=symbol,
                timeframe=timeframe,
                mean=fmean(feature_values),
                std=pstdev(feature_values) if len(feature_values) > 1 else 0.0,
                min_value=min(feature_values),
                max_value=max(feature_values),
                sample_count=len(feature_values),
            )
            for feature_name, feature_values in grouped.items()
        ]
        async with self._sessions() as session:
            session.add_all([*quality_rows, *statistic_rows])
            await session.commit()

    async def _load_candles(self, symbol: str, timeframe: str) -> list[Candle]:
        if self._sessions is None:
            return []
        lookback = self._settings.feature_candle_lookback
        async with self._sessions() as session:
            result = await session.execute(
                select(OhlcvCandle)
                .where(OhlcvCandle.symbol == symbol, OhlcvCandle.timeframe == timeframe)
                .order_by(OhlcvCandle.ts.desc())
                .limit(lookback)
            )
            rows = result.scalars().all()
        return [
            Candle(
                ts=row.ts,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume or 0,
            )
            for row in reversed(rows)
        ]
