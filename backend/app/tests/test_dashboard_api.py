from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import dashboard as dashboard_api


def make_client() -> TestClient:
    app = FastAPI()
    app.include_router(dashboard_api.router)
    return TestClient(app)


def test_intelligence_dashboard_page_serves_the_static_html() -> None:
    client = make_client()
    response = client.get("/dashboard/intelligence")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Market Intelligence Dashboard" in response.text
