"""Market data collectors (Volume 2, Prompts 2.2 and 2.3).

LiveMarketCollector streams quotes over the SmartAPI WebSocket feed and
falls back to REST polling per symbol whenever the stream is disconnected
or stale; raw ticks are persisted separately from aggregated candles. The
HistoricalCandleCollector backfills OHLCV candles for all seven timeframes,
resuming from the last stored bar, with dedup and continuity validation.
"""

import time
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from app.collectors.sources.bse_candles import BseCandleSource
    from app.collectors.sources.nse_candles import NseCandleSource
    from app.collectors.sources.yahoo_history import YahooDailyHistory
    from app.collectors.tick_aggregator import TickCandleAggregator
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

IST = ZoneInfo("Asia/Kolkata")


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

    Also carries the India VIX index token alongside the tradable watchlist.
    VixCollector only backfills daily candles (the broker gives at most a
    couple of daily bars), so an intraday VIX spike is otherwise invisible
    until end of day; this gives VIX the same 15s live tick as everything
    else here, for free, off the same WS/REST fallback path.
    """

    name = "live_market"
    category = CollectorCategory.MARKET_DATA
    source = "angel_one"
    interval_seconds = 15
    priority = 1
    requires_auth = True
    market_hours_only = True  # quotes/LTP freeze the instant the market closes

    def __init__(
        self, tick_aggregator: "TickCandleAggregator | None" = None, **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self._feed: AngelWebSocketFeed | None = None
        self._tick_aggregator = tick_aggregator
        vix_symbol = get_settings().feature_vix_symbol
        if vix_symbol not in self.symbols:
            self.symbols = [*self.symbols, vix_symbol]

    def _get_tick_aggregator(self) -> "TickCandleAggregator":
        if self._tick_aggregator is None:
            from app.collectors.tick_aggregator import TickCandleAggregator

            self._tick_aggregator = TickCandleAggregator(self._sessions())
        return self._tick_aggregator

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
        await self._get_tick_aggregator().ingest_batch(ticks)
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
    """Backfill OHLCV candles for all timeframes with dedup and continuity checks.

    Multi-source fallback (2026-07-15, DEBT-2): the broker's own
    historical-candle backend can silently lag real time for hours (found
    live -- Angel One's candle pipeline fell ~5.5h behind while its live
    tick feed stayed fresh; zero exceptions, zero empty-result signal until
    logging was added). For TODAY's data specifically (the common,
    steady-state case, and the exact scenario that broke), NSE/BSE's own
    public quote-page tick feeds are tried FIRST -- they're a different
    system from the broker's aggregated-candle backend, so they don't share
    its failure mode -- falling through to the broker's own fetch, then to
    Yahoo Finance as a last resort. Genuine multi-day backfill (e.g. after
    real downtime) skips straight to broker-then-Yahoo: NSE/BSE's public
    feeds only ever expose *today's* session, they cannot serve older days
    (verified live -- NSE's old historical-range endpoints are dead, and
    there is no equivalent BSE range endpoint either).
    """

    name = "historical_candles"
    category = CollectorCategory.MARKET_DATA
    source = "angel_one"
    interval_seconds = 300
    priority = 5
    requires_auth = True
    # No new intraday bars form once the market's shut; `_backfill_start`'s
    # resume-tracking already makes a caught-up run cheap (DB-only, no
    # network call), but this still means zero pointless scheduled runs
    # instead of many cheap ones. Inherited by VixCollector and
    # ReferenceIndexCollector -- their Yahoo deep-backfill path (unrelated
    # to NSE session timing) is also gated by this, which only delays a
    # first-time deep backfill to the next market session, never blocks it.
    market_hours_only = True

    intervals: tuple[str, ...] = ("1m", "3m", "5m", "15m", "30m", "1H", "D")
    default_lookback = {
        "1m": timedelta(days=2),
        "3m": timedelta(days=5),
        "5m": timedelta(days=10),
        "15m": timedelta(days=20),
        "30m": timedelta(days=30),
        "1H": timedelta(days=60),
        "D": timedelta(days=365 * 2),
    }

    def __init__(
        self,
        nse_candles: "NseCandleSource | None" = None,
        bse_candles: "BseCandleSource | None" = None,
        yahoo: "YahooDailyHistory | None" = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.gaps_detected = 0
        self._nse_candles = nse_candles
        self._bse_candles = bse_candles
        self._yahoo = yahoo

    def _get_nse_candles(self) -> "NseCandleSource":
        if self._nse_candles is None:
            from app.collectors.sources.nse_candles import NseCandleSource

            self._nse_candles = NseCandleSource()
        return self._nse_candles

    def _get_bse_candles(self) -> "BseCandleSource":
        if self._bse_candles is None:
            from app.collectors.sources.bse_candles import BseCandleSource

            self._bse_candles = BseCandleSource()
        return self._bse_candles

    def _get_yahoo(self) -> "YahooDailyHistory":
        if self._yahoo is None:
            from app.collectors.sources.yahoo_history import YahooDailyHistory

            self._yahoo = YahooDailyHistory()
        return self._yahoo

    async def cleanup(self) -> None:
        for source in (self._nse_candles, self._bse_candles, self._yahoo):
            closer = getattr(source, "close", None)
            if closer is not None:
                await closer()

    async def collect(self) -> list[CollectorOutput]:
        if not self._tokens:
            raise CollectionError("no instruments resolved for watchlist")
        records: list[CollectorOutput] = []
        end = datetime.now(UTC)
        today_ist = end.astimezone(IST).date()
        for symbol, (token, exchange, _) in self._tokens.items():
            for interval in self.intervals:
                start = await self._backfill_start(symbol, interval, end)
                if start >= end:
                    self.logger.debug(
                        "candle fetch skipped: already caught up",
                        extra={
                            "symbol": symbol, "interval": interval,
                            "start": start.isoformat(), "end": end.isoformat(),
                        },
                    )
                    continue
                candles = await self._fetch_with_fallback(
                    symbol, interval, start, end, token, exchange, today_ist
                )
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

    async def _fetch_with_fallback(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        token: str,
        exchange: str,
        today_ist: date,
    ) -> list[Candle]:
        """Try each source in order, using the first one that returns any
        candles. NSE/BSE only ever apply to an intraday, today-only window
        (they cannot serve older days -- see the class docstring); every
        other window goes broker-then-Yahoo, matching the collector's
        original behavior with the exception-vs-empty-result distinction
        (findings 15/16 of perf-audit-2026-07-14, extended 2026-07-15)."""
        is_today_only = (
            interval != "D"
            and start.astimezone(IST).date() == today_ist
            and end.astimezone(IST).date() == today_ist
        )

        sources: list[tuple[str, object]] = []
        if is_today_only:
            from app.collectors.sources.bse_candles import BSE_INDEX_CODES

            if symbol.upper() in BSE_INDEX_CODES:
                sources.append(("bse", lambda: self._get_bse_candles().fetch_today(symbol, interval)))
            else:
                # Every non-BSE-listed symbol is assumed NSE-native
                # (equities and NSE's own indices) -- fetch_today() itself
                # returns [] harmlessly if that assumption is ever wrong
                # for a future watchlist symbol.
                sources.append(("nse", lambda: self._get_nse_candles().fetch_today(symbol, interval)))
        sources.append((
            "angel_one",
            lambda: self.broker.get_historical(token, interval, start, end, exchange=exchange),
        ))
        if interval != "D":
            sources.append((
                "yahoo",
                lambda: self._get_yahoo().fetch_intraday(symbol, interval),
            ))

        for source_name, fetch in sources:
            try:
                candles = await fetch()
            except Exception as exc:
                self.logger.warning(
                    "candle fetch failed",
                    extra={
                        "symbol": symbol, "interval": interval,
                        "source": source_name, "error": str(exc),
                    },
                )
                continue
            if candles:
                return candles
            # A source that succeeds (no exception) but returns zero bars
            # was previously silent here -- no log, no metric -- which is
            # exactly why intraday candle collection stalling for hours
            # (DEBT-2's root cause, 2026-07-15) went undetected until a
            # manual DB query found it.
            self.logger.warning(
                "candle fetch returned no bars",
                extra={
                    "symbol": symbol, "interval": interval, "source": source_name,
                    "requested_from": start.isoformat(), "requested_to": end.isoformat(),
                },
            )
        return []

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


class VixCollector(HistoricalCandleCollector):
    """Backfill India VIX candles into ohlcv_candles (Volume 3 support).

    Reuses the HistoricalCandleCollector resume/dedup/continuity machinery,
    pointed at the configured VIX index symbol instead of the watchlist. The
    volatility engine's vix-distance features join these candles by timestamp,
    so implied volatility goes live once this collector runs.

    Angel One serves only a couple of daily VIX bars, so while the stored
    daily history is thinner than DAILY_BACKFILL_TARGET each run also pulls a
    deep daily backfill from Yahoo (^INDIAVIX); the shared upsert keeps the
    broker's bars authoritative where the two overlap. Once the target depth
    is reached the Yahoo path stays dormant.
    """

    name = "vix"
    category = CollectorCategory.MARKET_DATA
    source = "angel_one"
    interval_seconds = 300
    priority = 6
    requires_auth = True

    DAILY_BACKFILL_TARGET = 200

    def __init__(self, yahoo: "YahooDailyHistory | None" = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.symbols = [get_settings().feature_vix_symbol]
        self._yahoo = yahoo

    def _yahoo_ticker(self, symbol: str) -> str | None:
        """Yahoo ticker used for the deep backfill; None disables it."""
        from app.collectors.sources.yahoo_history import yahoo_ticker

        return yahoo_ticker(symbol)

    async def collect(self) -> list[CollectorOutput]:
        records: list[CollectorOutput] = []
        broker_error: CollectionError | None = None
        try:
            records = await super().collect()
        except CollectionError as exc:
            broker_error = exc
        for symbol in self.symbols:
            backfill = await self._deep_backfill_daily(symbol)
            if backfill is not None:
                records.append(backfill)
        if not records:
            raise broker_error or CollectionError("no data from broker or Yahoo")
        return records

    async def _daily_bar_count(self, symbol: str) -> int | None:
        sessions = self._sessions()
        if sessions is None:
            return None
        async with sessions() as session:
            result = await session.execute(
                select(func.count())
                .select_from(OhlcvCandle)
                .where(
                    OhlcvCandle.symbol == symbol,
                    OhlcvCandle.timeframe == "D",
                )
            )
            return int(result.scalar() or 0)

    async def _deep_backfill_daily(self, symbol: str) -> CollectorOutput | None:
        count = await self._daily_bar_count(symbol)
        if count is None or count >= self.DAILY_BACKFILL_TARGET:
            return None
        ticker = self._yahoo_ticker(symbol)
        if ticker is None:
            return None
        if self._yahoo is None:
            from app.collectors.sources.yahoo_history import YahooDailyHistory

            self._yahoo = YahooDailyHistory()
        try:
            candles = await self._yahoo.fetch_daily(ticker)
        except Exception as exc:
            self.logger.warning(
                "yahoo daily backfill failed",
                extra={"symbol": symbol, "ticker": ticker, "error": str(exc)},
            )
            return None
        if not candles:
            return None
        stored = await self._store_candles(symbol, candles)
        self.logger.info(
            "deep daily backfill from yahoo",
            extra={"symbol": symbol, "bars_fetched": len(candles), "bars_stored": stored},
        )
        return CollectorOutput(
            collector_name=self.name,
            collector_category=self.category,
            source="yahoo",
            instrument=symbol,
            exchange="NSE",
            raw_value=len(candles),
            normalized_value=float(candles[-1].close),
            confidence=0.85,  # secondary source, slightly below broker data
            metadata={
                "interval": "D",
                "bars_fetched": len(candles),
                "bars_stored": stored,
                "provider": "yahoo_deep_backfill",
                "from": candles[0].timestamp.isoformat(),
                "to": candles[-1].timestamp.isoformat(),
            },
        )


class ReferenceIndexCollector(VixCollector):
    """Daily candles for cross-reference indices: SENSEX plus every sector
    index the relative-strength engine compares stocks against (Prompt 3.8).

    Sector symbols store under their sector names (Energy, Banking, IT, ...),
    resolved to broker tokens via the sector source's token map; deep daily
    history comes from Yahoo while stored history is thin, exactly like the
    VIX collector.
    """

    name = "reference_indices"
    interval_seconds = 3600
    priority = 8

    intervals = ("D",)  # references feed daily features only
    default_lookback = {"D": timedelta(days=365 * 2)}

    def __init__(self, yahoo: "YahooDailyHistory | None" = None, **kwargs) -> None:
        super().__init__(yahoo=yahoo, **kwargs)
        settings = get_settings()
        sectors = sorted(
            set(settings.feature_stock_sectors.values())
            | set(settings.feature_stock_industries.values())
        )
        self.symbols = [settings.feature_sensex_symbol, *sectors]

    async def initialize(self) -> None:
        from app.collectors.sources.broker_sectors import SECTOR_TOKENS

        for symbol in self.symbols:
            if symbol in SECTOR_TOKENS:
                token, exchange = SECTOR_TOKENS[symbol]
                self._tokens[symbol] = (token, exchange, symbol)
                continue
            try:
                self._tokens[symbol] = self._instruments.resolve(symbol)
            except Exception as exc:
                self.logger.warning(
                    "could not resolve reference index",
                    extra={"symbol": symbol, "error": str(exc)},
                )

    def _yahoo_ticker(self, symbol: str) -> str | None:
        from app.collectors.sources.broker_sectors import YAHOO_SECTOR_TICKERS
        from app.collectors.sources.yahoo_history import yahoo_ticker

        ticker = YAHOO_SECTOR_TICKERS.get(symbol)
        return ticker if ticker is not None else yahoo_ticker(symbol)
