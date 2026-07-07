"""Feature store observability and access API (Volume 3)."""

from fastapi import APIRouter, HTTPException, Query

from app.core.container import container
from app.features.base import BaseFeatureEngine
from app.features.breadth import BreadthFeatureEngine
from app.features.liquidity import LiquidityFeatureEngine
from app.features.options import OptionsFeatureEngine
from app.features.price import PriceFeatureEngine
from app.features.relative import RelativeStrengthEngine
from app.features.schema import FeatureDefinition
from app.features.sector import SectorFeatureEngine
from app.features.volatility import VolatilityFeatureEngine
from app.features.volume import VolumeFeatureEngine

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
    ]


def _owning_engine(feature_name: str) -> tuple[BaseFeatureEngine, FeatureDefinition]:
    for engine in _engines():
        definition = engine.registry.get(feature_name)
        if definition is not None:
            return engine, definition
    raise HTTPException(status_code=404, detail=f"unknown feature: {feature_name}")


@router.get("")
async def list_features(category: str | None = None) -> list[dict]:
    """Registered feature metadata (Chapter 5), across every engine."""
    return [
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
        for engine in _engines()
        for d in engine.registry.list_definitions(category=category)
    ]


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
            select(FeatureVersion)
            .where(FeatureVersion.feature_name == feature_name)
            .order_by(FeatureVersion.id)
        )
        rows = result.scalars().all()
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


@router.post("/run/{symbol}")
async def run_feature_engines(symbol: str, timeframe: str = "D", full: bool = False) -> list[dict]:
    """Compute and store features for one symbol on demand, across every engine.

    `full=true` bypasses incremental watermarks and re-upserts the whole
    history — use after backfilling raw candles older than stored features.
    """
    results = []
    for engine in _engines():
        result = await engine.run(symbol, timeframe, full=full)
        results.append({"engine": engine.name, **result})
    return results
