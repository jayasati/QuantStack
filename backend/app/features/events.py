"""Event Risk Feature Engine (Volume 3, Prompt 3.11).

Transforms the event_calendar collector's observations (one summary plus one
record per upcoming event, per run) into versioned features on collector-run
time (symbol MARKET, synthetic timeframe "events").

Feature conventions (per run snapshot):
- Hours Until Event: hours to the nearest upcoming event.
- Risk Window: 1 when any event's pre/post window is active, else 0.
- Expected Volatility: the highest expected-volatility multiplier among
  events inside their risk window; the nearest event's multiplier otherwise.
- Event Category: impact of the nearest event (low=0, medium=0.5, high=1).
- Confidence Reduction: the summary's total confidence reduction (what the
  signal engines should multiply their confidence by, subtracted from 1).
- Trading Freeze Flag: 1 when the summary recommends freezing entries.
- Market Sensitivity: v1 composite of active-event count, max impact, and
  confidence reduction, 0..1.
- Historical Event Similarity: max Jaccard similarity between the current
  active-event-kind set and any earlier snapshot's set — how familiar the
  current event mix is (0 = never seen, 1 = exact repeat).

Every feature ships a look-ahead-safe rolling z-score companion (_z).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select

from app.core.logging import get_logger
from app.features.base import BaseFeatureEngine
from app.features.normalize import add_normalized_series, normalized_definition
from app.features.schema import Candle, FeatureDefinition, Series

logger = get_logger(__name__)

ENGINE_NAME = "event_risk_engine"
ENGINE_VERSION = "v1"
CATEGORY = "events"

MARKET_SYMBOL = "MARKET"
EVENTS_TIMEFRAME = "events"

IMPACT_SCORES = {"low": 0.0, "medium": 0.5, "high": 1.0}
BUCKET_SECONDS = 60
SIMILARITY_LOOKBACK = 500


@dataclass(frozen=True)
class EventSnapshot:
    """One collector run: the summary record plus every event record."""

    ts: datetime
    summary: dict[str, Any] = field(default_factory=dict)
    events: tuple[dict[str, Any], ...] = ()


def bucket_event_observations(
    rows: Sequence[tuple[datetime, dict[str, Any]]],
    bucket_seconds: int = BUCKET_SECONDS,
) -> list[EventSnapshot]:
    """Group (ts, metadata) calendar observations into run snapshots."""
    buckets: dict[int, dict[str, Any]] = {}
    for ts, metadata in rows:
        key = int(ts.timestamp()) // bucket_seconds
        bucket = buckets.setdefault(key, {"ts": ts, "summary": {}, "events": []})
        bucket["ts"] = max(bucket["ts"], ts)
        if metadata.get("record_type") == "summary":
            bucket["summary"] = metadata
        elif metadata.get("record_type") == "event":
            bucket["events"].append(metadata)
    return [
        EventSnapshot(ts=b["ts"], summary=b["summary"], events=tuple(b["events"]))
        for _, b in sorted(buckets.items())
    ]


# --- Feature definitions -------------------------------------------------------

def event_feature_definitions(
    normalization_window: int,
    calculation_frequency: str = "on_schedule",
) -> list[FeatureDefinition]:
    def define(name: str, description: str, unit: str,
               expected: tuple[float | None, float | None],
               dependencies: tuple[str, ...] = (),
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
        )

    definitions = [
        define("event_hours_until_next", "Hours to the nearest upcoming event.",
               "hours", (0.0, None)),
        define("event_risk_window", "1 when any event risk window is active.",
               "flag", (0.0, 1.0)),
        define("event_expected_volatility",
               "Expected volatility multiplier of the governing event.",
               "ratio", (1.0, 5.0)),
        define("event_category_impact",
               "Impact of the nearest event: low=0, medium=0.5, high=1.",
               "ratio", (0.0, 1.0)),
        define("event_confidence_reduction",
               "Total confidence reduction recommended by the calendar.",
               "ratio", (0.0, 1.0)),
        define("event_trading_freeze", "1 when a trading freeze is recommended.",
               "flag", (0.0, 1.0)),
        define("event_market_sensitivity",
               "v1 composite of active-event count, max impact, and confidence "
               "reduction, 0..1.",
               "ratio", (0.0, 1.0),
               ("event_confidence_reduction",)),
        define("event_historical_similarity",
               "Max Jaccard similarity of the current active-event-kind set vs "
               "earlier snapshots (1 = seen this exact mix before).",
               "ratio", (0.0, 1.0)),
    ]
    definitions.extend(
        normalized_definition(d, normalization_window) for d in list(definitions)
    )
    return definitions


# --- Pure calculations -----------------------------------------------------------

def compute_event_features(
    snapshots: Sequence[EventSnapshot],
    normalization_window: int = 100,
) -> dict[str, Series]:
    """Compute every event-risk feature (raw + _z) aligned to `snapshots`."""
    n = len(snapshots)
    hours_until: Series = [None] * n
    risk_window: Series = [None] * n
    expected_vol: Series = [None] * n
    category_impact: Series = [None] * n
    confidence_reduction: Series = [None] * n
    trading_freeze: Series = [None] * n
    sensitivity: Series = [None] * n
    similarity: Series = [None] * n

    kind_sets: list[frozenset[str]] = []
    for i, snap in enumerate(snapshots):
        upcoming = [
            e for e in snap.events
            if isinstance(e.get("hours_until_event"), int | float)
            and e["hours_until_event"] >= 0
        ]
        nearest = min(upcoming, key=lambda e: e["hours_until_event"], default=None)
        if nearest is not None:
            hours_until[i] = float(nearest["hours_until_event"])
            impact = IMPACT_SCORES.get(str(nearest.get("expected_impact")))
            if impact is not None:
                category_impact[i] = impact

        in_window = [e for e in upcoming if e.get("in_pre_event_window")]
        if snap.events:
            risk_window[i] = 1.0 if in_window else 0.0
        governing = in_window or ([nearest] if nearest is not None else [])
        multipliers = [
            float(e["expected_volatility_multiplier"])
            for e in governing
            if isinstance(e.get("expected_volatility_multiplier"), int | float)
        ]
        if multipliers:
            expected_vol[i] = max(multipliers)

        summary = snap.summary
        if summary:
            reduction = summary.get("total_confidence_reduction")
            if reduction is not None:
                confidence_reduction[i] = float(reduction)
            trading_freeze[i] = 1.0 if summary.get("trading_freeze_recommended") else 0.0
            count = float(summary.get("active_event_count") or 0)
            max_impact = IMPACT_SCORES.get(str(summary.get("max_active_impact")), 0.0)
            reduction_term = float(reduction or 0.0)
            sensitivity[i] = min(
                1.0, 0.2 * count + 0.4 * max_impact + 0.4 * reduction_term
            )
            kinds = frozenset(summary.get("active_event_kinds") or ())
        else:
            kinds = frozenset()

        history = kind_sets[-SIMILARITY_LOOKBACK:]
        if kinds and history:
            best = 0.0
            for past in history:
                union = kinds | past
                if union:
                    best = max(best, len(kinds & past) / len(union))
            similarity[i] = best
        elif kinds:
            similarity[i] = 0.0  # first time we see any event mix
        kind_sets.append(kinds)

    out: dict[str, Series] = {
        "event_hours_until_next": hours_until,
        "event_risk_window": risk_window,
        "event_expected_volatility": expected_vol,
        "event_category_impact": category_impact,
        "event_confidence_reduction": confidence_reduction,
        "event_trading_freeze": trading_freeze,
        "event_market_sensitivity": sensitivity,
        "event_historical_similarity": similarity,
    }
    return add_normalized_series(out, normalization_window)


# --- Engine -------------------------------------------------------------------------

class EventRiskEngine(BaseFeatureEngine):
    name = ENGINE_NAME
    category = CATEGORY

    def _definitions(self) -> list[FeatureDefinition]:
        return event_feature_definitions(
            self._settings.feature_normalization_window,
            calculation_frequency=f"{self._settings.feature_engine_interval}s",
        )

    def _compute(
        self, candles: Sequence[Candle], benchmark: Sequence[Candle] | None = None
    ) -> dict[str, Series]:
        return {}  # event features live on collector-run time, not bars

    async def run(
        self,
        symbol: str = MARKET_SYMBOL,
        timeframe: str = "D",
        full: bool = False,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict:
        """Event risk is market-wide: symbol/timeframe arguments are ignored
        (as are start/end -- accepted only for signature compatibility with
        the base class; this engine's calendar-row read isn't date-ranged,
        data foundation audit 2026-07-17, historical regeneration item)."""
        rows = await self._load_calendar_rows()
        snapshots = bucket_event_observations(rows)
        if len(snapshots) < 2:
            return {
                "symbol": MARKET_SYMBOL,
                "timeframe": EVENTS_TIMEFRAME,
                "stored": 0,
                "skipped": True,
            }
        series = compute_event_features(
            snapshots, self._settings.feature_normalization_window
        )
        return await self._process_series(
            MARKET_SYMBOL, EVENTS_TIMEFRAME, [s.ts for s in snapshots], series, full=full
        )

    async def run_all(
        self, full: bool = False, start: datetime | None = None, end: datetime | None = None,
    ) -> list[dict]:
        try:
            return [await self.run()]
        except Exception as exc:
            logger.error("event risk run failed", extra={"error": str(exc)})
            return [{"symbol": MARKET_SYMBOL, "error": str(exc)}]

    async def _load_calendar_rows(self) -> list[tuple[datetime, dict[str, Any]]]:
        if self._sessions is None:
            return []
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            result = await session.execute(
                select(MarketEvent.data)
                .where(MarketEvent.event_type == "economic_calendar.observation")
                .order_by(desc(MarketEvent.id))
                .limit(self._settings.feature_events_lookback)
            )
            rows = result.scalars().all()
        observations: list[tuple[datetime, dict[str, Any]]] = []
        for data in reversed(rows):
            if not data:
                continue
            ts_raw = data.get("timestamp")
            metadata = data.get("metadata") or {}
            if not ts_raw or not metadata.get("record_type"):
                continue
            try:
                ts = datetime.fromisoformat(ts_raw)
            except (TypeError, ValueError):
                continue
            observations.append((ts, metadata))
        return observations
