"""Price Feature Engine (Volume 3, Prompt 3.1).

Transforms OHLCV candles into the 17 price features of Chapter 9, across the
rolling windows 5/10/20/50/100/200. Every feature is registered with Chapter 5
metadata, stored independently in the Feature Store, and stamped with a
version so calculations stay reproducible (Chapter 6).

Feature conventions:
- Returns are ratios; everything suffixed _pct / distance / momentum is in %.
- Daily Range % measures bar extension vs the prior close; Intraday Range %
  measures open-to-close movement within the bar.
- ATR is the simple mean of True Range over the window.
- Beta/Alpha/Correlation regress the symbol's simple returns on the benchmark's
  (alpha is the per-bar abnormal return in %); the benchmark symbol itself
  skips these three features.
"""

import math
from collections.abc import Callable, Sequence
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
from app.features.schema import Candle, FeatureDefinition, FeatureValue
from app.features.store import FeatureStore

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession] | async_sessionmaker[AsyncSession]

ENGINE_NAME = "price_feature_engine"
ENGINE_VERSION = "v1"
CATEGORY = "price"

Series = list[float | None]


# --- Feature definitions -------------------------------------------------------

def price_feature_definitions(
    windows: Sequence[int],
    benchmark_symbol: str,
    calculation_frequency: str = "on_schedule",
) -> list[FeatureDefinition]:
    def define(name: str, description: str, unit: str,
               expected: tuple[float | None, float | None],
               dependencies: tuple[str, ...] = (), window: int | None = None,
               ) -> FeatureDefinition:
        return FeatureDefinition(
            feature_name=name,
            category=CATEGORY,
            description=description,
            version=ENGINE_VERSION,
            dependencies=dependencies,
            calculation_frequency=calculation_frequency,
            owner=ENGINE_NAME,
            unit=unit,
            expected_range=expected,
            window=window,
        )

    definitions = [
        define("price_log_return", "Log return of close vs prior close.",
               "ratio", (-1.0, 1.0)),
        define("price_simple_return", "Simple return of close vs prior close.",
               "ratio", (-1.0, 1.0)),
        define("price_gap_pct", "Open gap vs prior close, in %.", "%", (-25.0, 25.0)),
        define("price_true_range", "True range: max of bar range and gaps vs prior close.",
               "price", (0.0, None)),
        define("price_daily_range_pct", "High-low bar range as % of prior close.",
               "%", (0.0, 50.0)),
        define("price_intraday_range_pct", "Open-to-close move within the bar, in %.",
               "%", (-50.0, 50.0)),
    ]
    for w in windows:
        definitions.extend([
            define(f"price_atr_{w}", f"Average True Range over {w} bars.",
                   "price", (0.0, None), ("price_true_range",), w),
            define(f"price_rolling_high_{w}", f"Highest high over {w} bars.",
                   "price", (0.0, None), (), w),
            define(f"price_rolling_low_{w}", f"Lowest low over {w} bars.",
                   "price", (0.0, None), (), w),
            define(f"price_dist_from_high_{w}",
                   f"Close distance from the {w}-bar rolling high, in % (<= 0).",
                   "%", (-100.0, 0.001), (f"price_rolling_high_{w}",), w),
            define(f"price_dist_from_low_{w}",
                   f"Close distance from the {w}-bar rolling low, in % (>= 0).",
                   "%", (-0.001, 500.0), (f"price_rolling_low_{w}",), w),
            define(f"price_vwap_distance_{w}",
                   f"Close distance from the {w}-bar rolling VWAP, in %.",
                   "%", (-50.0, 50.0), (), w),
            define(f"price_momentum_{w}", f"Close change over {w} bars, in %.",
                   "%", (-95.0, 500.0), (), w),
            define(f"price_acceleration_{w}",
                   f"Bar-over-bar change of {w}-bar momentum, in % points.",
                   "%", (-200.0, 200.0), (f"price_momentum_{w}",), w),
            define(f"price_beta_{w}",
                   f"Rolling beta of simple returns vs {benchmark_symbol} over {w} bars.",
                   "ratio", (-5.0, 5.0), ("price_simple_return",), w),
            define(f"price_alpha_{w}",
                   f"Rolling per-bar alpha vs {benchmark_symbol} over {w} bars, in %.",
                   "%", (-10.0, 10.0), (f"price_beta_{w}",), w),
            define(f"price_correlation_{w}",
                   f"Rolling correlation of simple returns vs {benchmark_symbol} "
                   f"over {w} bars.",
                   "ratio", (-1.0, 1.0), ("price_simple_return",), w),
        ])
    return definitions


