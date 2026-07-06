"""Feature store observability and access API (Volume 3)."""

from fastapi import APIRouter, HTTPException, Query

from app.core.container import container
from app.features.price import PriceFeatureEngine

router = APIRouter(prefix="/features", tags=["features"])


@router.get("")
async def list_features(category: str | None = None) -> list[dict]:
    """Registered feature metadata (Chapter 5)."""
    engine = container.resolve(PriceFeatureEngine)
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
        for d in engine.registry.list_definitions(category=category)
    ]


@router.get("/latest/{symbol}")
async def latest_features(symbol: str, timeframe: str = "D") -> dict:
    """Latest value of every feature for a symbol (online store, offline fallback)."""
    engine = container.resolve(PriceFeatureEngine)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "features": await engine.store.latest(symbol, timeframe),
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
    engine = container.resolve(PriceFeatureEngine)
    if engine.registry.get(feature_name) is None:
        raise HTTPException(status_code=404, detail=f"unknown feature: {feature_name}")
    rows = await engine.store.history(
        feature_name, symbol=symbol, timeframe=timeframe,
        version=version, limit=limit, offset=offset,
    )
    return {"feature_name": feature_name, "limit": limit, "offset": offset, "history": rows}


@router.get("/{feature_name}/dependents")
async def feature_dependents(feature_name: str) -> dict:
    """Downstream features to recompute when this one changes (Chapter 7)."""
    engine = container.resolve(PriceFeatureEngine)
    if engine.registry.get(feature_name) is None:
        raise HTTPException(status_code=404, detail=f"unknown feature: {feature_name}")
    return {
        "feature_name": feature_name,
        "dependents": engine.registry.dependents_of(feature_name),
    }


@router.get("/{feature_name}/versions")
async def feature_versions(feature_name: str) -> dict:
    """Published version history for one feature (Chapter 6)."""
    engine = container.resolve(PriceFeatureEngine)
    if engine.registry.get(feature_name) is None:
        raise HTTPException(status_code=404, detail=f"unknown feature: {feature_name}")
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
async def run_feature_engine(symbol: str, timeframe: str = "D") -> dict:
    """Compute and store price features for one symbol on demand."""
    engine = container.resolve(PriceFeatureEngine)
    return await engine.run(symbol, timeframe)
