"""Market Intelligence API (Volume 4, Prompt 4.17)."""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query

from app.core.container import container
from app.intelligence.analogs import HistoricalAnalogEngine
from app.intelligence.breadth import BreadthIntelligenceEngine
from app.intelligence.composite import CompositeMarketIntelligenceEngine
from app.intelligence.confidence import MarketConfidenceEngine
from app.intelligence.institutional_flow import InstitutionalFlowIntelligenceEngine
from app.intelligence.regime import BayesianRegimeDetector
from app.intelligence.report import MarketStateReportEngine
from app.intelligence.sector import SectorIntelligenceEngine
from app.intelligence.trend import TrendIntelligenceEngine

router = APIRouter(prefix="/intelligence", tags=["intelligence"])


def _parse_as_of(as_of: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(as_of)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid as_of: {exc}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@router.get("/state/{symbol}")
async def current_market_state(symbol: str) -> dict:
    """Current Market State: generates (and persists) a fresh Market State Report."""
    engine = container.resolve(MarketStateReportEngine)
    report = await engine.generate(symbol)
    return report.to_dict()


@router.get("/state/{symbol}/history")
async def historical_market_state(symbol: str, as_of: str) -> dict:
    """Historical Market State: the report in effect at (or immediately before) `as_of`."""
    moment = _parse_as_of(as_of)
    engine = container.resolve(MarketStateReportEngine)
    report = await engine.report_as_of(symbol, moment)
    if report is None:
        raise HTTPException(status_code=404, detail=f"no report for {symbol} as of {as_of}")
    return report


@router.get("/reports/{symbol}")
async def market_intelligence_reports(
    symbol: str, limit: int = Query(default=20, ge=1, le=200)
) -> dict:
    """Market Intelligence Reports: persisted reports for `symbol`, most recent first."""
    engine = container.resolve(MarketStateReportEngine)
    reports = await engine.list_reports(symbol, limit=limit)
    return {"symbol": symbol, "count": len(reports), "reports": reports}


@router.get("/composite/{symbol}")
async def composite_market_intelligence(symbol: str) -> dict:
    """Composite Market Intelligence: single blended score/confidence across
    all ten Volume 4 components (Prompt 4.14). For the full detail behind
    that score (sector names, analog dates, reasoning strings), see
    /intelligence/state/{symbol}."""
    engine = container.resolve(CompositeMarketIntelligenceEngine)
    result = await engine.assess(symbol=symbol)
    return result.to_dict()


@router.get("/regime/{component}/{symbol}/{timeframe}")
async def regime_history(
    component: str, symbol: str, timeframe: str,
    limit: int = Query(default=20, ge=1, le=200),
) -> dict:
    """Regime History: persisted Bayesian belief snapshots, oldest first."""
    detector = container.resolve(BayesianRegimeDetector)
    history = await detector.history(component, symbol, timeframe, limit=limit)
    return {
        "component": component, "symbol": symbol, "timeframe": timeframe,
        "count": len(history), "history": history,
    }


@router.get("/trend/{symbol}")
async def trend_intelligence(symbol: str, timeframe: str = "D") -> dict:
    engine = container.resolve(TrendIntelligenceEngine)
    result = await engine.assess(symbol=symbol, timeframe=timeframe)
    return result.to_dict()


@router.get("/breadth")
async def breadth_intelligence() -> dict:
    engine = container.resolve(BreadthIntelligenceEngine)
    result = await engine.assess()
    return result.to_dict()


@router.get("/sector")
async def sector_intelligence() -> dict:
    engine = container.resolve(SectorIntelligenceEngine)
    result = await engine.assess()
    return result.to_dict()


@router.get("/institutional-flow")
async def institutional_intelligence() -> dict:
    engine = container.resolve(InstitutionalFlowIntelligenceEngine)
    result = await engine.assess()
    return result.to_dict()


@router.get("/analogs/{symbol}")
async def historical_analogs(symbol: str) -> dict:
    engine = container.resolve(HistoricalAnalogEngine)
    result = await engine.assess(symbol=symbol)
    return result.to_dict()


@router.get("/confidence/{symbol}")
async def market_confidence(symbol: str) -> dict:
    engine = container.resolve(MarketConfidenceEngine)
    result = await engine.assess(symbol=symbol)
    return result.to_dict()
