"""Composite Market Intelligence Engine (Volume 4, Prompt 4.14).

"One of the most important outputs of the entire platform" per the docs:
aggregates all eleven other components (the original ten Volume 4 components
plus Options, added later once OptionsIntelligenceEngine closed the gap
between the options feature columns and this synthesis layer) into a single
Market State read. The biggest orchestration in this layer — calls every
other engine concurrently and degrades gracefully if any one of them fails,
since a top-level synthesis component should never go down because one
ingredient did.

Seven of the eleven inputs are directional (50-centered, bull/bear, same
convention as Trend Intelligence): Trend, Breadth, Macro, Sector,
Institutional Flow, Market Structure, Options. Four are magnitude-only
(0-100, not directional): Volatility, Liquidity, Correlation, Event Risk —
and among those, Liquidity is "higher = safer" while the other three are
"higher = riskier", which matters for how they fold into Stability vs. Risk.

- IntelligenceResult.score      -> Overall Market Intelligence Score
                                    (50-centered, mean of the six
                                    directional components' own leans)
- IntelligenceResult.confidence -> blend of how many of the ten components
                                    reported at all, and their own average
                                    confidence
- metrics["bullishness"] / ["bearishness"] -> Overall Bullishness/Bearishness
- metrics["market_stability"]  -> Market Stability (volatility + liquidity
                                    + correlation — NOT event risk; a known
                                    upcoming catalyst isn't "instability" in
                                    the ambient-market-structure sense)
- metrics["expected_risk"]     -> Expected Risk Level (volatility + event
                                    risk + correlation — NOT liquidity; an
                                    illiquid-but-calm market isn't the same
                                    risk as a volatile one)
- metrics["expected_opportunity"] -> Expected Opportunity Level: directional
                                    conviction scaled down by instability —
                                    the same conviction reads as a lower-
                                    quality opportunity in a chaotic market
                                    than in a calm one
"""

import asyncio
from collections.abc import Mapping
from statistics import fmean

from app.core.cache import CacheService
from app.core.config import Settings
from app.events.bus import EventBus
from app.intelligence.base import (
    Contribution,
    IntelligenceComponent,
    IntelligenceResult,
    SessionFactory,
    clamp,
    normalize_states,
)
from app.intelligence.breadth import BreadthIntelligenceEngine
from app.intelligence.correlation import CorrelationIntelligenceEngine
from app.intelligence.events import EventIntelligenceEngine
from app.intelligence.institutional_flow import InstitutionalFlowIntelligenceEngine
from app.intelligence.liquidity import LiquidityIntelligenceEngine
from app.intelligence.macro import MacroIntelligenceEngine
from app.intelligence.options import OptionsIntelligenceEngine
from app.intelligence.sector import SectorIntelligenceEngine
from app.intelligence.structure import MarketStructureIntelligenceEngine
from app.intelligence.trend import TrendIntelligenceEngine
from app.intelligence.volatility import VolatilityIntelligenceEngine

COMPONENT = "composite_market_intelligence"

DIRECTIONAL_COMPONENTS: tuple[str, ...] = (
    "trend", "breadth", "macro", "sector", "institutional_flow", "market_structure", "options",
)
MAGNITUDE_COMPONENTS: tuple[str, ...] = ("volatility", "liquidity", "correlation", "event_risk")
ALL_COMPONENTS: tuple[str, ...] = DIRECTIONAL_COMPONENTS + MAGNITUDE_COMPONENTS

LEVEL_ANCHORS: dict[str, float] = {
    "strong_bearish": 0.0, "bearish": 0.25, "neutral": 0.5, "bullish": 0.75, "strong_bullish": 1.0,
}
LEVEL_BAND = 0.3


def _level_weights(level: float) -> dict[str, float]:
    return {
        name: max(0.0, 1 - abs(level - anchor) / LEVEL_BAND)
        for name, anchor in LEVEL_ANCHORS.items()
    }


