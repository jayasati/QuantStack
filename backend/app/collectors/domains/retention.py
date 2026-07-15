"""Candle retention / pruning (2026-07-16).

Nothing has ever pruned `ohlcv_candles` before this -- rows accumulate
forever. Deletes candles older than their own interval's backfill lookback
window (the same table HistoricalCandleCollector.default_lookback already
encodes): a 1m bar older than 2 days, a 1H bar older than 60 days, etc. are
never fetched again by the backfill sweep and never queried by any feature
engine's own lookback window, so they're dead weight once they age out.

Runs after hours only -- pruning during market hours would just compete
with live writes for no benefit; nothing about "how old is too old" changes
based on whether the market happens to be open right now.
"""

from datetime import UTC, datetime

from sqlalchemy import delete

from app.collectors.base import BaseCollector, CollectionError
from app.collectors.market_data import HistoricalCandleCollector
from app.collectors.schema import CollectorCategory, CollectorOutput
from app.database.tables import OhlcvCandle


class CandleRetentionCollector(BaseCollector):
    name = "candle_retention"
    category = CollectorCategory.MARKET_DATA
    source = "internal"
    interval_seconds = 3600
    priority = 50
    after_hours_only = True

    def __init__(self, session_factory=None) -> None:
        super().__init__()
        self._session_factory = session_factory

    def _sessions(self):
        if self._session_factory is None:
            try:
                from app.database.session import get_session_factory

                self._session_factory = get_session_factory()
            except Exception:
                return None
        return self._session_factory

    async def collect(self) -> list[CollectorOutput]:
        sessions = self._sessions()
        if sessions is None:
            raise CollectionError("no database session factory available")

        now = datetime.now(UTC)
        records: list[CollectorOutput] = []
        async with sessions() as session:
            for interval, lookback in HistoricalCandleCollector.default_lookback.items():
                cutoff = now - lookback
                result = await session.execute(
                    delete(OhlcvCandle).where(
                        OhlcvCandle.timeframe == interval, OhlcvCandle.ts < cutoff
                    )
                )
                deleted = result.rowcount or 0
                records.append(
                    CollectorOutput(
                        collector_name=self.name,
                        collector_category=self.category,
                        source=self.source,
                        instrument=interval,
                        raw_value=deleted,
                        normalized_value=float(deleted),
                        metadata={
                            "interval": interval,
                            "cutoff": cutoff.isoformat(),
                            "rows_deleted": deleted,
                        },
                    )
                )
            await session.commit()
        return records
