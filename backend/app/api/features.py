"""Feature store observability and access API (Volume 3)."""

import inspect
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from app.core.container import container
from app.core.logging import get_logger
from app.features.base import BaseFeatureEngine
from app.features.breadth import BreadthFeatureEngine
from app.features.events import EventRiskEngine
from app.features.institutional_flow import InstitutionalFlowFeatureEngine
from app.features.intraday_risk import IntradayRiskFeatureEngine
from app.features.liquidity import LiquidityFeatureEngine
from app.features.macro import MacroFeatureEngine
from app.features.news import NewsFeatureEngine
from app.features.options import OptionsFeatureEngine
from app.features.price import PriceFeatureEngine
from app.features.relative import RelativeStrengthEngine
from app.features.risk import RiskFeatureEngine
from app.features.schema import FeatureDefinition
from app.features.sector import SectorFeatureEngine
from app.features.structure import MarketStructureEngine
from app.features.timefeat import TimeFeatureEngine
from app.features.volatility import VolatilityFeatureEngine
from app.features.volume import VolumeFeatureEngine

logger = get_logger(__name__)

router = APIRouter(prefix="/features", tags=["features"])


def _engines() -> list[BaseFeatureEngine]:
    return [
        container.resolve(PriceFeatureEngine),
        container.resolve(VolumeFeatureEngine),
        container.resolve(VolatilityFeatureEngine),
        container.resolve(LiquidityFeatureEngine),
        container.resolve(OptionsFeatureEngine),
        container.resolve(BreadthFeatureEngine),
        container.resolve(SectorFeatureEngine),
        container.resolve(RelativeStrengthEngine),
        container.resolve(MarketStructureEngine),
        container.resolve(NewsFeatureEngine),
        container.resolve(EventRiskEngine),
        container.resolve(TimeFeatureEngine),
        container.resolve(InstitutionalFlowFeatureEngine),
        container.resolve(MacroFeatureEngine),
        container.resolve(RiskFeatureEngine),
        container.resolve(IntradayRiskFeatureEngine),
    ]


def _owning_engine(feature_name: str) -> tuple[BaseFeatureEngine, FeatureDefinition]:
    for engine in _engines():
        definition = engine.registry.get(feature_name)
        if definition is not None:
            return engine, definition
    raise HTTPException(status_code=404, detail=f"unknown feature: {feature_name}")


