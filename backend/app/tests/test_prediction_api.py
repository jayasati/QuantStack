"""HTTP-level tests for api/prediction.py (IRR-2026-07-11 finding #2).

Before this file, `api/prediction.py` -- 586 lines, ~44 distinct routes,
by far the largest and most consequential router in the app -- had zero
HTTP-level tests. Every engine it resolves already has its own unit tests
(pure-function / stubbed-dependency level); what was missing is proof that
the router itself wires up, that FastAPI's request/response validation
matches what each engine actually returns, and -- the specific gap called
out by the audit -- coverage of the lifecycle mutation endpoints, which
used to be GET (implicated in the race-condition bug fixed in `ea24e96`)
and are now POST (see prediction/lifecycle.py's OpportunityLifecycleManager
docstring on IRR Critical #2).

Every engine other than OpportunityLifecycleManager is registered with
session_factory=None (the established "usable standalone, no DB" pattern
already used throughout this codebase, e.g. test_features_api.py) --
against an empty feature store they return honest neutral/insufficient-data
results rather than raising, which is exactly the behavior this file
verifies. OpportunityLifecycleManager is wired to a REAL test Postgres
(test_session_factory) because its mutation endpoints' actual persistence
and concurrency behavior is the highest-value thing to prove here.
"""

import asyncio
from functools import partial

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.api import prediction as prediction_api
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
from app.prediction.labeling import TripleBarrierLabelingEngine
from app.prediction.lifecycle import OpportunityLifecycleManager
from app.prediction.market_context import MarketContextAdjustmentEngine
from app.prediction.multi_horizon import MultiHorizonPredictionEngine
from app.prediction.opportunity import OpportunityDetectionEngine
from app.prediction.priority import SignalPriorityEngine
from app.prediction.qualification import TradeQualificationEngine
from app.prediction.snapshot import FeatureSnapshotEngine

STANDALONE_ENGINE_TYPES = [
    OpportunityDetectionEngine, CandidateGenerationEngine, FeatureSnapshotEngine,
    MultiHorizonPredictionEngine, TripleBarrierLabelingEngine, EnsemblePredictionEngine,
    ProbabilityCalibrationEngine, ModelAgreementEngine, HistoricalSimilarityEngine,
    MarketContextAdjustmentEngine, ConvictionEngine, TradeQualificationEngine,
    SignalPriorityEngine, DuplicateSignalEngine, ExplainabilityReportEngine,
    AlphaResearchEngine,
]

SYMBOL = "TESTSYM"


def make_client(lifecycle_session_factory=None) -> TestClient:
    for engine_type in STANDALONE_ENGINE_TYPES:
        container.register(engine_type, partial(engine_type, session_factory=None))
    container.register(
        OpportunityLifecycleManager,
        partial(OpportunityLifecycleManager, session_factory=lifecycle_session_factory),
    )
    app = FastAPI()
    app.include_router(prediction_api.router)
    return TestClient(app)


@pytest.fixture
def client() -> TestClient:
    return make_client()


# --- Read-only engines: opportunities / candidates / snapshots --------------

def test_scan_opportunities(client: TestClient) -> None:
    response = client.get("/prediction/opportunities")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_symbol_opportunity_history(client: TestClient) -> None:
    response = client.get(f"/prediction/opportunities/{SYMBOL}")
    assert response.status_code == 200
    assert response.json() == []


def test_generate_candidates(client: TestClient) -> None:
    response = client.get("/prediction/candidates")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_symbol_candidate_history(client: TestClient) -> None:
    response = client.get(f"/prediction/candidates/{SYMBOL}")
    assert response.status_code == 200
    assert response.json() == []


def test_get_snapshot_unknown_id_returns_404(client: TestClient) -> None:
    response = client.get("/prediction/snapshots/does-not-exist")
    assert response.status_code == 404


def test_snapshot_history(client: TestClient) -> None:
    response = client.get("/prediction/snapshots")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


# --- Horizons / labels -------------------------------------------------------

