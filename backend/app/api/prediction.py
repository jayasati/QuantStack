"""Prediction & Conviction API (Volume 5, Prompts 5.1-5.16)."""

from fastapi import APIRouter, HTTPException, Query

from app.core.container import container
from app.prediction.agreement import ModelAgreementEngine
from app.prediction.alpha_research import AlphaResearchEngine
from app.prediction.calibration import ProbabilityCalibrationEngine
from app.prediction.candidates import CandidateGenerationEngine
from app.prediction.conviction import ConvictionEngine
from app.prediction.duplicate import DuplicateSignalEngine
from app.prediction.ensemble import EnsemblePredictionEngine
from app.prediction.explainability import ExplainabilityReportEngine
from app.prediction.historical_similarity import HistoricalSimilarityEngine
from app.prediction.labeling import DEFAULT_MAX_HOLDING_BARS, TripleBarrierLabelingEngine
from app.prediction.lifecycle import (
    InvalidTransitionError,
    LifecycleState,
    OpportunityLifecycleManager,
)
from app.prediction.market_context import MarketContextAdjustmentEngine
from app.prediction.multi_horizon import MultiHorizonPredictionEngine
from app.prediction.opportunity import OpportunityDetectionEngine
from app.prediction.priority import TOP_N_DEFAULT, SignalPriorityEngine
from app.prediction.qualification import TradeQualificationEngine
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
    """Top-20 ranked trade candidates from a fresh opportunity scan, each
    enriched with signal_since (when this exact instrument/direction pair's
    current continuous run started, vs. as_of which just says when this
    particular scan re-confirmed it)."""
    engine = container.resolve(CandidateGenerationEngine)
    candidates = await engine.generate()
    return await engine.enrich_with_signal_since(candidates)


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


@router.post("/ensemble/{symbol}/train")
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


@router.post("/calibration/{symbol}/train")
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


@router.get("/qualification/candidates")
async def qualification_for_candidates() -> list[dict]:
    """Qualification result for every candidate in a fresh Top-20 scan
    (Prompt 5.2). Only qualified trades should continue downstream."""
    engine = container.resolve(TradeQualificationEngine)
    results = await engine.evaluate_top_candidates()
    return [r.to_dict() for r in results]


@router.get("/qualification/qualified")
async def qualified_trades() -> list[dict]:
    """Chapter 17's own "Qualified Trades" surface: a fresh Top-20 scan,
    filtered down to only the trades that actually qualified."""
    engine = container.resolve(TradeQualificationEngine)
    results = await engine.qualified_trades()
    return [r.to_dict() for r in results]


@router.get("/qualification/{symbol}")
async def trade_qualification(
    symbol: str, timeframe: str = "D", direction: str = "long"
) -> dict:
    """Rejects the trade if liquidity is too low, spread is too large,
    event risk is too high, model disagreement is high, feature quality
    is poor, market confidence is poor, or historical analog reliability
    is poor -- with explicit rejection reasons."""
    engine = container.resolve(TradeQualificationEngine)
    result = await engine.evaluate(symbol, timeframe=timeframe, direction=direction)
    return result.to_dict()


@router.get("/qualification/{symbol}/history")
async def qualification_history(
    symbol: str, limit: int = Query(default=50, ge=1, le=500)
) -> list[dict]:
    """Persisted qualification history, newest first."""
    engine = container.resolve(TradeQualificationEngine)
    return await engine.recent(symbol=symbol, limit=limit)


@router.get("/priority")
async def signal_priority(top_n: int = Query(default=TOP_N_DEFAULT, ge=1, le=20)) -> list[dict]:
    """A fresh Top-20 candidate scan, filtered to only qualified trades
    (Prompt 5.12), ranked across 8 factors (Conviction, Opportunity
    Quality, Risk, Liquidity, Sector Leadership, Historical Reliability,
    Expected Reward, Expected Opportunity Lifetime), Top N returned."""
    engine = container.resolve(SignalPriorityEngine)
    signals = await engine.rank(top_n=top_n)
    return [s.to_dict() for s in signals]


