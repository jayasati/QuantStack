"""Market data collectors (Volume 2, Prompts 2.2 and 2.3).

LiveMarketCollector streams quotes over the SmartAPI WebSocket feed and
falls back to REST polling per symbol whenever the stream is disconnected
or stale; raw ticks are persisted separately from aggregated candles. The
HistoricalCandleCollector backfills OHLCV candles for all seven timeframes,
resuming from the last stored bar, with dedup and continuity validation.
"""

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.market.angel_ws import AngelWebSocketFeed

from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collectors.base import BaseCollector, CollectionError
from app.collectors.schema import CollectorCategory, CollectorOutput, Direction
from app.core.config import get_settings
from app.core.container import container
from app.database.tables import OhlcvCandle, RawTick
from app.market.broker import BrokerInterface, Candle
from app.market.instruments import InstrumentService

INTERVAL_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1H": 60, "D": 1440}


class _BrokerBackedCollector(BaseCollector):
    """Shared broker/instrument plumbing for market data collectors."""

    def __init__(
        self,
        broker: BrokerInterface | None = None,
        instruments: InstrumentService | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        super().__init__()
        self._broker = broker
        self._instruments = instruments or InstrumentService()
        self._session_factory = session_factory
        self.symbols: list[str] = get_settings().watchlist
        self._tokens: dict[str, tuple[str, str, str]] = {}

    @property
    def broker(self) -> BrokerInterface:
        if self._broker is None:
            self._broker = container.resolve(BrokerInterface)
        return self._broker

    def _sessions(self) -> async_sessionmaker[AsyncSession] | None:
        if self._session_factory is None:
            try:
                from app.database.session import get_session_factory

                self._session_factory = get_session_factory()
            except Exception:
                return None
        return self._session_factory

    async def initialize(self) -> None:
        for symbol in self.symbols:
            try:
                self._tokens[symbol] = self._instruments.resolve(symbol)
            except Exception as exc:
                self.logger.warning(
                    "could not resolve instrument",
                    extra={"symbol": symbol, "error": str(exc)},
                )

    async def authenticate(self) -> None:
        if not await self.broker.is_connected():
            await self.broker.connect()
            self.health.extras["reconnect_count"] = (
                self.health.extras.get("reconnect_count", 0) + 1
            )
        if not await self.broker.is_connected():
            raise CollectionError("broker is not connected (credentials missing?)")


class LiveMarketCollector(_BrokerBackedCollector):
    """LTP/OHLC/volume/VWAP/bid/ask/depth for the watchlist, plus raw ticks.

    Streams via the SmartAPI WebSocket feed when enabled and connected;
    any symbol whose stream is missing or stale falls back to REST polling.
    """

    name = "live_market"
    category = CollectorCategory.MARKET_DATA
    source = "angel_one"
    interval_seconds = 15
    priority = 1
    requires_auth = True

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._feed: AngelWebSocketFeed | None = None

    async def authenticate(self) -> None:
        await super().authenticate()
        if get_settings().enable_websocket and self._feed is None:
            await self._start_feed()

    async def _start_feed(self) -> None:
        credentials = getattr(self.broker, "stream_credentials", lambda: None)()
        if not credentials:
            self.logger.warning("websocket enabled but stream credentials unavailable")
            return
        from app.market.angel_ws import AngelWebSocketFeed

        feed = AngelWebSocketFeed(**credentials)
        for token, exchange, _ in self._tokens.values():
            feed.subscribe(exchange, token)
        await feed.start()
        self._feed = feed
        self.logger.info("websocket feed started")

    async def cleanup(self) -> None:
        if self._feed is not None:
            await self._feed.stop()
            self._feed = None

    def _record_from_stream(
        self, symbol: str, token: str, exchange: str, trading_symbol: str
    ) -> tuple[CollectorOutput, dict] | None:
        if self._feed is None or not self._feed.connected:
            return None
        tick = self._feed.latest(token, max_age_seconds=self.interval_seconds * 2)
        if tick is None or "close" not in tick:
            return None
        ltp = tick["ltp"]
        close = tick.get("close") or None
        direction = Direction.NEUTRAL
        if close:
            direction = Direction.BULLISH if ltp >= close else Direction.BEARISH
        record = CollectorOutput(
            collector_name=self.name,
            collector_category=self.category,
            source="angel_one_ws",
            instrument=symbol,
            exchange=exchange,
            raw_value=ltp,
            normalized_value=ltp,
            direction=direction,
            confidence=0.95,
            freshness_seconds=max(0.0, time.time() - tick["received_at"]),
            metadata={
                "transport": "websocket",
                "trading_symbol": trading_symbol,
                "open": tick.get("open"),
                "high": tick.get("high"),
                "low": tick.get("low"),
                "close": close,
                "volume": tick.get("volume"),
                "vwap": tick.get("avg_traded_price"),
                "total_buy_qty": tick.get("total_buy_quantity"),
                "total_sell_qty": tick.get("total_sell_quantity"),
                "sequence": tick.get("sequence"),
            },
        )
        raw_tick = {
            "symbol": symbol,
            "ts": datetime.now(UTC),
            "ltp": ltp,
            "data": {"volume": tick.get("volume"), "transport": "websocket"},
        }
        return record, raw_tick

    async def collect(self) -> list[CollectorOutput]:
        if not self._tokens:
            raise CollectionError("no instruments resolved for watchlist")
        records: list[CollectorOutput] = []
        ticks: list[dict] = []
        dropped = 0
        streamed = 0
        for symbol, (token, exchange, trading_symbol) in self._tokens.items():
            from_stream = self._record_from_stream(symbol, token, exchange, trading_symbol)
            if from_stream is not None:
                record, raw_tick = from_stream
                records.append(record)
                ticks.append(raw_tick)
                streamed += 1
                continue
            try:
                quote = await self.broker.get_quote(token, exchange=exchange)
            except Exception as exc:
                dropped += 1
                self.logger.warning(
                    "quote fetch failed",
                    extra={"symbol": symbol, "error": str(exc)},
                )
                continue
            spread = (
                (quote.ask - quote.bid)
                if quote.ask is not None and quote.bid is not None
                else None
            )
            direction = Direction.NEUTRAL
            if quote.close:
                direction = (
                    Direction.BULLISH if quote.last_price >= quote.close else Direction.BEARISH
                )
            records.append(
                CollectorOutput(
                    collector_name=self.name,
                    collector_category=self.category,
                    source=self.source,
                    instrument=symbol,
                    exchange=exchange,
                    raw_value=quote.last_price,
                    normalized_value=quote.last_price,
                    direction=direction,
                    confidence=0.9,
                    metadata={
                        "trading_symbol": trading_symbol,
                        "open": quote.open,
                        "high": quote.high,
                        "low": quote.low,
                        "close": quote.close,
                        "volume": quote.volume,
                        "vwap": quote.vwap,
                        "bid": quote.bid,
                        "ask": quote.ask,
                        "bid_qty": quote.bid_qty,
                        "ask_qty": quote.ask_qty,
                        "spread": spread,
                        "depth": quote.depth,
                    },
                )
            )
            ticks.append(
                {
                    "symbol": symbol,
                    "ts": quote.timestamp,
                    "ltp": quote.last_price,
                    "data": {"volume": quote.volume, "bid": quote.bid, "ask": quote.ask},
                }
            )
        self.health.extras["dropped_packets"] = (
            self.health.extras.get("dropped_packets", 0) + dropped
        )
        self.health.extras["streamed_symbols"] = streamed
        if self._feed is not None:
            self.health.extras["ws_connected"] = self._feed.connected
            self.health.extras["ws_packets"] = self._feed.metrics.packets
            self.health.extras["ws_reconnects"] = self._feed.metrics.reconnects
        await self._persist_ticks(ticks)
        if not records:
            raise CollectionError(f"all {len(self._tokens)} quote fetches failed")
        return records

    async def _persist_ticks(self, ticks: list[dict]) -> None:
        """Raw ticks are persisted separately from aggregated candles."""
        sessions = self._sessions()
        if sessions is None or not ticks:
            return
        try:
            async with sessions() as session:
                await session.execute(insert(RawTick), ticks)
                await session.commit()
        except Exception as exc:
            self.logger.error("failed to persist raw ticks", extra={"error": str(exc)})


class HistoricalCandleCollector(_BrokerBackedCollector):
    """Backfill OHLCV candles for all timeframes with dedup and continuity checks."""

    name = "historical_candles"
    category = CollectorCategory.MARKET_DATA
    source = "angel_one"
    interval_seconds = 300
    priority = 5
    requires_auth = True

    intervals = ("1m", "3m", "5m", "15m", "30m", "1H", "D")
    default_lookback = {
        "1m": timedelta(days=2),
        "3m": timedelta(days=5),
        "5m": timedelta(days=10),
        "15m": timedelta(days=20),
        "30m": timedelta(days=30),
        "1H": timedelta(days=60),
        "D": timedelta(days=365 * 2),
    }

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.gaps_detected = 0

    async def collect(self) -> list[CollectorOutput]:
        if not self._tokens:
            raise CollectionError("no instruments resolved for watchlist")
        records: list[CollectorOutput] = []
        end = datetime.now(UTC)
        for symbol, (token, exchange, _) in self._tokens.items():
            for interval in self.intervals:
                start = await self._backfill_start(symbol, interval, end)
                if start >= end:
                    continue
                try:
                    candles = await self.broker.get_historical(
                        token, interval, start, end, exchange=exchange
                    )
                except Exception as exc:
                    self.logger.warning(
                        "candle fetch failed",
                        extra={"symbol": symbol, "interval": interval, "error": str(exc)},
                    )
                    continue
                if not candles:
                    continue
                gaps = self._validate_continuity(
                    [c.timestamp for c in candles], INTERVAL_MINUTES[interval]
                )
                self.gaps_detected += gaps
                stored = await self._store_candles(symbol, candles)
                records.append(
                    CollectorOutput(
                        collector_name=self.name,
                        collector_category=self.category,
                        source=self.source,
                        instrument=symbol,
                        exchange=exchange,
                        raw_value=len(candles),
                        normalized_value=float(candles[-1].close),
                        confidence=0.95 if gaps == 0 else 0.7,
                        metadata={
                            "interval": interval,
                            "bars_fetched": len(candles),
                            "bars_stored": stored,
                            "gaps": gaps,
                            "from": candles[0].timestamp.isoformat(),
                            "to": candles[-1].timestamp.isoformat(),
                        },
                    )
                )
        if not records:
            raise CollectionError("no candles fetched for any symbol/timeframe")
        return records

    async def _backfill_start(self, symbol: str, interval: str, end: datetime) -> datetime:
        """Resume from the last stored bar; fall back to the default lookback."""
        default_start = end - self.default_lookback[interval]
        sessions = self._sessions()
        if sessions is None:
            return default_start
        try:
            async with sessions() as session:
                result = await session.execute(
                    select(func.max(OhlcvCandle.ts)).where(
                        OhlcvCandle.symbol == symbol, OhlcvCandle.timeframe == interval
                    )
                )
                last_ts = result.scalar()
            if last_ts is None:
                return default_start
            return max(last_ts, default_start)
        except Exception:
            return default_start

    @staticmethod
    def _validate_continuity(timestamps: list[datetime], step_minutes: int) -> int:
        """Count gaps larger than the expected bar interval (ignoring overnight)."""
        gaps = 0
        expected = timedelta(minutes=step_minutes)
        for prev, current in zip(timestamps, timestamps[1:], strict=False):
            delta = current - prev
            if expected < delta < timedelta(hours=16):
                gaps += 1
        return gaps

    async def _store_candles(self, symbol: str, candles: list[Candle]) -> int:
        """Upsert candles — duplicate bars are silently ignored."""
        sessions = self._sessions()
        if sessions is None:
            return 0
        rows = [
            {
                "symbol": symbol,
                "timeframe": c.interval,
                "ts": c.timestamp,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ]
        async with sessions() as session:
            statement = pg_insert(OhlcvCandle).values(rows)
            statement = statement.on_conflict_do_nothing(
                index_elements=["symbol", "timeframe", "ts"]
            )
            result = await session.execute(statement)
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0)