def assess_composite(
    component_results: Mapping[str, IntelligenceResult | None],
) -> IntelligenceResult:
    """Pure synthesis from the ten other components' already-computed results."""
    contributions: list[Contribution] = []
    present = {k: v for k, v in component_results.items() if v is not None}

    for name in ALL_COMPONENTS:
        result = present.get(name)
        if result is None:
            continue
        is_directional = name in DIRECTIONAL_COMPONENTS
        effect = (
            ("bullish" if result.score > 50 else ("bearish" if result.score < 50 else "neutral"))
            if is_directional else f"{result.score:.0f}/100"
        )
        contributions.append(Contribution(
            feature=name, value=result.score, weight=1 / len(ALL_COMPONENTS), effect=effect,
        ))

    leans = [(present[name].score - 50) / 50 for name in DIRECTIONAL_COMPONENTS if name in present]
    overall_level = fmean(leans) if leans else 0.0
    bullishness = 100 * fmean([max(lean, 0.0) for lean in leans]) if leans else 0.0
    bearishness = 100 * fmean([max(-lean, 0.0) for lean in leans]) if leans else 0.0
    overall_score = clamp(50 + 50 * overall_level, 0.0, 100.0)

    def magnitude_level(name: str) -> float | None:
        result = present.get(name)
        return result.score / 100 if result is not None else None

    volatility_level = magnitude_level("volatility")
    liquidity_level = magnitude_level("liquidity")
    correlation_concentration = magnitude_level("correlation")
    event_risk_level = magnitude_level("event_risk")

    stability_terms = [v for v in (
        (1 - volatility_level) if volatility_level is not None else None,
        liquidity_level,
        (1 - correlation_concentration) if correlation_concentration is not None else None,
    ) if v is not None]
    market_stability = 100 * fmean(stability_terms) if stability_terms else 50.0

    risk_terms = [v for v in (
        volatility_level, event_risk_level, correlation_concentration,
    ) if v is not None]
    expected_risk = 100 * fmean(risk_terms) if risk_terms else 50.0

    conviction = abs(overall_level)
    expected_opportunity = clamp(100 * conviction * (market_stability / 100), 0.0, 100.0)

    data_completeness = len(present) / len(ALL_COMPONENTS)
    mean_component_confidence = fmean([r.confidence for r in present.values()]) if present else 0.0
    confidence = clamp(0.4 * data_completeness + 0.6 * mean_component_confidence, 0.0, 1.0)

    level_0_1 = clamp((overall_level + 1) / 2, 0.0, 1.0)
    states = normalize_states(_level_weights(level_0_1))
    dominant = max(states, key=lambda s: states[s])

    reasoning = [
        f"{len(present)}/{len(ALL_COMPONENTS)} components reporting; overall score "
        f"{overall_score:.0f}/100 (bullishness {bullishness:.0f}, bearishness {bearishness:.0f}).",
        f"Stability {market_stability:.0f}/100, expected risk {expected_risk:.0f}/100, "
        f"expected opportunity {expected_opportunity:.0f}/100.",
        f"Dominant state: {dominant}.",
    ]

    return IntelligenceResult(
        component=COMPONENT,
        score=round(overall_score, 4),
        confidence=confidence,
        states=states,
        metrics={
            "bullishness": round(bullishness, 4),
            "bearishness": round(bearishness, 4),
            "market_stability": round(market_stability, 4),
            "expected_opportunity": round(expected_opportunity, 4),
            "expected_risk": round(expected_risk, 4),
            "components_present": len(present),
            "component_scores": {
                name: (present[name].score if name in present else None)
                for name in ALL_COMPONENTS
            },
        },
        contributions=contributions,
        reasoning=reasoning,
    )


class CompositeMarketIntelligenceEngine(IntelligenceComponent):
    name = "composite_market_intelligence"

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
        options_engine: OptionsIntelligenceEngine | None = None,
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
        self._options = options_engine or OptionsIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )

    async def assess(self, symbol: str | None = None) -> IntelligenceResult:
        symbol = symbol or self._settings.feature_benchmark_symbol

        async def safe(coro):
            try:
                return await coro
            except Exception:
                return None

        (
            trend, volatility, breadth, liquidity, macro,
            sector, flow, correlation, structure, events, options,
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
            safe(self._options.assess(symbol=symbol)),
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
            "options": options,
        }
        result = assess_composite(component_results)
        result.metrics["symbol"] = symbol
        await self._publish_assessment(symbol, result)
        return result
