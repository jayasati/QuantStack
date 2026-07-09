from functools import partial

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import features as features_api
from app.core.container import container
from app.features.base import BaseFeatureEngine
from app.features.breadth import BreadthFeatureEngine
from app.features.events import EventRiskEngine
from app.features.institutional_flow import InstitutionalFlowFeatureEngine
from app.features.liquidity import LiquidityFeatureEngine
from app.features.news import NewsFeatureEngine
from app.features.options import OptionsFeatureEngine
from app.features.price import PriceFeatureEngine
from app.features.relative import RelativeStrengthEngine
from app.features.sector import SectorFeatureEngine
from app.features.structure import MarketStructureEngine
from app.features.timefeat import TimeFeatureEngine
from app.features.volatility import VolatilityFeatureEngine
from app.features.volume import VolumeFeatureEngine

ENGINE_TYPES = [
    PriceFeatureEngine, VolumeFeatureEngine, VolatilityFeatureEngine,
    LiquidityFeatureEngine, OptionsFeatureEngine, BreadthFeatureEngine,
    SectorFeatureEngine, RelativeStrengthEngine, MarketStructureEngine,
    NewsFeatureEngine, EventRiskEngine, TimeFeatureEngine,
    InstitutionalFlowFeatureEngine,
]


def make_client() -> TestClient:
    for engine_type in ENGINE_TYPES:
        container.register(engine_type, partial(engine_type, session_factory=None))
    app = FastAPI()
    app.include_router(features_api.router)
    return TestClient(app)


def test_registry_lists_every_engine_with_pagination() -> None:
    client = make_client()
    first_page = client.get("/features", params={"limit": 50}).json()
    assert first_page["total"] > 700  # the full cross-engine registry
    assert len(first_page["features"]) == 50
    second_page = client.get("/features", params={"limit": 50, "offset": 50}).json()
    assert second_page["features"][0] != first_page["features"][0]


def test_registry_filtering() -> None:
    client = make_client()
    volatility_only = client.get("/features", params={"category": "volatility"}).json()
    assert volatility_only["total"] == 120
    assert all(f["category"] == "volatility" for f in volatility_only["features"])

    raw_only = client.get(
        "/features", params={"include_normalized": False, "limit": 2000}
    ).json()
    assert all(not f["feature_name"].endswith("_z") for f in raw_only["features"])

    searched = client.get("/features", params={"search": "vix_distance"}).json()
    assert searched["total"] == 12  # 6 windows x (raw + _z)

    owned = client.get("/features", params={"owner": "time_feature_engine"}).json()
    assert owned["total"] == 13


def test_unknown_feature_is_404_everywhere() -> None:
    client = make_client()
    for path in (
        "/features/nonexistent/versions",
        "/features/nonexistent/dependents",
        "/features/nonexistent/quality",
        "/features/nonexistent/drift",
        "/features/history/nonexistent",
    ):
        assert client.get(path).status_code == 404, path


def test_replay_rejects_malformed_timestamp() -> None:
    client = make_client()
    response = client.get("/features/replay/NIFTY", params={"as_of": "not-a-date"})
    assert response.status_code == 422


def test_engine_registry_coverage_matches_container() -> None:
    engines = features_api._engines()
    assert len(engines) == len(ENGINE_TYPES)
    assert all(isinstance(e, BaseFeatureEngine) for e in engines)
    names = {e.name for e in engines}
    assert len(names) == len(engines)  # no duplicate engine names
