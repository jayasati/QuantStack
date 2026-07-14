"""Real-Postgres persistence tests (IRR-2026-07-11 finding #4).

Every engine's actual DB-backed persist/get/recent implementation was
previously only ever exercised with `session_factory=None` (the no-op
bypass branch). These tests wire the real session_factory from the
`test_session_factory` fixture (app/tests/conftest.py) so the JSONB
read/write paths -- and the queries built on top of MarketEvent.data's
JSON operators -- actually run against a real Postgres.
"""

import shutil
from datetime import UTC, datetime

import pytest

from app.core.config import REPO_ROOT
from app.features.schema import FeatureValue
from app.features.store import FeatureStore
from app.intelligence.base import IntelligenceResult
from app.intelligence.explain import ExplainabilityStore
from app.intelligence.regime import BayesianRegimeDetector

pytestmark = pytest.mark.db


# --- BayesianRegimeDetector: belief persistence -----------------------------

async def test_regime_belief_round_trips_through_real_postgres(test_session_factory) -> None:
    detector = BayesianRegimeDetector(session_factory=test_session_factory)
    result = IntelligenceResult(
        component="fake", score=70.0, confidence=0.8,
        states={"bullish": 0.7, "bearish": 0.3},
    )
    await detector.update_from_result("trend", "TESTSYM", "D", result)

    history = await detector.history("trend", "TESTSYM", "D")
    assert len(history) == 1
    assert history[0]["bullish"] == pytest.approx(0.7)


async def test_regime_belief_blends_with_a_real_stored_prior(test_session_factory) -> None:
    detector = BayesianRegimeDetector(session_factory=test_session_factory)
    first = IntelligenceResult(
        component="fake", score=70.0, confidence=0.8,
        states={"bullish": 1.0, "bearish": 0.0},
    )
    second = IntelligenceResult(
        component="fake", score=30.0, confidence=0.8,
        states={"bullish": 0.0, "bearish": 1.0},
    )
    await detector.update_from_result("trend", "TESTSYM", "D", first)
    await detector.update_from_result("trend", "TESTSYM", "D", second)

    history = await detector.history("trend", "TESTSYM", "D")
    assert len(history) == 2
    # Second observation blended against the real stored prior, not just
    # the raw likelihood -- bearish should have moved but not hit 1.0.
    assert 0.0 < history[-1]["bearish"] < 1.0


# --- ExplainabilityStore: audit-log persistence -----------------------------

async def test_explainability_record_round_trips_through_real_postgres(
    test_session_factory,
) -> None:
    store = ExplainabilityStore(session_factory=test_session_factory)
    result = IntelligenceResult(
        component="fake", score=55.0, confidence=0.6,
        reasoning=["because reasons"],
    )
    await store.record("trend", "TESTSYM", "D", result)

    history = await store.history("trend", "TESTSYM", "D")
    assert len(history) == 1
    assert history[0]["score"] == 55.0
    assert history[0]["reasoning"] == ["because reasons"]


async def test_explainability_history_is_scoped_by_symbol(test_session_factory) -> None:
    store = ExplainabilityStore(session_factory=test_session_factory)
    await store.record("trend", "TESTSYM_A", "D", IntelligenceResult(
        component="fake", score=10.0, confidence=0.5,
    ))
    await store.record("trend", "TESTSYM_B", "D", IntelligenceResult(
        component="fake", score=90.0, confidence=0.5,
    ))

    history_a = await store.history("trend", "TESTSYM_A", "D")
    assert len(history_a) == 1
    assert history_a[0]["score"] == 10.0


# --- FeatureStore: offline (Postgres) round trip -----------------------------

@pytest.fixture(autouse=True)
def _clean_test_parquet_partitions():
    """FeatureStore.write() always archives to a real Parquet partition
    under the repo's data/ dir regardless of session_factory -- clean up
    the TESTSYM partitions these tests create so they don't linger in the
    working tree."""
    yield
    for symbol in ("TESTSYM",):
        partition = REPO_ROOT / "data" / "feature_store_parquet" / f"symbol={symbol}"
        shutil.rmtree(partition, ignore_errors=True)


async def test_feature_store_offline_write_and_latest_round_trip(test_session_factory) -> None:
    store = FeatureStore(session_factory=test_session_factory)
    now = datetime.now(UTC)
    await store.write([
        FeatureValue(
            feature_name="test_feature", feature_version="v1",
            symbol="TESTSYM", timeframe="D", ts=now, value=42.0,
        ),
    ])

    latest = await store.latest("TESTSYM", "D")
    assert latest["test_feature"]["value"] == 42.0


async def test_feature_store_upsert_updates_value_on_conflict(test_session_factory) -> None:
    store = FeatureStore(session_factory=test_session_factory)
    now = datetime.now(UTC)
    value = FeatureValue(
        feature_name="test_feature", feature_version="v1",
        symbol="TESTSYM", timeframe="D", ts=now, value=1.0,
    )
    await store.write([value])
    await store.write([FeatureValue(**{**value.__dict__, "value": 2.0})])

    latest = await store.latest("TESTSYM", "D")
    assert latest["test_feature"]["value"] == 2.0

    history = await store.history("test_feature", symbol="TESTSYM", timeframe="D")
    assert len(history) == 1  # upsert, not a second row