# --- Pure calculations -----------------------------------------------------------

def compute_price_features(
    candles: Sequence[Candle],
    benchmark: Sequence[Candle] | None = None,
    windows: Sequence[int] = (5, 10, 20, 50, 100, 200),
) -> dict[str, Series]:
    """Compute every price feature as a series aligned to `candles`.

    Cold-start bars (not enough history for a window) hold None and are never
    emitted or stored.
    """
    n = len(candles)
    opens = [c.open for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]
    typical = [(h + low + c) / 3 for h, low, c in zip(highs, lows, closes, strict=True)]

    log_return: Series = [None] * n
    simple_return: Series = [None] * n
    gap_pct: Series = [None] * n
    true_range: Series = [None] * n
    daily_range_pct: Series = [None] * n
    intraday_range_pct: Series = [None] * n

    for i in range(n):
        if i > 0 and closes[i - 1] > 0:
            prev_close = closes[i - 1]
            simple_return[i] = closes[i] / prev_close - 1
            if closes[i] > 0:
                log_return[i] = math.log(closes[i] / prev_close)
            gap_pct[i] = (opens[i] - prev_close) / prev_close * 100
            true_range[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - prev_close),
                abs(lows[i] - prev_close),
            )
            daily_range_pct[i] = (highs[i] - lows[i]) / prev_close * 100
        if opens[i] > 0:
            intraday_range_pct[i] = (closes[i] - opens[i]) / opens[i] * 100

    out: dict[str, Series] = {
        "price_log_return": log_return,
        "price_simple_return": simple_return,
        "price_gap_pct": gap_pct,
        "price_true_range": true_range,
        "price_daily_range_pct": daily_range_pct,
        "price_intraday_range_pct": intraday_range_pct,
    }

    bench_return: Series | None = None
    if benchmark:
        by_ts: dict[datetime, float] = {}
        prev: float | None = None
        for candle in benchmark:
            if prev is not None and prev > 0:
                by_ts[candle.ts] = candle.close / prev - 1
            prev = candle.close
        bench_return = [by_ts.get(c.ts) for c in candles]

    for w in windows:
        rolling_high: Series = [None] * n
        rolling_low: Series = [None] * n
        dist_high: Series = [None] * n
        dist_low: Series = [None] * n
        vwap_distance: Series = [None] * n
        atr: Series = [None] * n
        momentum: Series = [None] * n
        acceleration: Series = [None] * n

        for i in range(n):
            if i >= w - 1:
                window_high = max(highs[i - w + 1 : i + 1])
                window_low = min(lows[i - w + 1 : i + 1])
                rolling_high[i] = window_high
                rolling_low[i] = window_low
                if window_high > 0:
                    dist_high[i] = (closes[i] - window_high) / window_high * 100
                if window_low > 0:
                    dist_low[i] = (closes[i] - window_low) / window_low * 100
                volume_sum = sum(volumes[i - w + 1 : i + 1])
                if volume_sum > 0:
                    vwap = (
                        sum(typical[j] * volumes[j] for j in range(i - w + 1, i + 1))
                        / volume_sum
                    )
                    if vwap > 0:
                        vwap_distance[i] = (closes[i] - vwap) / vwap * 100
            if i >= w:
                trs = [t for t in true_range[i - w + 1 : i + 1] if t is not None]
                if len(trs) == w:
                    atr[i] = sum(trs) / w
                if closes[i - w] > 0:
                    momentum[i] = (closes[i] / closes[i - w] - 1) * 100
        for i in range(1, n):
            current, previous = momentum[i], momentum[i - 1]
            if current is not None and previous is not None:
                acceleration[i] = current - previous

        out[f"price_atr_{w}"] = atr
        out[f"price_rolling_high_{w}"] = rolling_high
        out[f"price_rolling_low_{w}"] = rolling_low
        out[f"price_dist_from_high_{w}"] = dist_high
        out[f"price_dist_from_low_{w}"] = dist_low
        out[f"price_vwap_distance_{w}"] = vwap_distance
        out[f"price_momentum_{w}"] = momentum
        out[f"price_acceleration_{w}"] = acceleration

        if bench_return is not None:
            beta, alpha, correlation = _rolling_regression(simple_return, bench_return, w)
            out[f"price_beta_{w}"] = beta
            out[f"price_alpha_{w}"] = alpha
            out[f"price_correlation_{w}"] = correlation

    return out


