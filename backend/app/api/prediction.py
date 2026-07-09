"""Prediction & Conviction API (Volume 5, Prompts 5.1-5.4)."""

from fastapi import APIRouter, HTTPException, Query

from app.core.container import container
from app.prediction.candidates import CandidateGenerationEngine
from app.prediction.multi_horizon import MultiHorizonPredictionEngine
from app.prediction.opportunity import OpportunityDetectionEngine
from app.prediction.snapshot import FeatureSnapshotEngine

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


@router.get("/snapshots/{snapshot_id}")
async def get_snapshot(snapshot_id: str) -> dict:
    """Exact historical reconstruction of one frozen feature snapshot."""
    engine = container.resolve(FeatureSnapshotEngine)
    snapshot = await engine.get(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"unknown snapshot: {snapshot_id}")
    return snapshot


@router.get("/snapshots")
async def snapshot_history(
    symbol: str | None = None, limit: int = Query(default=50, ge=1, le=500)
) -> list[dict]:
    """Persisted snapshot history, optionally filtered by symbol, newest first."""
    engine = container.resolve(FeatureSnapshotEngine)
    return await engine.recent(symbol=symbol, limit=limit)


@router.get("/horizons/{symbol}")
async def predict_horizons(symbol: str) -> dict:
    """Fresh multi-horizon probability-of-up-move prediction (5min/15min/
    30min/1hour/end_of_day/next_day), captured from a new frozen snapshot."""
    engine = container.resolve(MultiHorizonPredictionEngine)
    prediction = await engine.predict(symbol)
    return prediction.to_dict()


@router.get("/horizons/{symbol}/history")
async def horizon_history(
    symbol: str,
    horizon: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict]:
    """Persisted per-horizon probability history, newest first."""
    engine = container.resolve(MultiHorizonPredictionEngine)
    return await engine.recent(symbol=symbol, horizon=horizon, limit=limit)
