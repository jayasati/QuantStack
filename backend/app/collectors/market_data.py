"""Market data collectors (Volume 2, Prompts 2.2 and 2.3).

LiveMarketCollector polls quotes through the broker interface (REST polling;
WebSocket streaming becomes an upgrade inside the same collector). The
HistoricalCandleCollector backfills OHLCV candles with dedup and continuity
validation.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collectors.base import BaseCollector, CollectionError
from app.collectors.schema import CollectorCategory, CollectorOutput, Direction
from app.core.config import get_settings
from app.core.container import container
from app.database.tables import OhlcvCandle
from app.market.broker import BrokerInterface

INTERVAL_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1H": 60, "D": 1440}


class LiveMarketCollector(BaseCollector):
    """Collect LTP/OHLC/volume/VWAP/bid/ask/depth for the configured watchlist."""

    name = "live_market"
    category = CollectorCategory.MARKET_DATA
    source = "angel_one"
    interval_seconds = 15
    priority = 1
    requires_auth = True

    def __init__(self, broker: BrokerInterface | None = None) -> None:
        super().__init__()
        self._broker = broker
        self.symbols: list[str] = get_settings().watchlist

    @property
    def broker(self) -> BrokerInterface:
        if self._broker is None:
            self._broker = container.resolve(BrokerInterface)
        return self._broker

    async def authenticate(self) -> None:
        if not await self.broker.is_connected():
            await self.broker.connect()
        if not await self.broker.is_connected():
            raise CollectionError("broker is not connected (credentials missing?)")

    async def collect(self) -> list[CollectorOutput]:
        records: list[CollectorOutput] = []
        for symbol in self.symbols:
            quote = await self.broker.get_quote(symbol)
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
                    instrument=quote.symbol,
                    exchange=quote.exchange,
                    raw_value=quote.last_price,
                    normalized_value=quote.last_price,
                    direction=direction,
                    confidence=0.9,
                    metadata={
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
        return records


class HistoricalCandleCollector(BaseCollector):
    """Backfill OHLCV candles per timeframe with dedup and continuity checks."""

    name = "historical_candles"
    category = CollectorCategory.MARKET_DATA
    source = "angel_one"
    interval_seconds = 300
    priority = 5
    requires_auth = True

    intervals = ("1m", "5m", "15m", "1H", "D")
    lookback = {
        "1m": timedelta(days=1),
        "5m": timedelta(days=5),
        "15m": timedelta(days=10),
        "1H": timedelta(days=30),
        "D": timedelta(days=365),
    }

    def __init__(
        self,
        broker: BrokerInterface | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        super().__init__()
        self._broker = broker
        self._session_factory = session_factory
        self.symbols: list[str] = get_settings().watchlist
        self.gaps_detected = 0

    @property
    def broker(self) -> BrokerInterface:
        if self._broker is None:
            self._broker = container.resolve(BrokerInterface)
        return self._broker

    async def authenticate(self) -> None:
        if not await self.broker.is_connected():
            await self.broker.connect()
        if not await self.broker.is_connected():
            raise CollectionError("broker is not connected (credentials missing?)")

    async def collect(self) -> list[CollectorOutput]:
        records: list[CollectorOutput] = []
        end = datetime.now(UTC)
        for symbol in self.symbols:
            for interval in self.intervals:
                candles = await self.broker.get_historical(
                    symbol, interval, end - self.lookback[interval], end
                )
                if not candles:
                    continue
                gaps = self._validate_continuity(
                    [c.timestamp for c in candles], INTERVAL_MINUTES[interval]
                )
                self.gaps_detected += gaps
                await self._store_candles(candles)
                records.append(
                    CollectorOutput(
                        collector_name=self.name,
                        collector_category=self.category,
                        source=self.source,
                        instrument=symbol,
                        raw_value=len(candles),
                        normalized_value=float(candles[-1].close),
                        confidence=0.95 if gaps == 0 else 0.7,
                        metadata={
                            "interval": interval,
                            "bars": len(candles),
                            "gaps": gaps,
                            "first": candles[0].timestamp.isoformat(),
                            "last": candles[-1].timestamp.isoformat(),
                        },
                    )
                )
        return records

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

    async def _store_candles(self, candles: list) -> None:
        """Upsert candles — duplicate bars are silently ignored."""
        if self._session_factory is None:
            return
        rows = [
            {
                "symbol": c.symbol,
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
        async with self._session_factory() as session:
            statement = pg_insert(OhlcvCandle).values(rows)
            statement = statement.on_conflict_do_nothing(
                index_elements=["symbol", "timeframe", "ts"]
            )
            await session.execute(statement)
            await session.commit()
