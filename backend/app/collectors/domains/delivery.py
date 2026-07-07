"""NSE delivery-percentage collector (Volume 3, Prompt 3.4 support).

Collects security-wise delivery data for the tradeable (non-index) watchlist
symbols from NSE's daily bhavcopy archive and persists it through the
standard pipeline into market_events. The liquidity feature engine joins
these observations by session date to produce liquidity_delivery_pct.

First run (no stored delivery history) scans the last BACKFILL_DAYS calendar
days so the feature starts with real history; subsequent runs pick up the
most recent published session only. Index symbols have no delivery concept
and are skipped.
"""

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.collectors.base import BaseCollector, CollectionError
from app.collectors.schema import CollectorCategory, CollectorOutput, Direction
from app.core.config import get_settings
from app.market.instruments import INDEX_TOKENS

IST = ZoneInfo("Asia/Kolkata")


class DeliveryCollector(BaseCollector):
    name = "nse_delivery"
    category = CollectorCategory.MARKET_DATA
    source = "nse"
    # Bhavcopy publishes end-of-day; hourly polling catches it soon after.
    interval_seconds = 3600
    priority = 7
    requires_auth = False

    BACKFILL_DAYS = 30
    RECENT_SCAN_DAYS = 5

    def __init__(
        self,
        delivery_source: Any = None,
        symbols: list[str] | None = None,
        session_factory: Any = None,
    ) -> None:
        super().__init__()
        if delivery_source is None:
            from app.collectors.sources.nse_delivery import NseDeliverySource

            delivery_source = NseDeliverySource()
        self._source = delivery_source
        self._symbols = symbols
        self._session_factory = session_factory

    @property
    def symbols(self) -> list[str]:
        if self._symbols is not None:
            return self._symbols
        return [s for s in get_settings().watchlist if s.upper() not in INDEX_TOKENS]

    async def collect(self) -> list[CollectorOutput]:
        symbols = {s.upper() for s in self.symbols}
        if not symbols:
            raise CollectionError("no tradeable (non-index) symbols in watchlist")

        backfilling = not await self._has_history()
        scan_days = self.BACKFILL_DAYS if backfilling else self.RECENT_SCAN_DAYS
        today = datetime.now(IST).date()

        records: list[CollectorOutput] = []
        sessions_found = 0
        for offset in range(scan_days):
            day = today - timedelta(days=offset)
            try:
                data = await self._source.fetch_day(day, symbols)
            except Exception as exc:
                self.logger.warning(
                    "bhavcopy fetch failed",
                    extra={"date": day.isoformat(), "error": str(exc)},
                )
                continue
            if not data:
                continue  # weekend, holiday, or not yet published
            sessions_found += 1
            for symbol, item in sorted(data.items()):
                records.append(self._record(symbol, item))
            if not backfilling:
                break  # incremental mode: latest published session is enough
        if not records:
            raise CollectionError(
                f"no delivery data found in the last {scan_days} days "
                f"for {sorted(symbols)}"
            )
        self.health.extras["sessions_found"] = sessions_found
        self.health.extras["backfill_mode"] = backfilling
        return records

    def _record(self, symbol: str, item: dict[str, Any]) -> CollectorOutput:
        position_date = item.get("position_date")
        return CollectorOutput(
            collector_name=self.name,
            collector_category=self.category,
            source=self.source,
            instrument=symbol,
            exchange="NSE",
            raw_value=item["delivery_pct"],
            normalized_value=item["delivery_pct"],
            direction=Direction.NEUTRAL,
            confidence=0.9,
            metadata={
                "delivery_pct": item["delivery_pct"],
                "traded_qty": item["traded_qty"],
                "delivered_qty": item["delivered_qty"],
                "position_date": position_date.isoformat() if position_date else None,
            },
        )

    async def _has_history(self) -> bool:
        """True when delivery observations already exist in market_events."""
        sessions = self._sessions()
        if sessions is None:
            return False
        try:
            from app.database.tables import MarketEvent

            async with sessions() as session:
                count = (
                    await session.execute(
                        select(func.count())
                        .select_from(MarketEvent)
                        .where(MarketEvent.source == self.name)
                    )
                ).scalar()
            return bool(count)
        except Exception:
            return False

    def _sessions(self) -> Any:
        if self._session_factory is None:
            try:
                from app.database.session import get_session_factory

                self._session_factory = get_session_factory()
            except Exception:
                return None
        return self._session_factory

    async def cleanup(self) -> None:
        closer = getattr(self._source, "close", None)
        if closer is not None:
            await closer()