def _rolling_regression(
    returns: Series, bench_returns: Series, w: int
) -> tuple[Series, Series, Series]:
    n = len(returns)
    beta: Series = [None] * n
    alpha: Series = [None] * n
    correlation: Series = [None] * n
    # Real feeds have holes; require most of the window rather than all of it.
    min_obs = max(3, w // 2)
    for i in range(w, n):
        pairs: list[tuple[float, float]] = []
        for j in range(i - w + 1, i + 1):
            stock_return, bench_return = returns[j], bench_returns[j]
            if stock_return is not None and bench_return is not None:
                pairs.append((stock_return, bench_return))
        if len(pairs) < min_obs:
            continue
        ys = [p[0] for p in pairs]
        xs = [p[1] for p in pairs]
        mean_y = fmean(ys)
        mean_x = fmean(xs)
        var_x = fmean([(x - mean_x) ** 2 for x in xs])
        if var_x <= 0:
            continue
        cov = fmean([(x - mean_x) * (y - mean_y) for y, x in pairs])
        beta_value = cov / var_x
        beta[i] = beta_value
        alpha[i] = (mean_y - beta_value * mean_x) * 100
        var_y = fmean([(y - mean_y) ** 2 for y in ys])
        if var_y > 0:
            correlation[i] = cov / math.sqrt(var_x * var_y)
    return beta, alpha, correlation


# --- Engine -------------------------------------------------------------------------

class PriceFeatureEngine:
    """Runs the Chapter 3 pipeline: load raw candles -> calculate -> quality
    check -> store (online + offline) -> publish feature event."""

    name = ENGINE_NAME

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
        for definition in price_feature_definitions(
            self.windows,
            self.benchmark_symbol,
            calculation_frequency=f"{self._settings.feature_engine_interval}s",
        ):
            self.registry.register(definition)
        self.store = FeatureStore(session_factory=session_factory, cache=cache)

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
        since: datetime | None = None,
    ) -> list[FeatureValue]:
        values: list[FeatureValue] = []
        for feature_name, feature_series in series.items():
            definition = self.registry.get(feature_name)
            version = definition.version if definition else ENGINE_VERSION
            window = definition.window if definition else None
            for candle, value in zip(candles, feature_series, strict=True):
                if value is None or not math.isfinite(value):
                    continue
                if since is not None and candle.ts <= since:
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

    async def run(self, symbol: str, timeframe: str = "D") -> dict:
        candles = await self._load_candles(symbol, timeframe)
        if len(candles) < 2:
            logger.info(
                "price features skipped: not enough candles",
                extra={"symbol": symbol, "timeframe": timeframe, "candles": len(candles)},
            )
            return {"symbol": symbol, "timeframe": timeframe, "stored": 0, "skipped": True}

        benchmark: list[Candle] | None = None
        if symbol != self.benchmark_symbol:
            benchmark = await self._load_candles(self.benchmark_symbol, timeframe)

        series = compute_price_features(candles, benchmark, self.windows)
        since = await self.store.latest_ts(symbol, timeframe)
        values = self.build_values(symbol, timeframe, candles, series, since=since)
        quality = self._quality_check(values)
        stored = await self.store.write(values)
        await self._persist_run_metadata(symbol, timeframe, values, quality)

        if self._bus is not None and values:
            await self._bus.publish(
                Event(
                    type="feature.price.updated",
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
                        "price feature run failed",
                        extra={"symbol": symbol, "timeframe": timeframe, "error": str(exc)},
                    )
                    results.append({"symbol": symbol, "timeframe": timeframe, "error": str(exc)})
        return results

    def _quality_check(
        self, values: list[FeatureValue]
    ) -> dict[str, tuple[float, int]]:
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
