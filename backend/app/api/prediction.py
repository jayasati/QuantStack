"""Prediction & Conviction API (Volume 5, Prompts 5.1-5.2)."""

from fastapi import APIRouter, Query

from app.core.container import container
from app.prediction.candidates import CandidateGenerationEngine
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


@router.get("/candidates")
async def generate_candidates() -> list[dict]:
    """Top-20 ranked trade candidates from a fresh opportunity scan."""
    engine = container.resolve(CandidateGenerationEngine)
    candidates = await engine.generate()
    return [c.to_dict() for c in candidates]


@router.get("/candidates/{symbol}")
async def symbol_candidate_history(
    symbol: str, limit: int = Query(default=50, ge=1, le=500)
) -> list[dict]:
    """Persisted candidate-generation history for one symbol, newest first."""
    engine = container.resolve(CandidateGenerationEngine)
    return await engine.recent(symbol=symbol, limit=limit)