def test_predict_horizons(client: TestClient) -> None:
    response = client.get(f"/prediction/horizons/{SYMBOL}")
    assert response.status_code == 200
    assert "predictions" in response.json() or isinstance(response.json(), dict)


def test_horizon_history(client: TestClient) -> None:
    response = client.get(f"/prediction/horizons/{SYMBOL}/history")
    assert response.status_code == 200
    assert response.json() == []


def test_generate_labels(client: TestClient) -> None:
    response = client.get(f"/prediction/labels/{SYMBOL}")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_label_history(client: TestClient) -> None:
    response = client.get(f"/prediction/labels/{SYMBOL}/history")
    assert response.status_code == 200
    assert response.json() == []


# --- Ensemble / calibration / agreement --------------------------------------

def test_train_ensemble_with_insufficient_data(client: TestClient) -> None:
    response = client.post(f"/prediction/ensemble/{SYMBOL}/train")
    assert response.status_code == 200
    # No feature history for TESTSYM -> nothing to train on.
    assert response.json()["n_samples"] == 0
    assert response.json()["models"] == []


def test_predict_ensemble(client: TestClient) -> None:
    response = client.get(f"/prediction/ensemble/{SYMBOL}")
    assert response.status_code == 200


def test_ensemble_history(client: TestClient) -> None:
    response = client.get(f"/prediction/ensemble/{SYMBOL}/history")
    assert response.status_code == 200
    assert response.json() == []


def test_train_calibration_with_insufficient_data(client: TestClient) -> None:
    response = client.post(f"/prediction/calibration/{SYMBOL}/train")
    assert response.status_code == 200
    assert response.json()["calibrated"] is False


def test_predict_calibrated(client: TestClient) -> None:
    response = client.get(f"/prediction/calibration/{SYMBOL}")
    assert response.status_code == 200


def test_calibration_history(client: TestClient) -> None:
    response = client.get(f"/prediction/calibration/{SYMBOL}/history")
    assert response.status_code == 200
    assert response.json() == []


def test_model_agreement(client: TestClient) -> None:
    response = client.get(f"/prediction/agreement/{SYMBOL}")
    assert response.status_code == 200


def test_agreement_history(client: TestClient) -> None:
    response = client.get(f"/prediction/agreement/{SYMBOL}/history")
    assert response.status_code == 200
    assert response.json() == []


# --- Similarity / context / conviction / qualification ----------------------

def test_historical_similarity_for_candidates(client: TestClient) -> None:
    response = client.get("/prediction/similarity/candidates")
    assert response.status_code == 200
    assert response.json() == []


def test_historical_similarity(client: TestClient) -> None:
    response = client.get(f"/prediction/similarity/{SYMBOL}")
    assert response.status_code == 200


def test_similarity_history(client: TestClient) -> None:
    response = client.get(f"/prediction/similarity/{SYMBOL}/history")
    assert response.status_code == 200
    assert response.json() == []


def test_market_context_adjustment(client: TestClient) -> None:
    response = client.get(f"/prediction/context/{SYMBOL}")
    assert response.status_code == 200


def test_market_context_history(client: TestClient) -> None:
    response = client.get(f"/prediction/context/{SYMBOL}/history")
    assert response.status_code == 200
    assert response.json() == []


def test_conviction_for_candidates(client: TestClient) -> None:
    response = client.get("/prediction/conviction/candidates")
    assert response.status_code == 200
    assert response.json() == []


def test_conviction(client: TestClient) -> None:
    response = client.get(f"/prediction/conviction/{SYMBOL}")
    assert response.status_code == 200


def test_conviction_history(client: TestClient) -> None:
    response = client.get(f"/prediction/conviction/{SYMBOL}/history")
    assert response.status_code == 200
    assert response.json() == []


def test_qualification_for_candidates(client: TestClient) -> None:
    response = client.get("/prediction/qualification/candidates")
    assert response.status_code == 200
    assert response.json() == []


def test_qualified_trades(client: TestClient) -> None:
    response = client.get("/prediction/qualification/qualified")
    assert response.status_code == 200
    assert response.json() == []


