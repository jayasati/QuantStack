"""Prediction & Conviction API (Volume 5, Prompts 5.1-5.11)."""

from fastapi import APIRouter, HTTPException, Query

from app.core.container import container
from app.prediction.agreement import ModelAgreementEngine
from app.prediction.calibration import ProbabilityCalibrationEngine
from app.prediction.candidates import CandidateGenerationEngine
from app.prediction.conviction import ConvictionEngine
from app.prediction.ensemble import EnsemblePredictionEngine
from app.prediction.historical_similarity import HistoricalSimilarityEngine
from app.prediction.labeling import DEFAULT_MAX_HOLDING_BARS, TripleBarrierLabelingEngine
from app.prediction.market_context import MarketContextAdjustmentEngine
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


@router.get("/agreement/{symbol}")
async def model_agreement(
    symbol: str, timeframe: str = "D", direction: str = "long"
) -> dict:
    """Fresh model-agreement evaluation over a fresh ensemble prediction:
    prediction variance, agreement %, confidence spread, consensus
    probability, and model reliability. `proceed` is the trading gate --
    only high-agreement predictions should proceed."""
    engine = container.resolve(ModelAgreementEngine)
    result = await engine.evaluate(symbol, timeframe=timeframe, direction=direction)
    return result.to_dict()


@router.get("/agreement/{symbol}/history")
async def agreement_history(
    symbol: str, limit: int = Query(default=50, ge=1, le=500)
) -> list[dict]:
    """Persisted model-agreement history, newest first."""
    engine = container.resolve(ModelAgreementEngine)
    return await engine.recent(symbol=symbol, limit=limit)


@router.get("/similarity/candidates")
async def historical_similarity_for_candidates() -> list[dict]:
    """Historical similarity for every candidate in a fresh Top-20 scan
    (Prompt 5.2)."""
    engine = container.resolve(HistoricalSimilarityEngine)
    results = await engine.evaluate_top_candidates()
    return [r.to_dict() for r in results]


@router.get("/similarity/{symbol}")
async def historical_similarity(
    symbol: str, direction: str = "long"
) -> dict:
    """Top 20 historical analogs for one (symbol, direction): historical
    win rate, average return, worst drawdown, best run-up, and the
    probability distribution of subsequent returns."""
    engine = container.resolve(HistoricalSimilarityEngine)
    result = await engine.evaluate(symbol, direction=direction)
    return result.to_dict()


@router.get("/similarity/{symbol}/history")
async def similarity_history(
    symbol: str, limit: int = Query(default=50, ge=1, le=500)
) -> list[dict]:
    """Persisted historical-similarity history, newest first."""
    engine = container.resolve(HistoricalSimilarityEngine)
    return await engine.recent(symbol=symbol, limit=limit)


@router.get("/context/{symbol}")
async def market_context_adjustment(
    symbol: str, timeframe: str = "D", direction: str = "long"
) -> dict:
    """Adjusts the calibrated probability (Prompt 5.7) using Market
    Confidence, Liquidity, Event Risk, Regime Stability, Institutional
    Participation, and Volatility. Confidence is reduced whenever market
    quality deteriorates."""
    engine = container.resolve(MarketContextAdjustmentEngine)
    result = await engine.evaluate(symbol, timeframe=timeframe, direction=direction)
    return result.to_dict()


@router.get("/context/{symbol}/history")
async def market_context_history(
    symbol: str, limit: int = Query(default=50, ge=1, le=500)
) -> list[dict]:
    """Persisted market-context-adjustment history, newest first."""
    engine = container.resolve(MarketContextAdjustmentEngine)
    return await engine.recent(symbol=symbol, limit=limit)


@router.get("/conviction/candidates")
async def conviction_for_candidates() -> list[dict]:
    """Conviction Score/Confidence/Stability/Trend/Grade for every
    candidate in a fresh Top-20 scan (Prompt 5.2)."""
    engine = container.resolve(ConvictionEngine)
    results = await engine.evaluate_top_candidates()
    return [r.to_dict() for r in results]


@router.get("/conviction/{symbol}")
async def conviction(symbol: str, timeframe: str = "D", direction: str = "long") -> dict:
    """Blends all 8 evidence sources (Calibrated Probability, Market
    Context, Historical Analog, Institutional Flow, Market Structure,
    Liquidity, Sector Strength, Model Agreement) into a Conviction Score,
    Confidence, Stability, Trend, and Grade, with every contribution
    explained."""
    engine = container.resolve(ConvictionEngine)
    result = await engine.evaluate(symbol, timeframe=timeframe, direction=direction)
    return result.to_dict()


@router.get("/conviction/{symbol}/history")
async def conviction_history(
    symbol: str, limit: int = Query(default=50, ge=1, le=500)
) -> list[dict]:
    """Persisted conviction history, newest first."""
    engine = container.resolve(ConvictionEngine)
    return await engine.recent(symbol=symbol, limit=limit)
