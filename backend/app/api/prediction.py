"""Prediction & Conviction API (Volume 5, Prompt 5.1)."""

from fastapi import APIRouter, Query

from app.core.container import container
from app.prediction.opportunity import OpportunityDetectionEngine

router = APIRouter(prefix="/prediction", tags=["prediction"])


@router.get("/opportunities")
async def scan_opportunities() -> list[dict]:
    """Fresh scan across the watchlist, sorted by priority descending."""
    engine = container.resolve(OpportunityDetectionEngine)
    candidates = await engine.scan()
    return [c.to_dict() for c in candidates]


@router.get("/opportunities/{symbol}")
async def symbol_opportunity_history(
    symbol: str, limit: int = Query(default=50, ge=1, le=500)
) -> list[dict]:
    """Persisted opportunity-detection history for one symbol, newest first."""
    engine = container.resolve(OpportunityDetectionEngine)
    return await engine.recent(symbol=symbol, limit=limit)