def test_trade_qualification(client: TestClient) -> None:
    response = client.get(f"/prediction/qualification/{SYMBOL}")
    assert response.status_code == 200


def test_qualification_history(client: TestClient) -> None:
    response = client.get(f"/prediction/qualification/{SYMBOL}/history")
    assert response.status_code == 200
    assert response.json() == []


# --- Priority / signals ------------------------------------------------------

def test_signal_priority(client: TestClient) -> None:
    response = client.get("/prediction/priority")
    assert response.status_code == 200
    assert response.json() == []


def test_signal_priority_history(client: TestClient) -> None:
    response = client.get("/prediction/priority/history")
    assert response.status_code == 200
    assert response.json() == []


def test_deduplicated_signals(client: TestClient) -> None:
    response = client.get("/prediction/signals")
    assert response.status_code == 200


def test_deduplicated_signals_history(client: TestClient) -> None:
    response = client.get("/prediction/signals/history")
    assert response.status_code == 200
    assert response.json() == []


# --- Explainability / alpha research -----------------------------------------

def test_explainability_for_qualified_candidates(client: TestClient) -> None:
    response = client.get("/prediction/explainability/qualified")
    assert response.status_code == 200
    assert response.json() == []


def test_explainability_report(client: TestClient) -> None:
    response = client.get(f"/prediction/explainability/{SYMBOL}")
    assert response.status_code == 200


def test_explainability_history(client: TestClient) -> None:
    response = client.get(f"/prediction/explainability/{SYMBOL}/history")
    assert response.status_code == 200
    assert response.json() == []


def test_alpha_research_feature_leaderboard(client: TestClient) -> None:
    response = client.get("/prediction/research/leaderboard/features")
    assert response.status_code == 200
    assert response.json() == []


def test_alpha_research_comparison_leaderboard(client: TestClient) -> None:
    response = client.get("/prediction/research/leaderboard/comparisons")
    assert response.status_code == 200
    assert response.json() == []


def test_alpha_research_features(client: TestClient) -> None:
    response = client.get(f"/prediction/research/{SYMBOL}/features")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_alpha_research_recommendations(client: TestClient) -> None:
    response = client.get(f"/prediction/research/{SYMBOL}/recommendations")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_alpha_research_compare(client: TestClient) -> None:
    response = client.get(f"/prediction/research/{SYMBOL}/compare")
    assert response.status_code == 200


# --- Lifecycle mutation endpoints (the ex-GET-for-mutation race-condition ---
# --- endpoints, now POST -- IRR Critical #2 / IRR-2026-07-11 finding #2) ----
#
# These use httpx.AsyncClient(transport=ASGITransport(...)) rather than the
# sync starlette TestClient: TestClient drives the ASGI app from its own
# background thread with its own event loop, but test_session_factory's
# AsyncConnection is bound to *this* test's event loop -- asyncpg then
# raises "attached to a different loop" the moment a route handler touches
# it. AsyncClient runs the app in-process on the same loop as the test.


def make_lifecycle_app(session_factory) -> FastAPI:
    container.register(
        OpportunityLifecycleManager,
        partial(OpportunityLifecycleManager, session_factory=session_factory),
    )
    app = FastAPI()
    app.include_router(prediction_api.router)
    return app


