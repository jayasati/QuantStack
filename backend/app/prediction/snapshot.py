"""Feature Snapshot Engine (Volume 5, Prompt 5.3).

"One of the biggest missing pieces in retail systems: every prediction must
freeze the market first." The flow must be Snapshot -> Prediction, never
Live Market -> Prediction. This engine is that freeze point: every field
Prompt 5.2 approximated with a bare UUID now resolves to a real, addressable,
persisted record — and Candidate Generation is updated to actually call this
engine instead of minting an unbacked UUID (see candidates.py).

Field sourcing (no field here is invented; each traces to a real store):
- Feature Values / Feature Versions: FeatureStore.latest(symbol, timeframe)
  already returns both per feature in one call (each stored FeatureValue
  carries its feature_version at write time, per BaseFeatureEngine).
- Market Report / Regime: MarketStateReportEngine.generate(symbol) (Volume 4,
  Prompt 4.15) — generated fresh here, not read from a possibly-stale prior
  report, so the snapshot's market context is synced to the exact as_of
  moment. This does mean generate() also persists its own
  market_state_report.observation event as a side effect — an accepted v1
  redundancy, the same category report.py's own docstring already accepts
  for Market Confidence/Historical Analogs being fetched twice per report.
- Collector Versions: the `collectors` table (Volume 1 schema) has a
  `version` column, but nothing in the collector framework has ever written
  to it — retrofitting a versioning scheme onto Volume 1/2's collectors is
  out of scope for this prompt. Queried honestly as-is: today this reads
  back empty, and will start populating on its own the moment anything
  writes real rows there, with no change needed here.
- Model Version / Prediction Version: None until Prompt 5.6 (Ensemble
  Prediction) and Prompt 5.4 (Multi-Horizon Prediction) respectively exist
  — there is nothing to version yet, and fabricating a placeholder string
  would violate the "never fabricate" rule every collector/engine in this
  codebase follows.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.cache import CacheService
from app.core.config import Settings, get_settings
from app.events.bus import Event, EventBus
from app.features.store import FeatureStore
from app.intelligence.base import IntelligenceResult
from app.intelligence.report import MarketStateReportEngine

EVENT_TYPE = "feature_snapshot.captured"


@dataclass
class FeatureSnapshot:
    snapshot_id: str
    symbol: str
    timeframe: str
    as_of: datetime
    feature_values: dict[str, float] = field(default_factory=dict)
    feature_versions: dict[str, str] = field(default_factory=dict)
    market_report: dict[str, Any] = field(default_factory=dict)
    regime: dict[str, str | None] = field(default_factory=dict)
    collector_versions: dict[str, str] = field(default_factory=dict)
    model_version: str | None = None
    prediction_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "as_of": self.as_of.isoformat(),
            "feature_values": self.feature_values,
            "feature_versions": self.feature_versions,
            "market_report": self.market_report,
            "regime": self.regime,
            "collector_versions": self.collector_versions,
            "model_version": self.model_version,
            "prediction_version": self.prediction_version,
        }


class FeatureSnapshotEngine:
    name = "feature_snapshot_engine"

    def __init__(
        self,
        session_factory: Any = None,
        cache: CacheService | None = None,
        settings: Settings | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings or get_settings()
        self._bus = bus
        self.store = FeatureStore(session_factory=session_factory, cache=cache)
        self._report_engine = MarketStateReportEngine(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )

    async def capture(
        self,
        symbol: str,
        timeframe: str = "D",
        *,
        precomputed: Mapping[str, IntelligenceResult | None] | None = None,
    ) -> FeatureSnapshot:
        """Freeze every feature value/version, the market report, current
        regime, and collector versions for `symbol` right now.

        `precomputed` (e.g. an OpportunityCandidate.component_results, merged
        with this engine's own `market_wide_context()`) is forwarded to
        MarketStateReportEngine.generate() so it doesn't recompute
        components the caller already has -- see report.py's docstring."""
        latest = await self.store.latest(symbol, timeframe)
        feature_values: dict[str, float] = {}
        feature_versions: dict[str, str] = {}
        for name, entry in latest.items():
            if not isinstance(entry, dict) or entry.get("value") is None:
                continue
            feature_values[name] = entry["value"]
            feature_versions[name] = entry.get("version") or "v1"

        report = await self._report_engine.generate(symbol, precomputed=precomputed)
        collector_versions = await self._collector_versions()

        snapshot = FeatureSnapshot(
            snapshot_id=uuid.uuid4().hex,
            symbol=symbol,
            timeframe=timeframe,
            as_of=datetime.now(UTC),
            feature_values=feature_values,
            feature_versions=feature_versions,
            market_report=report.to_dict(),
            regime=dict(report.current_regimes),
            collector_versions=collector_versions,
        )
        await self._persist(snapshot)
        return snapshot

    async def market_wide_context(self) -> dict[str, IntelligenceResult | None]:
        """Passthrough to the internal report engine's one-time market-wide
        fetch (breadth/macro/sector/correlation) -- a caller capturing
        snapshots for several symbols in one request should call this once
        and pass the result into every `capture(..., precomputed=...)` call
        rather than letting each one re-fetch these independently."""
        return await self._report_engine.market_wide_context()

    async def _collector_versions(self) -> dict[str, str]:
        if self._sessions is None:
            return {}
        from sqlalchemy import select

        from app.database.tables import Collector

        async with self._sessions() as session:
            result = await session.execute(select(Collector.name, Collector.version))
            return {name: version for name, version in result.all() if name and version}

    async def _persist(self, snapshot: FeatureSnapshot) -> None:
        if self._bus is not None:
            await self._bus.publish(
                Event(type=EVENT_TYPE, payload=snapshot.to_dict(), source=self.name)
            )
        if self._sessions is None:
            return
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            session.add(MarketEvent(
                event_type=EVENT_TYPE,
                source=self.name,
                data=snapshot.to_dict(),
            ))
            await session.commit()

    async def get(self, snapshot_id: str) -> dict[str, Any] | None:
        """Exact historical reconstruction: the frozen state behind one
        snapshot_id, exactly as it was captured."""
        if self._sessions is None:
            return None
        from sqlalchemy import select

        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            result = await session.execute(
                select(MarketEvent.data).where(
                    MarketEvent.event_type == EVENT_TYPE,
                    MarketEvent.data["snapshot_id"].astext == snapshot_id,
                )
            )
            return result.scalar_one_or_none()

    async def recent(self, symbol: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if self._sessions is None:
            return []
        from sqlalchemy import desc, select

        from app.database.tables import MarketEvent

        query = select(MarketEvent.data).where(MarketEvent.event_type == EVENT_TYPE)
        if symbol is not None:
            query = query.where(MarketEvent.data["symbol"].astext == symbol)
        query = query.order_by(desc(MarketEvent.id)).limit(limit)
        async with self._sessions() as session:
            result = await session.execute(query)
            return list(result.scalars().all())