@router.get("/priority/history")
async def signal_priority_history(
    symbol: str | None = None, limit: int = Query(default=50, ge=1, le=500)
) -> list[dict]:
    """Persisted signal-priority history, optionally filtered by symbol,
    newest first."""
    engine = container.resolve(SignalPriorityEngine)
    return await engine.recent(symbol=symbol, limit=limit)


@router.get("/signals")
async def deduplicated_signals(top_n: int = Query(default=TOP_N_DEFAULT, ge=1, le=20)) -> dict:
    """A fresh ranked Top-N scan, de-duplicated: repeated opportunities,
    correlated stocks, repeated breakouts, and sector duplication are
    suppressed (with explicit reasons), keeping the batch diverse."""
    engine = container.resolve(DuplicateSignalEngine)
    result = await engine.rank_and_filter(top_n=top_n)
    return result.to_dict()


@router.get("/signals/history")
async def deduplicated_signals_history(limit: int = Query(default=50, ge=1, le=500)) -> list[dict]:
    """Persisted duplicate-filter history, newest first."""
    engine = container.resolve(DuplicateSignalEngine)
    return await engine.recent(limit=limit)


@router.post("/lifecycle/detect")
async def lifecycle_detect(symbol: str, direction: str = "long") -> dict:
    """Mints a new lifecycle_id at the 'detected' stage."""
    manager = container.resolve(OpportunityLifecycleManager)
    state = await manager.detect(symbol, direction)
    return state.to_dict()


def _lifecycle_or_404(state: LifecycleState | None) -> dict:
    if state is None:
        raise HTTPException(status_code=404, detail="unknown lifecycle_id")
    return state.to_dict()


@router.get("/lifecycle/{lifecycle_id}")
async def lifecycle_get(lifecycle_id: str) -> dict:
    """Current reconstructed state of one lifecycle."""
    manager = container.resolve(OpportunityLifecycleManager)
    return _lifecycle_or_404(await manager.get(lifecycle_id))


@router.post("/lifecycle/{lifecycle_id}/confirm")
async def lifecycle_confirm(lifecycle_id: str) -> dict:
    manager = container.resolve(OpportunityLifecycleManager)
    try:
        state = await manager.confirm(lifecycle_id)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return state.to_dict()


@router.post("/lifecycle/{lifecycle_id}/qualify")
async def lifecycle_qualify(lifecycle_id: str) -> dict:
    manager = container.resolve(OpportunityLifecycleManager)
    try:
        state = await manager.qualify(lifecycle_id)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return state.to_dict()


@router.post("/lifecycle/{lifecycle_id}/sent")
async def lifecycle_sent(lifecycle_id: str) -> dict:
    manager = container.resolve(OpportunityLifecycleManager)
    try:
        state = await manager.mark_sent(lifecycle_id)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return state.to_dict()


@router.post("/lifecycle/{lifecycle_id}/monitor")
async def lifecycle_monitor(lifecycle_id: str) -> dict:
    manager = container.resolve(OpportunityLifecycleManager)
    try:
        state = await manager.monitor(lifecycle_id)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return state.to_dict()


@router.post("/lifecycle/{lifecycle_id}/expire")
async def lifecycle_expire(lifecycle_id: str, reason: str) -> dict:
    manager = container.resolve(OpportunityLifecycleManager)
    try:
        state = await manager.expire(lifecycle_id, reason=reason)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return state.to_dict()


@router.post("/lifecycle/{lifecycle_id}/succeed")
async def lifecycle_succeed(lifecycle_id: str) -> dict:
    manager = container.resolve(OpportunityLifecycleManager)
    try:
        state = await manager.succeed(lifecycle_id)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return state.to_dict()


@router.post("/lifecycle/{lifecycle_id}/fail")
async def lifecycle_fail(lifecycle_id: str) -> dict:
    manager = container.resolve(OpportunityLifecycleManager)
    try:
        state = await manager.fail(lifecycle_id)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return state.to_dict()