@pytest.mark.db
async def test_lifecycle_full_happy_path(test_session_factory) -> None:
    app = make_lifecycle_app(test_session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        detect = await ac.post("/prediction/lifecycle/detect", params={"symbol": SYMBOL})
        assert detect.status_code == 200
        lifecycle_id = detect.json()["lifecycle_id"]
        assert detect.json()["stage"] == "detected"

        for stage_path, expected_stage in [
            ("confirm", "confirmed"),
            ("qualify", "qualified"),
            ("sent", "sent"),
            ("monitor", "monitoring"),
            ("succeed", "succeeded"),
        ]:
            response = await ac.post(f"/prediction/lifecycle/{lifecycle_id}/{stage_path}")
            assert response.status_code == 200, response.text
            assert response.json()["stage"] == expected_stage

        get_response = await ac.get(f"/prediction/lifecycle/{lifecycle_id}")
        assert get_response.status_code == 200
        assert get_response.json()["stage"] == "succeeded"

        history = await ac.get(f"/prediction/lifecycle/{lifecycle_id}/history")
        assert history.status_code == 200
        assert len(history.json()) == 6  # detected + 5 transitions above


@pytest.mark.db
async def test_lifecycle_expire_and_fail_paths(test_session_factory) -> None:
    app = make_lifecycle_app(test_session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        detect = await ac.post("/prediction/lifecycle/detect", params={"symbol": SYMBOL})
        lifecycle_id = detect.json()["lifecycle_id"]
        response = await ac.post(
            f"/prediction/lifecycle/{lifecycle_id}/expire", params={"reason": "timed_out"}
        )
        assert response.status_code == 200
        assert response.json()["stage"] == "expired"

        detect2 = await ac.post("/prediction/lifecycle/detect", params={"symbol": SYMBOL})
        lifecycle_id2 = detect2.json()["lifecycle_id"]
        await ac.post(f"/prediction/lifecycle/{lifecycle_id2}/confirm")
        fail_response = await ac.post(f"/prediction/lifecycle/{lifecycle_id2}/fail")
        assert fail_response.status_code == 200
        assert fail_response.json()["stage"] == "failed"


@pytest.mark.db
async def test_lifecycle_invalid_transition_returns_409(test_session_factory) -> None:
    app = make_lifecycle_app(test_session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        detect = await ac.post("/prediction/lifecycle/detect", params={"symbol": SYMBOL})
        lifecycle_id = detect.json()["lifecycle_id"]

        # Non-terminal stages must be entered strictly in order -- can't
        # skip "confirmed" and jump straight to "qualified". (A terminal
        # stage like "succeeded" may legitimately be entered from any
        # non-terminal stage, so that's not a usable invalid case here.)
        response = await ac.post(f"/prediction/lifecycle/{lifecycle_id}/qualify")
        assert response.status_code == 409

        # Once terminal, no further transitions -- not even another
        # terminal one.
        await ac.post(f"/prediction/lifecycle/{lifecycle_id}/succeed")
        response = await ac.post(f"/prediction/lifecycle/{lifecycle_id}/fail")
        assert response.status_code == 409


@pytest.mark.db
async def test_lifecycle_unknown_id_returns_404(test_session_factory) -> None:
    app = make_lifecycle_app(test_session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/prediction/lifecycle/does-not-exist")
        assert response.status_code == 404


@pytest.mark.db
async def test_concurrent_confirm_requests_apply_exactly_once(test_session_factory) -> None:
    """HTTP-level version of the race condition ea24e96 fixed. The lock
    makes a duplicate confirm() an idempotent no-op rather than an error
    (see lifecycle.py's _advance -- deliberately, so a retried/duplicate
    client call is safe rather than surfacing as a spurious failure), so
    the guarantee under test isn't "one wins, one 409s": it's that the
    transition log ends up with exactly ONE 'confirmed' row no matter how
    the two requests interleave, and both callers see a consistent
    'confirmed' state -- proving the lock actually serialized the
    read-modify-write instead of both racing off the same stale read."""
    app = make_lifecycle_app(test_session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        detect = await ac.post("/prediction/lifecycle/detect", params={"symbol": SYMBOL})
        lifecycle_id = detect.json()["lifecycle_id"]

        results = await asyncio.gather(
            ac.post(f"/prediction/lifecycle/{lifecycle_id}/confirm"),
            ac.post(f"/prediction/lifecycle/{lifecycle_id}/confirm"),
        )

        assert all(r.status_code == 200 for r in results)
        assert all(r.json()["stage"] == "confirmed" for r in results)

        history = await ac.get(f"/prediction/lifecycle/{lifecycle_id}/history")
        confirmed_transitions = [
            row for row in history.json() if row.get("stage") == "confirmed"
        ]
        assert len(confirmed_transitions) == 1
