"""Market State Report (Volume 4, Prompt 4.15).

The culminating Volume 4 output: rather than sending dozens of raw metrics
downstream, one structured report per evaluation cycle becomes the single
input for the Prediction & Conviction Engine (Volume 5). A genuinely
different shape from every other component: not a single blended score
(that's Composite Market Intelligence's job) but a comprehensive snapshot
of the FULL detail behind it — sector names, analog dates, reasoning
strings — persisted with a timestamp for historical replay.

Reuses assess_composite() (the pure function from Prompt 4.14) directly on
the same fetched component results, rather than calling
CompositeMarketIntelligenceEngine and re-fetching all ten components a
second time. Market Confidence and Historical Analogs are fetched
alongside as two more concurrent calls — Market Confidence's own internal
orchestration (Regime Transition, Breadth, Institutional Flow, Correlation)
means some of those four get computed twice per report. That's an
accepted v1 redundancy: every component here is a cheap Feature Store
read, and this runs on a periodic evaluation cycle, not a hot path —
threading pre-computed results through would be premature optimization for
a report that just needs to be *right*, not fast.

Contribution-level explainability (features, weights, per-score reasoning
chains) is deliberately NOT embedded here — that's Prompt 4.16's job. This
report carries each component's score, confidence, dominant state, metrics,
and human-readable reasoning; not the full Contribution breakdown.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.cache import CacheService
from app.core.config import Settings
from app.events.bus import EventBus
from app.intelligence.analogs import HistoricalAnalogEngine
from app.intelligence.base import IntelligenceComponent, IntelligenceResult, SessionFactory
from app.intelligence.breadth import BreadthIntelligenceEngine
from app.intelligence.composite import assess_composite
from app.intelligence.confidence import MarketConfidenceEngine
from app.intelligence.correlation import CorrelationIntelligenceEngine
from app.intelligence.events import EventIntelligenceEngine
from app.intelligence.institutional_flow import InstitutionalFlowIntelligenceEngine
from app.intelligence.liquidity import LiquidityIntelligenceEngine
from app.intelligence.macro import MacroIntelligenceEngine
from app.intelligence.sector import SectorIntelligenceEngine
from app.intelligence.structure import MarketStructureIntelligenceEngine
from app.intelligence.trend import TrendIntelligenceEngine
from app.intelligence.volatility import VolatilityIntelligenceEngine

REPORT_EVENT_TYPE = "market_state_report.observation"


@dataclass
class MarketStateReport:
    as_of: datetime
    symbol: str
    current_regimes: dict[str, str | None] = field(default_factory=dict)
    probabilities: dict[str, dict[str, float]] = field(default_factory=dict)
    trend_summary: dict[str, Any] = field(default_factory=dict)
    breadth_summary: dict[str, Any] = field(default_factory=dict)
    liquidity_summary: dict[str, Any] = field(default_factory=dict)
    sector_leaders: dict[str, Any] = field(default_factory=dict)
    macro_summary: dict[str, Any] = field(default_factory=dict)
    institutional_positioning: dict[str, Any] = field(default_factory=dict)
    historical_analogs: list[dict[str, Any]] = field(default_factory=list)
    market_confidence: dict[str, Any] = field(default_factory=dict)
    composite_intelligence_score: float = 50.0
    expected_opportunity: float = 0.0
    expected_risk: float = 50.0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["as_of"] = self.as_of.isoformat()
        return payload


def _summarize(result: IntelligenceResult | None) -> dict[str, Any]:
    if result is None:
        return {"available": False}
    dominant = max(result.states, key=lambda s: result.states[s]) if result.states else None
    return {
        "available": True,
        "score": result.score,
        "confidence": result.confidence,
        "dominant_state": dominant,
        "metrics": result.metrics,
        "reasoning": result.reasoning,
    }


def build_market_state_report(
    symbol: str,
    as_of: datetime,
    component_results: Mapping[str, IntelligenceResult | None],
    confidence_result: IntelligenceResult | None,
    analog_result: IntelligenceResult | None,
) -> MarketStateReport:
    """Pure report assembly from already-computed component results."""
    composite = assess_composite(component_results)

    current_regimes: dict[str, str | None] = {}
    probabilities: dict[str, dict[str, float]] = {}
    for name, result in component_results.items():
        if result is None or not result.states:
            continue
        states = result.states
        current_regimes[name] = max(states, key=lambda s: states[s])
        probabilities[name] = dict(states)
    if confidence_result is not None and confidence_result.states:
        confidence_states = confidence_result.states
        current_regimes["market_confidence"] = max(
            confidence_states, key=lambda s: confidence_states[s]
        )
        probabilities["market_confidence"] = dict(confidence_states)

    sector_result = component_results.get("sector")
    sector_leaders = {
        "leading_sectors": sector_result.metrics.get("leading_sectors") if sector_result else None,
        "lagging_sectors": sector_result.metrics.get("lagging_sectors") if sector_result else None,
    }

    market_confidence = {
        "score": confidence_result.score if confidence_result else None,
        "grade": confidence_result.metrics.get("confidence_grade") if confidence_result else None,
        "trend": confidence_result.metrics.get("confidence_trend") if confidence_result else None,
    }

    analogs = list(analog_result.metrics.get("analogs", [])) if analog_result else []

    return MarketStateReport(
        as_of=as_of,
        symbol=symbol,
        current_regimes=current_regimes,
        probabilities=probabilities,
        trend_summary=_summarize(component_results.get("trend")),
        breadth_summary=_summarize(component_results.get("breadth")),
        liquidity_summary=_summarize(component_results.get("liquidity")),
        sector_leaders=sector_leaders,
        macro_summary=_summarize(component_results.get("macro")),
        institutional_positioning=_summarize(component_results.get("institutional_flow")),
        historical_analogs=analogs,
        market_confidence=market_confidence,
        composite_intelligence_score=composite.score,
        expected_opportunity=composite.metrics["expected_opportunity"],
        expected_risk=composite.metrics["expected_risk"],
    )


class MarketStateReportEngine(IntelligenceComponent):
    name = "market_state_report_engine"

    def __init__(
        self,
        session_factory: SessionFactory | None = None,
        cache: CacheService | None = None,
        settings: Settings | None = None,
        bus: EventBus | None = None,
        trend_engine: TrendIntelligenceEngine | None = None,
        volatility_engine: VolatilityIntelligenceEngine | None = None,
        breadth_engine: BreadthIntelligenceEngine | None = None,
        liquidity_engine: LiquidityIntelligenceEngine | None = None,
        macro_engine: MacroIntelligenceEngine | None = None,
        sector_engine: SectorIntelligenceEngine | None = None,
        institutional_flow_engine: InstitutionalFlowIntelligenceEngine | None = None,
        correlation_engine: CorrelationIntelligenceEngine | None = None,
        market_structure_engine: MarketStructureIntelligenceEngine | None = None,
        event_engine: EventIntelligenceEngine | None = None,
        confidence_engine: MarketConfidenceEngine | None = None,
        analog_engine: HistoricalAnalogEngine | None = None,
    ) -> None:
        super().__init__(session_factory=session_factory, cache=cache, settings=settings, bus=bus)
        self._trend = trend_engine or TrendIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )
        self._volatility = volatility_engine or VolatilityIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )
        self._breadth = breadth_engine or BreadthIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )
        self._liquidity = liquidity_engine or LiquidityIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )
        self._macro = macro_engine or MacroIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )
        self._sector = sector_engine or SectorIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )
        self._institutional_flow = institutional_flow_engine or InstitutionalFlowIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )
        self._correlation = correlation_engine or CorrelationIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )
        self._market_structure = market_structure_engine or MarketStructureIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )
        self._events = event_engine or EventIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )
        self._confidence = confidence_engine or MarketConfidenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )
        self._analogs = analog_engine or HistoricalAnalogEngine(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )

    async def generate(self, symbol: str | None = None) -> MarketStateReport:
        symbol = symbol or self._settings.feature_benchmark_symbol

        async def safe(coro):
            try:
                return await coro
            except Exception:
                return None

        (
            trend, volatility, breadth, liquidity, macro, sector, flow,
            correlation, structure, events, confidence, analogs,
        ) = await asyncio.gather(
            safe(self._trend.assess(symbol=symbol)),
            safe(self._volatility.assess(symbol=symbol)),
            safe(self._breadth.assess()),
            safe(self._liquidity.assess(symbol=symbol)),
            safe(self._macro.assess()),
            safe(self._sector.assess()),
            safe(self._institutional_flow.assess()),
            safe(self._correlation.assess()),
            safe(self._market_structure.assess(symbol=symbol)),
            safe(self._events.assess()),
            safe(self._confidence.assess(symbol=symbol)),
            safe(self._analogs.assess(symbol=symbol)),
        )

        component_results: dict[str, IntelligenceResult | None] = {
            "trend": trend,
            "volatility": volatility,
            "breadth": breadth,
            "liquidity": liquidity,
            "macro": macro,
            "sector": sector,
            "institutional_flow": flow,
            "correlation": correlation,
            "market_structure": structure,
            "event_risk": events,
        }
        report = build_market_state_report(
            symbol, datetime.now(UTC), component_results, confidence, analogs
        )
        await self._persist(report)
        await self._publish(
            REPORT_EVENT_TYPE,
            {
                "symbol": symbol,
                "composite_intelligence_score": report.composite_intelligence_score,
                "expected_opportunity": report.expected_opportunity,
                "expected_risk": report.expected_risk,
                "as_of": report.as_of.isoformat(),
            },
        )
        return report

    async def _persist(self, report: MarketStateReport) -> None:
        if self._sessions is None:
            return
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            session.add(MarketEvent(
                event_type=REPORT_EVENT_TYPE,
                source=self.name,
                data=report.to_dict(),
            ))
            await session.commit()

    async def report_as_of(self, symbol: str, as_of: datetime) -> dict[str, Any] | None:
        """Historical replay: the report in effect at (or immediately before)
        `as_of`. Call with datetime.now(UTC) for the latest report."""
        if self._sessions is None:
            return None
        from sqlalchemy import desc, select

        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            result = await session.execute(
                select(MarketEvent.data)
                .where(
                    MarketEvent.event_type == REPORT_EVENT_TYPE,
                    MarketEvent.data["symbol"].astext == symbol,
                    MarketEvent.created_at <= as_of,
                )
                .order_by(desc(MarketEvent.created_at))
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def list_reports(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        """Persisted reports for `symbol`, most recent first."""
        if self._sessions is None:
            return []
        from sqlalchemy import desc, select

        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            result = await session.execute(
                select(MarketEvent.data)
                .where(
                    MarketEvent.event_type == REPORT_EVENT_TYPE,
                    MarketEvent.data["symbol"].astext == symbol,
                )
                .order_by(desc(MarketEvent.created_at))
                .limit(limit)
            )
            return [row for row in result.scalars().all() if row is not None]