@router.get("")
async def list_features(
    category: str | None = None,
    owner: str | None = None,
    search: str | None = None,
    include_normalized: bool = True,
    limit: int = Query(default=100, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Registered feature metadata (Chapter 5), paginated and filterable."""
    definitions = [
        d
        for engine in _engines()
        for d in engine.registry.list_definitions(category=category)
        if (owner is None or d.owner == owner)
        and (search is None or search.lower() in d.feature_name.lower())
        and (include_normalized or not d.feature_name.endswith("_z"))
    ]
    page = definitions[offset : offset + limit]
    return {
        "total": len(definitions),
        "limit": limit,
        "offset": offset,
        "features": [
            {
                "feature_name": d.feature_name,
                "category": d.category,
                "description": d.description,
                "version": d.version,
                "dependencies": list(d.dependencies),
                "calculation_frequency": d.calculation_frequency,
                "owner": d.owner,
                "quality_threshold": d.quality_threshold,
                "unit": d.unit,
                "expected_range": list(d.expected_range),
                "window": d.window,
            }
            for d in page
        ],
    }


@router.get("/latest/{symbol}")
async def latest_features(symbol: str, timeframe: str = "D") -> dict:
    """Latest value of every feature for a symbol (online store, offline fallback)."""
    store = _engines()[0].store  # stores share the same tables/cache
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "features": await store.latest(symbol, timeframe),
    }


@router.get("/history/{feature_name}")
async def feature_history(
    feature_name: str,
    symbol: str | None = None,
    timeframe: str | None = None,
    version: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Historical observations of one feature (offline store), paginated."""
    engine, _ = _owning_engine(feature_name)
    rows = await engine.store.history(
        feature_name, symbol=symbol, timeframe=timeframe,
        version=version, limit=limit, offset=offset,
    )
    return {"feature_name": feature_name, "limit": limit, "offset": offset, "history": rows}


@router.get("/{feature_name}/dependents")
async def feature_dependents(feature_name: str) -> dict:
    """Downstream features to recompute when this one changes (Chapter 7)."""
    engine, _ = _owning_engine(feature_name)
    return {
        "feature_name": feature_name,
        "dependents": engine.registry.dependents_of(feature_name),
    }


@router.get("/{feature_name}/versions")
async def feature_versions(feature_name: str) -> dict:
    """Published version history for one feature (Chapter 6)."""
    _owning_engine(feature_name)
    from sqlalchemy import select

    from app.database.session import get_session_factory
    from app.database.tables import FeatureVersion

    sessions = get_session_factory()
    async with sessions() as session:
        result = await session.execute(
            select(FeatureVersion.version, FeatureVersion.description, FeatureVersion.created_at)
            .where(FeatureVersion.feature_name == feature_name)
            .order_by(FeatureVersion.id)
        )
        rows = result.all()
    return {
        "feature_name": feature_name,
        "versions": [
            {
                "version": row.version,
                "description": row.description,
                "published_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
    }


@router.get("/replay/{symbol}")
async def replay_features(
    symbol: str,
    as_of: str,
    timeframe: str = "D",
    names: str | None = None,
) -> dict:
    """Feature state exactly as it existed at `as_of` (Prompt 3.17).

    `names` narrows to a comma-separated feature list.
    """
    from datetime import datetime

    from app.database.session import get_session_factory
    from app.features.replay import HistoricalReplayEngine

    try:
        moment = datetime.fromisoformat(as_of)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid as_of: {exc}") from exc
    engine = HistoricalReplayEngine(get_session_factory())
    feature_names = [n.strip() for n in names.split(",")] if names else None
    state = await engine.replay(symbol, moment, timeframe, feature_names)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "as_of": moment.isoformat(),
        "features": state,
    }


@router.post("/selection/{symbol}")
async def run_feature_selection(
    symbol: str,
    timeframe: str = "D",
    max_features: int = Query(default=10, ge=1, le=50),
) -> dict:
    """Systematic feature selection against the next-bar return (Prompt 3.16)."""
    from app.database.session import get_session_factory
    from app.features.selection import FeatureSelectionEngine

    engine = FeatureSelectionEngine(get_session_factory())
    report = await engine.select(symbol, timeframe, max_features)
    return {
        "symbol": report.symbol,
        "timeframe": report.timeframe,
        "rows": report.rows,
        "recommended_feature_set": report.recommended,
        "feature_ranking": report.ranking,
        "redundant_features": report.redundant,
        "highly_correlated_pairs": report.correlated_pairs,
        "dropped_low_variance": report.dropped_low_variance,
    }


@router.get("/usage/{symbol}")
async def feature_usage(
    symbol: str,
    timeframe: str = "D",
    consumer: str = "feature_selection",
) -> dict:
    """Currently-recommended feature set for one symbol/timeframe (Ch.8
    feature_usage; the persisted-read counterpart of POST /selection).

    This is feature_usage's only consumer today -- DEBT-9's own resolution.
    """
    from sqlalchemy import select

    from app.database.session import get_session_factory
    from app.database.tables import FeatureUsageRow

    sessions = get_session_factory()
    async with sessions() as session:
        rows = (
            await session.execute(
                select(
                    FeatureUsageRow.feature_name, FeatureUsageRow.data, FeatureUsageRow.created_at
                )
                .where(
                    FeatureUsageRow.consumer == consumer,
                    FeatureUsageRow.symbol == symbol,
                    FeatureUsageRow.timeframe == timeframe,
                )
                .order_by(FeatureUsageRow.id)
            )
        ).all()
    ranked = sorted(rows, key=lambda row: (row.data or {}).get("rank", 999))
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "consumer": consumer,
        "recommended_feature_set": [
            {
                "feature_name": row.feature_name,
                "rank": (row.data or {}).get("rank"),
                "last_selected_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in ranked
        ],
    }


@router.post("/quality/evaluate/{symbol}")
async def evaluate_feature_quality(symbol: str, timeframe: str = "D") -> dict:
    """On-demand quality sweep for one group (Prompt 3.14)."""
    from app.database.session import get_session_factory
    from app.features.quality import FeatureQualityEngine

    engine = FeatureQualityEngine(get_session_factory())
    reports = await engine.evaluate_group(symbol, timeframe)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "features_evaluated": len(reports),
        "drift_warnings": sum(1 for r in reports if r.drift_warning),
        "reports": [
            {
                "feature_name": r.feature_name,
                "quality_score": r.quality_score,
                "confidence_multiplier": r.confidence_multiplier,
                "drift_warning": r.drift_warning,
                "sample_count": r.sample_count,
                "components": r.components,
            }
            for r in sorted(reports, key=lambda r: r.quality_score)
        ],
    }


@router.post("/drift/detect/{symbol}")
async def detect_feature_drift(symbol: str, timeframe: str = "D") -> dict:
    """On-demand drift detection for one group (Prompt 3.15)."""
    from app.database.session import get_session_factory
    from app.features.drift import FeatureDriftEngine

    engine = FeatureDriftEngine(get_session_factory())
    results = await engine.detect_group(symbol, timeframe)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "detections": len(results),
        "breaches": [
            {
                "feature_name": r.feature_name,
                "metric": r.metric,
                "value": r.value,
                "threshold": r.threshold,
            }
            for r in results
            if r.breached
        ],
    }


@router.get("/{feature_name}/quality")
async def feature_quality_history(
    feature_name: str,
    symbol: str | None = None,
    limit: int = Query(default=20, ge=1, le=200),
) -> dict:
    """Persisted quality reports for one feature, newest first."""
    _owning_engine(feature_name)
    from sqlalchemy import desc, select

    from app.database.session import get_session_factory
    from app.database.tables import FeatureQualityRow

    query = (
        select(
            FeatureQualityRow.created_at, FeatureQualityRow.symbol,
            FeatureQualityRow.timeframe, FeatureQualityRow.quality_score,
            FeatureQualityRow.sample_count, FeatureQualityRow.data,
        )
        .where(FeatureQualityRow.feature_name == feature_name)
        .order_by(desc(FeatureQualityRow.id))
        .limit(limit)
    )
    if symbol is not None:
        query = query.where(FeatureQualityRow.symbol == symbol)
    sessions = get_session_factory()
    async with sessions() as session:
        rows = (await session.execute(query)).all()
    return {
        "feature_name": feature_name,
        "history": [
            {
                "at": row.created_at.isoformat() if row.created_at else None,
                "symbol": row.symbol,
                "timeframe": row.timeframe,
                "quality_score": row.quality_score,
                "sample_count": row.sample_count,
                **(row.data or {}),
            }
            for row in rows
        ],
    }


@router.get("/{feature_name}/drift")
async def feature_drift_history(
    feature_name: str,
    symbol: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    """Persisted drift history for one feature, newest first."""
    _owning_engine(feature_name)
    from app.database.session import get_session_factory
    from app.features.drift import FeatureDriftEngine

    engine = FeatureDriftEngine(get_session_factory())
    return {
        "feature_name": feature_name,
        "history": await engine.history(feature_name, symbol=symbol, limit=limit),
    }


def _run_kwargs(engine: BaseFeatureEngine, full: bool, start, end) -> dict:
    """Not every engine's run() accepts start/end -- 4 of 16 (price, volume,
    volatility, risk) inherit BaseFeatureEngine.run() unmodified and always
    do; liquidity/structure/relative/intraday_risk/options were extended to
    accept them explicitly; the remaining 7 (breadth/macro/events/news/
    institutional_flow/timefeat/sector) are MarketEvent-observation-based
    with their own lookback-COUNT loading, a different mechanism this chunk
    doesn't extend (data foundation audit 2026-07-17, historical
    regeneration item -- documented scope boundary, not an oversight).
    Checking the bound method's real signature, rather than hardcoding
    which engines qualify, means this stays correct automatically as more
    engines gain support later."""
    kwargs = {"full": full}
    if "start" in inspect.signature(engine.run).parameters:
        kwargs["start"] = start
        kwargs["end"] = end
    return kwargs


@router.post("/run/{symbol}")
async def run_feature_engines(
    symbol: str,
    timeframe: str = "D",
    full: bool = False,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict]:
    """Compute and store features for one symbol on demand, across every engine.

    `full=true` bypasses incremental watermarks and re-upserts the whole
    history — use after backfilling raw candles older than stored features.

    `start`/`end` (data foundation audit 2026-07-17, historical
    regeneration item) scope regeneration to an explicit date range instead
    of each engine's default trailing-lookback window -- "regenerate March
    2026 for HDFCBANK" rather than only whatever the current lookback
    covers. Providing either bound forces full=True semantics for that
    engine regardless of the `full` argument (see BaseFeatureEngine.run's
    own docstring for why). Applies only to engines whose run() accepts
    these params -- see `_run_kwargs`'s docstring for exactly which.
    """
    results = []
    for engine in _engines():
        try:
            result = await engine.run(symbol, timeframe, **_run_kwargs(engine, full, start, end))
            results.append({"engine": engine.name, **result})
        except Exception as exc:
            # Found live 2026-07-17: this endpoint had no per-engine
            # isolation at all -- one engine raising 500'd the entire
            # multi-engine call, unlike run_all()'s existing per-symbol
            # try/except. First surfaced by IntradayRiskFeatureEngine
            # rejecting the default timeframe="D" (now fixed at the engine
            # level too, see its own run() docstring) -- this is the
            # matching endpoint-level defense so a future engine-specific
            # bug can't repeat the same full-endpoint failure.
            logger.error(
                "feature run failed",
                extra={"engine": engine.name, "symbol": symbol, "error": str(exc)},
            )
            results.append({"engine": engine.name, "symbol": symbol, "error": str(exc)})
    return results


@router.post("/run")
async def run_feature_engines_for_watchlist(
    full: bool = False,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict:
    """Watchlist-wide regeneration across every symbol and every engine
    (data foundation audit 2026-07-17, historical regeneration item) --
    `POST /features/run/{symbol}` scoped to one symbol at a time. Real CPU
    cost at 25-symbol x 16-engine scale (perf-audit-2026-07-14 already
    found this box strains under far less concurrent load during market
    hours) -- deliberately NOT auto-scheduled anywhere; call this only as a
    deliberate, manually-triggered, after-hours operation, same convention
    as `feature_selection_sweep`/`ensemble_training_sweep`'s own
    `after_hours_only` gate in main.py (this endpoint has no such gate
    itself -- the caller is responsible for choosing when, matching how
    the existing per-symbol `/run/{symbol}` also has no gate)."""
    results: dict[str, list[dict]] = {}
    for engine in _engines():
        run_all_params = inspect.signature(engine.run_all).parameters
        if "start" in run_all_params:
            results[engine.name] = await engine.run_all(full=full, start=start, end=end)
        elif "full" in run_all_params:
            results[engine.name] = await engine.run_all(full=full)
        else:
            # The remaining market-wide, observation-based engines
            # (breadth/macro/events/news/institutional_flow/timefeat/
            # sector) override run_all() as a zero-arg single call --
            # unaffected by full/start/end, same scope boundary as
            # _run_kwargs above (relative/intraday_risk were extended to
            # accept full/start/end here; those 7 were not).
            results[engine.name] = await engine.run_all()
    return results
