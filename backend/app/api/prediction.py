"""Prediction & Conviction API (Volume 5, Prompts 5.1-5.7)."""

from fastapi import APIRouter, HTTPException, Query

from app.core.container import container
from app.prediction.calibration import ProbabilityCalibrationEngine
from app.prediction.candidates import CandidateGenerationEngine
from app.prediction.ensemble import EnsemblePredictionEngine
from app.prediction.labeling import DEFAULT_MAX_HOLDING_BARS, TripleBarrierLabelingEngine
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


@router.get("/labels/{symbol}")
async def generate_labels(
    symbol: str,
    timeframe: str = "D",
    direction: str = "long",
    lookback_bars: int = Query(default=100, ge=1, le=2000),
    max_holding_bars: int = Query(default=DEFAULT_MAX_HOLDING_BARS, ge=1, le=200),
) -> list[dict]:
    """Triple-barrier labels over the trailing `lookback_bars` historical
    entry points — training data for Prompt 5.6, not a live signal."""
    engine = container.resolve(TripleBarrierLabelingEngine)
    labels = await engine.label_history(
        symbol, timeframe=timeframe, direction=direction,
        lookback_bars=lookback_bars, max_holding_bars=max_holding_bars,
    )
    return [label.to_dict() for label in labels]


@router.get("/labels/{symbol}/history")
async def label_history(
    symbol: str,
    label: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict]:
    """Persisted labels, optionally filtered by outcome, newest first."""
    engine = container.resolve(TripleBarrierLabelingEngine)
    return await engine.recent(symbol=symbol, label=label, limit=limit)


@router.get("/ensemble/{symbol}/train")
async def train_ensemble(
    symbol: str,
    timeframe: str = "D",
    direction: str = "long",
    lookback_bars: int = Query(default=500, ge=1, le=5000),
    max_holding_bars: int = Query(default=DEFAULT_MAX_HOLDING_BARS, ge=1, le=200),
) -> dict:
    """Fits the six-model ensemble against Triple Barrier labels and cached
    in memory for subsequent /ensemble/{symbol} calls."""
    engine = container.resolve(EnsemblePredictionEngine)
    training = await engine.train(
        symbol, timeframe=timeframe, direction=direction,
        lookback_bars=lookback_bars, max_holding_bars=max_holding_bars,
    )
    return training.to_dict()


@router.get("/ensemble/{symbol}")
async def predict_ensemble(
    symbol: str, timeframe: str = "D", direction: str = "long"
) -> dict:
    """Fresh ensemble prediction (training on first call if not already
    cached) from a newly frozen snapshot: probability, confidence,
    uncertainty, disagreement score, and per-model explanations."""
    engine = container.resolve(EnsemblePredictionEngine)
    prediction = await engine.predict(symbol, timeframe=timeframe, direction=direction)
    return prediction.to_dict()


@router.get("/ensemble/{symbol}/history")
async def ensemble_history(
    symbol: str, limit: int = Query(default=50, ge=1, le=500)
) -> list[dict]:
    """Persisted ensemble prediction history, newest first."""
    engine = container.resolve(EnsemblePredictionEngine)
    return await engine.recent(symbol=symbol, limit=limit)


@router.get("/calibration/{symbol}/train")
async def train_calibration(
    symbol: str,
    timeframe: str = "D",
    direction: str = "long",
    lookback_bars: int = Query(default=500, ge=1, le=5000),
    max_holding_bars: int = Query(default=DEFAULT_MAX_HOLDING_BARS, ge=1, le=200),
) -> dict:
    """Trains the ensemble (if needed) and chooses the best of Platt
    Scaling / Isotonic Regression / Temperature Scaling against its
    out-of-sample holdout set. `calibrated: false` honestly means there
    wasn't enough calibration data yet -- not a fabricated fit."""
    engine = container.resolve(ProbabilityCalibrationEngine)
    result = await engine.calibrate(
        symbol, timeframe=timeframe, direction=direction,
        lookback_bars=lookback_bars, max_holding_bars=max_holding_bars,
    )
    if result is None:
        return {"calibrated": False, "reason": "insufficient_calibration_samples"}
    return {"calibrated": True, **result.to_dict()}


@router.get("/calibration/{symbol}")
async def predict_calibrated(
    symbol: str, timeframe: str = "D", direction: str = "long"
) -> dict:
    """Fresh calibrated prediction: raw ensemble probability, calibrated
    probability, and calibration confidence."""
    engine = container.resolve(ProbabilityCalibrationEngine)
    prediction = await engine.predict(symbol, timeframe=timeframe, direction=direction)
    return prediction.to_dict()


@router.get("/calibration/{symbol}/history")
async def calibration_history(
    symbol: str, limit: int = Query(default=50, ge=1, le=500)
) -> list[dict]:
    """Persisted calibrated-prediction history, newest first."""
    engine = container.resolve(ProbabilityCalibrationEngine)
    return await engine.recent(symbol=symbol, limit=limit)
