"""Historical Replay Engine (Volume 3, Prompt 3.17).

Reconstructs the platform's exact feature state at any past moment: for a
given timestamp, every feature's value is the latest observation with
ts <= as_of — never anything later, which is the whole look-ahead guarantee.
Because the feature store is append-only and versioned, the same as_of always
reproduces the same state.

Two access patterns:
- replay(): one point-in-time snapshot (signal review, SHAP analysis, or
  debugging "what did the model see?").
- replay_matrix(): a walk-forward series of snapshots over many timestamps
  in one query (training and backtesting) — rows are streamed in time order
  and each snapshot only ever sees observations at or before its timestamp.
"""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession] | async_sessionmaker[AsyncSession]

ReplayRow = tuple[str, datetime, float, str]  # feature, ts, value, version


def walk_forward_snapshots(
    rows: Sequence[ReplayRow],
    timestamps: Sequence[datetime],
) -> list[dict[str, dict[str, Any]]]:
    """Pure walk-forward reconstruction.

    `rows` must be sorted by ts ascending; `timestamps` are the snapshot
    moments (also ascending). Each snapshot contains, per feature, the last
    observation with ts <= snapshot time — future rows are structurally
    unreachable.
    """
    snapshots: list[dict[str, dict[str, Any]]] = []
    state: dict[str, dict[str, Any]] = {}
    row_index = 0
    ordered = sorted(timestamps)
    for snapshot_ts in ordered:
        while row_index < len(rows) and rows[row_index][1] <= snapshot_ts:
            feature_name, ts, value, version = rows[row_index]
            state[feature_name] = {
                "value": value,
                "ts": ts.isoformat(),
                "version": version,
            }
            row_index += 1
        snapshots.append({name: dict(entry) for name, entry in state.items()})
    return snapshots


class HistoricalReplayEngine:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._sessions = session_factory

    async def replay(
        self,
        symbol: str,
        as_of: datetime,
        timeframe: str = "D",
        feature_names: Sequence[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Feature state exactly as it existed at `as_of` (ts <= as_of)."""
        from app.database.tables import FeatureStoreRow

        query = (
            select(FeatureStoreRow)
            .where(
                FeatureStoreRow.symbol == symbol,
                FeatureStoreRow.timeframe == timeframe,
                FeatureStoreRow.ts <= as_of,
            )
            .distinct(FeatureStoreRow.feature_name)
            .order_by(FeatureStoreRow.feature_name, FeatureStoreRow.ts.desc())
        )
        if feature_names:
            query = query.where(FeatureStoreRow.feature_name.in_(list(feature_names)))
        async with self._sessions() as session:
            rows = (await session.execute(query)).scalars().all()
        return {
            row.feature_name: {
                "value": row.value,
                "ts": row.ts.isoformat(),
                "version": row.feature_version,
            }
            for row in rows
        }

    async def replay_matrix(
        self,
        symbol: str,
        timestamps: Sequence[datetime],
        timeframe: str = "D",
        feature_names: Sequence[str] | None = None,
    ) -> list[dict[str, dict[str, Any]]]:
        """Walk-forward snapshots at each timestamp, in one query.

        The reconstruction order guarantees no snapshot ever sees a value
        stamped after its own timestamp — the training/backtest-safe path.
        """
        if not timestamps:
            return []
        from app.database.tables import FeatureStoreRow

        query = (
            select(
                FeatureStoreRow.feature_name,
                FeatureStoreRow.ts,
                FeatureStoreRow.value,
                FeatureStoreRow.feature_version,
            )
            .where(
                FeatureStoreRow.symbol == symbol,
                FeatureStoreRow.timeframe == timeframe,
                FeatureStoreRow.ts <= max(timestamps),
            )
            .order_by(FeatureStoreRow.ts)
        )
        if feature_names:
            query = query.where(FeatureStoreRow.feature_name.in_(list(feature_names)))
        async with self._sessions() as session:
            rows = (await session.execute(query)).all()
        return walk_forward_snapshots(
            [(name, ts, value, version) for name, ts, value, version in rows],
            timestamps,
        )
