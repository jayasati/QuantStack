from functools import partial

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import intelligence as intelligence_api
from app.core.container import container
from app.intelligence.analogs import HistoricalAnalogEngine
from app.intelligence.breadth import BreadthIntelligenceEngine
from app.intelligence.composite import CompositeMarketIntelligenceEngine
from app.intelligence.confidence import MarketConfidenceEngine
from app.intelligence.correlation import CorrelationIntelligenceEngine
from app.intelligence.institutional_flow import InstitutionalFlowIntelligenceEngine
from app.intelligence.liquidity import LiquidityIntelligenceEngine
from app.intelligence.regime import BayesianRegimeDetector
from app.intelligence.report import MarketStateReportEngine
from app.intelligence.sector import SectorIntelligenceEngine
from app.intelligence.trend import TrendIntelligenceEngine

ENGINE_TYPES = [
    TrendIntelligenceEngine, BreadthIntelligenceEngine, SectorIntelligenceEngine,
    InstitutionalFlowIntelligenceEngine, HistoricalAnalogEngine, MarketConfidenceEngine,
    MarketStateReportEngine, BayesianRegimeDetector, CompositeMarketIntelligenceEngine,
    LiquidityIntelligenceEngine, CorrelationIntelligenceEngine,
]


def make_client() -> TestClient:
    for engine_type in ENGINE_TYPES:
        container.register(engine_type, partial(engine_type, session_factory=None))
    app = FastAPI()
    app.include_router(intelligence_api.router)
    return TestClient(app)


def test_current_market_state_is_a_pure_read() -> None:
    """GET /state/{symbol} no longer generates a report as a side effect
    (that was the same GET-mutation smell flagged for Volume 5) -- with
    nothing ever persisted (session_factory=None), it honestly 404s rather
    than silently computing and returning a fresh one."""
    client = make_client()
    response = client.get("/intelligence/state/NIFTY")
    assert response.status_code == 404


def test_generate_market_state_via_post() -> None:
    """POST /state/{symbol}/generate is the mutating counterpart -- this is
    where report generation actually happens now."""
    client = make_client()
    response = client.post("/intelligence/state/NIFTY/generate")
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "NIFTY"
    assert "composite_intelligence_score" in body
    assert "trend_summary" in body


def test_historical_market_state_404_when_nothing_persisted() -> None:
    client = make_client()
    response = client.get(
        "/intelligence/state/NOSUCHSYMBOL/history", params={"as_of": "2000-01-01T00:00:00Z"}
    )
    assert response.status_code == 404


def test_historical_market_state_rejects_malformed_as_of() -> None:
    client = make_client()
    response = client.get("/intelligence/state/NIFTY/history", params={"as_of": "not-a-date"})
    assert response.status_code == 422


def test_market_intelligence_reports_list() -> None:
    client = make_client()
    response = client.get("/intelligence/reports/NIFTY", params={"limit": 5})
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "NIFTY"
    assert "reports" in body
    assert isinstance(body["reports"], list)


def test_composite_market_intelligence() -> None:
    client = make_client()
    response = client.get("/intelligence/composite/NIFTY")
    assert response.status_code == 200
    body = response.json()
    assert body["component"] == "composite_market_intelligence"
    assert "score" in body and "confidence" in body
    assert "bullishness" in body["metrics"] and "expected_risk" in body["metrics"]


def test_regime_history() -> None:
    client = make_client()
    response = client.get("/intelligence/regime/trend/NIFTY/D")
    assert response.status_code == 200
    body = response.json()
    assert body["component"] == "trend"
    assert "history" in body


def test_trend_intelligence() -> None:
    client = make_client()
    response = client.get("/intelligence/trend/NIFTY")
    assert response.status_code == 200
    body = response.json()
    assert body["component"] == "trend"
    assert "score" in body and "confidence" in body


def test_breadth_intelligence() -> None:
    client = make_client()
    response = client.get("/intelligence/breadth")
    assert response.status_code == 200
    assert response.json()["component"] == "breadth"


def test_sector_intelligence() -> None:
    client = make_client()
    response = client.get("/intelligence/sector")
    assert response.status_code == 200
    assert response.json()["component"] == "sector"


def test_institutional_flow_intelligence() -> None:
    client = make_client()
    response = client.get("/intelligence/institutional-flow")
    assert response.status_code == 200
    assert response.json()["component"] == "institutional_flow"


def test_historical_analogs() -> None:
    client = make_client()
    response = client.get("/intelligence/analogs/NIFTY")
    assert response.status_code == 200
    body = response.json()
    assert body["component"] == "historical_analogs"
    assert "analogs" in body["metrics"]


def test_liquidity_intelligence() -> None:
    client = make_client()
    response = client.get("/intelligence/liquidity/NIFTY")
    assert response.status_code == 200
    body = response.json()
    assert body["component"] == "liquidity"
    assert "liquidity_stress" in body["metrics"] and "execution_risk" in body["metrics"]


def test_correlation_intelligence() -> None:
    client = make_client()
    response = client.get("/intelligence/correlation")
    assert response.status_code == 200
    body = response.json()
    assert body["component"] == "correlation"
    assert "correlation_matrix" in body["metrics"]


def test_market_confidence() -> None:
    client = make_client()
    response = client.get("/intelligence/confidence/NIFTY")
    assert response.status_code == 200
    body = response.json()
    assert body["component"] == "market_confidence"
    assert "confidence_grade" in body["metrics"]