@router.get("/lifecycle/{lifecycle_id}/history")
async def lifecycle_history(
    lifecycle_id: str, limit: int = Query(default=50, ge=1, le=500)
) -> list[dict]:
    """Raw transition log for one lifecycle, newest first."""
    manager = container.resolve(OpportunityLifecycleManager)
    return await manager.recent(lifecycle_id=lifecycle_id, limit=limit)


@router.get("/explainability/qualified")
async def explainability_for_qualified_candidates() -> list[dict]:
    """Chapter 18's own acceptance criterion: "Explainability reports
    accompany every qualified trade." A fresh Top-20 scan, qualified via
    Prompt 5.12's own gate, and one full report generated for each trade
    that actually qualifies."""
    engine = container.resolve(ExplainabilityReportEngine)
    reports = await engine.generate_for_qualified_candidates()
    return [r.to_dict() for r in reports]


@router.get("/explainability/{symbol}")
async def explainability_report(
    symbol: str, timeframe: str = "D", direction: str = "long"
) -> dict:
    """Top SHAP Features, Market Regime, Historical Analogs, Model
    Agreement, Confidence Breakdown, Conviction Breakdown, Reason Codes,
    and a Natural Language Summary -- every qualified trade should carry
    one of these."""
    engine = container.resolve(ExplainabilityReportEngine)
    report = await engine.generate(symbol, timeframe=timeframe, direction=direction)
    return report.to_dict()


@router.get("/explainability/{symbol}/history")
async def explainability_history(
    symbol: str, limit: int = Query(default=50, ge=1, le=500)
) -> list[dict]:
    """Persisted explainability-report history, newest first."""
    engine = container.resolve(ExplainabilityReportEngine)
    return await engine.recent(symbol=symbol, limit=limit)


@router.get("/research/leaderboard/features")
async def alpha_research_feature_leaderboard(
    top_n: int = Query(default=20, ge=1, le=100)
) -> list[dict]:
    """Top persisted feature evaluations by predictive power, across
    every symbol/timeframe ever evaluated (Volume 5.5)."""
    engine = container.resolve(AlphaResearchEngine)
    return await engine.feature_leaderboard(top_n=top_n)


@router.get("/research/leaderboard/comparisons")
async def alpha_research_comparison_leaderboard(
    top_n: int = Query(default=20, ge=1, le=100)
) -> list[dict]:
    """Top persisted champion-vs-challenger model comparisons by
    improvement, across every symbol ever compared (Volume 5.5)."""
    engine = container.resolve(AlphaResearchEngine)
    return await engine.comparison_leaderboard(top_n=top_n)


@router.get("/research/{symbol}/features")
async def alpha_research_features(
    symbol: str, timeframe: str = "D", direction: str = "long"
) -> list[dict]:
    """Evaluates candidate features (not in the production ensemble's own
    feature set) against real Triple Barrier outcomes, ranked by
    predictive power, with a feature-decay read."""
    engine = container.resolve(AlphaResearchEngine)
    evaluations = await engine.evaluate_candidate_features(
        symbol, timeframe=timeframe, direction=direction
    )
    return [e.to_dict() for e in evaluations]


@router.get("/research/{symbol}/recommendations")
async def alpha_research_recommendations(
    symbol: str, timeframe: str = "D", direction: str = "long"
) -> list[dict]:
    """Candidate features recommended for production inclusion: strong,
    non-decaying predictive power, not already in production."""
    engine = container.resolve(AlphaResearchEngine)
    recommendations = await engine.recommend_features(
        symbol, timeframe=timeframe, direction=direction
    )
    return [r.to_dict() for r in recommendations]


@router.get("/research/{symbol}/compare")
async def alpha_research_compare(
    symbol: str, timeframe: str = "D", direction: str = "long"
) -> dict:
    """Champion (production feature set) vs. challenger (production +
    candidate features) ensemble comparison over the same labels."""
    engine = container.resolve(AlphaResearchEngine)
    result = await engine.compare_against_production(
        symbol, timeframe=timeframe, direction=direction
    )
    return result.to_dict()
