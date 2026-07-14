"""Composite Market Intelligence Engine (Volume 4, Prompt 4.14).

"One of the most important outputs of the entire platform" per the docs:
aggregates all twelve other components (the original ten Volume 4 components
plus Options and Momentum, added later once their own engines closed the
gaps between raw feature columns and this synthesis layer) into a single
Market State read. The biggest orchestration in this layer — calls every
other engine concurrently and degrades gracefully if any one of them fails,
since a top-level synthesis component should never go down because one
ingredient did.

Eight of the twelve inputs are directional (50-centered, bull/bear, same
convention as Trend Intelligence): Trend, Breadth, Macro, Sector,
Institutional Flow, Market Structure, Options, Momentum. Four are
magnitude-only (0-100, not directional): Volatility, Liquidity, Correlation,
Event Risk — and among those, Liquidity is "higher = safer" while the other
three are "higher = riskier", which matters for how they fold into
Stability vs. Risk.

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
from app.core.logging import get_logger
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
from app.intelligence.explain import ExplainabilityStore
from app.intelligence.macro import MacroIntelligenceEngine
from app.intelligence.momentum import MomentumIntelligenceEngine
from app.intelligence.options import OptionsIntelligenceEngine
from app.intelligence.regime import BayesianRegimeDetector
from app.intelligence.sector import SectorIntelligenceEngine
from app.intelligence.structure import MarketStructureIntelligenceEngine
from app.intelligence.trend import TrendIntelligenceEngine
from app.intelligence.volatility import VolatilityIntelligenceEngine

logger = get_logger(__name__)

COMPONENT = "composite_market_intelligence"

DIRECTIONAL_COMPONENTS: tuple[str, ...] = (
    "trend", "breadth", "macro", "sector", "institutional_flow", "market_structure", "options",
    "momentum",
)
MAGNITUDE_COMPONENTS: tuple[str, ...] = ("volatility", "liquidity", "correlation", "event_risk")
ALL_COMPONENTS: tuple[str, ...] = DIRECTIONAL_COMPONENTS + MAGNITUDE_COMPONENTS

LEVEL_ANCHORS: dict[str, float] = {
    "strong_bearish": 0.0, "bearish": 0.25, "neutral": 0.5, "bullish": 0.75, "strong_bullish": 1.0,
}
LEVEL_BAND = 0.3

# Tolerance for "unchanged" when deciding whether to re-feed the regime
# detector -- states are deterministic pure-function outputs of the same
# underlying features, so an unchanged input reproduces bit-identical
# floats; this only needs to be loose enough to not misfire on that.
_STATES_UNCHANGED_EPSILON = 1e-9


def _states_changed(previous: dict[str, float] | None, current: Mapping[str, float]) -> bool:
    """True if `current` is materially different from `previous` (or there
    is no previous reading yet, in which case it's always "changed" -- the
    first feed always happens)."""
    if previous is None:
        return True
    if set(previous) != set(current):
        return True
    return any(abs(previous[k] - current[k]) > _STATES_UNCHANGED_EPSILON for k in current)


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
        momentum_engine: MomentumIntelligenceEngine | None = None,
        regime_detector: BayesianRegimeDetector | None = None,
        explainability_store: ExplainabilityStore | None = None,
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
        self._momentum = momentum_engine or MomentumIntelligenceEngine(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )
        self._regime_detector = regime_detector or BayesianRegimeDetector(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )
        self._explainability = explainability_store or ExplainabilityStore(
            session_factory=session_factory, cache=cache, settings=self._settings, bus=bus,
        )
        # In-memory only (see assess()'s regime-feed dedup) -- resets on
        # process restart, which is fine: a fresh process re-feeding once
        # after restart is correct, not a leak to guard against.
        self._last_fed_states: dict[tuple[str, str, str], dict[str, float]] = {}

    async def assess(self, symbol: str | None = None) -> IntelligenceResult:
        symbol = symbol or self._settings.feature_benchmark_symbol

        async def safe(coro):
            try:
                return await coro
            except Exception:
                return None

        (
            trend, volatility, breadth, liquidity, macro,
            sector, flow, correlation, structure, events, options, momentum,
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
            safe(self._momentum.assess(symbol=symbol)),
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
            "momentum": momentum,
        }
        result = assess_composite(component_results)
        result.metrics["symbol"] = symbol
        await self._publish_assessment(symbol, result)

        # Feed every present component's own reading into the shared
        # Bayesian regime detector (Ch15) -- previously only trend/
        # market_structure were ever fed, exclusively from
        # prediction/opportunity.py. Composite already computes all 11
        # components on every assess() call, so this is the natural place
        # to extend regime-belief tracking to the rest without any new
        # engine calls. Record each component's own explainability record
        # too (Ch16) -- ExplainabilityStore existed but had zero production
        # callers; this is the natural place for that as well, since every
        # component's full IntelligenceResult (contributions/reasoning) is
        # already in hand here.
        #
        # Skip the regime feed when a component's states are unchanged since
        # the last feed for that (component, symbol) -- found live in
        # production: this sweep runs every market_intelligence_interval
        # (a few minutes), far more often than slower-moving dimensions'
        # underlying data actually refreshes (trend reads "D"-timeframe
        # price momentum, which only updates once/day; institutional_flow's
        # FII/DII figures are effectively daily too). Feeding the identical
        # likelihood every cycle doesn't just waste a write -- bayesian_update
        # increments observation_count on every call regardless of whether
        # the evidence is new, so BayesianRegimeDetector's maturity/confidence
        # was inflating from repetition, not from genuinely new observations
        # (confirmed: trend/NIFTY reached observation_count=117 in one
        # session, 117 consecutive byte-identical states dicts). Explainability
        # recording is left un-deduped -- it's an audit log of every computed
        # score, not a belief-accumulation mechanism, so a redundant record
        # there is harmless.
        for name, component_result in component_results.items():
            if component_result is None:
                continue
            key = (name, symbol, "D")
            if _states_changed(self._last_fed_states.get(key), component_result.states):
                try:
                    await self._regime_detector.update_from_result(
                        name, symbol, "D", component_result
                    )
                    self._last_fed_states[key] = dict(component_result.states)
                except Exception:
                    logger.debug("regime belief update failed", extra={"component": name})
            # Not gated by the dedup check above -- always runs, even when
            # the regime feed was skipped as unchanged.
            try:
                await self._explainability.record(name, symbol, "D", component_result)
            except Exception:
                logger.debug("explainability record failed", extra={"component": name})
        try:
            await self._explainability.record(self.name, symbol, "D", result)
        except Exception:
            logger.debug("explainability record failed", extra={"component": self.name})

        return result
