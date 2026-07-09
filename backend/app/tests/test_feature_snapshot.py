"""Tests for the Feature Snapshot Engine (Volume 5, Prompt 5.3)."""

from datetime import UTC, datetime

from app.prediction.snapshot import FeatureSnapshot, FeatureSnapshotEngine


def test_to_dict_serializes_every_field() -> None:
    snapshot = FeatureSnapshot(
        snapshot_id="abc123",
        symbol="NIFTY",
        timeframe="D",
        as_of=datetime(2026, 7, 9, 10, 0, tzinfo=UTC),
        feature_values={"price_momentum_20": 1.5},
        feature_versions={"price_momentum_20": "v1"},
        market_report={"composite_intelligence_score": 62.0},
        regime={"trend": "strong_bull_trend"},
        collector_versions={"live_market": "1.0.0"},
        model_version=None,
        prediction_version=None,
    )
    payload = snapshot.to_dict()
    assert payload["snapshot_id"] == "abc123"
    assert payload["as_of"] == "2026-07-09T10:00:00+00:00"
    assert payload["feature_values"] == {"price_momentum_20": 1.5}
    assert payload["feature_versions"] == {"price_momentum_20": "v1"}
    assert payload["market_report"] == {"composite_intelligence_score": 62.0}
    assert payload["regime"] == {"trend": "strong_bull_trend"}
    assert payload["collector_versions"] == {"live_market": "1.0.0"}
    assert payload["model_version"] is None
    assert payload["prediction_version"] is None


def test_model_and_prediction_version_default_to_none() -> None:
    """Never fabricated: no model or prediction pipeline exists yet
    (Prompts 5.4/5.6), so these stay honestly None, not a placeholder string."""
    snapshot = FeatureSnapshot(
        snapshot_id="x", symbol="NIFTY", timeframe="D",
        as_of=datetime.now(UTC),
    )
    assert snapshot.model_version is None
    assert snapshot.prediction_version is None


async def test_capture_runs_cleanly_without_a_db_and_still_has_a_market_report() -> None:
    """No session_factory -> feature_values/collector_versions are honestly
    empty, but MarketStateReportEngine.generate() degrades gracefully rather
    than crashing, so the snapshot still captures whatever it can."""
    engine = FeatureSnapshotEngine(session_factory=None)
    snapshot = await engine.capture("NIFTY")

    assert snapshot.symbol == "NIFTY"
    assert snapshot.timeframe == "D"
    assert len(snapshot.snapshot_id) == 32  # uuid4 hex
    assert snapshot.feature_values == {}
    assert snapshot.feature_versions == {}
    assert snapshot.collector_versions == {}
    assert snapshot.model_version is None
    assert snapshot.prediction_version is None
    assert isinstance(snapshot.market_report, dict)
    assert snapshot.market_report.get("symbol") == "NIFTY"


async def test_capture_generates_unique_snapshot_ids() -> None:
    engine = FeatureSnapshotEngine(session_factory=None)
    a = await engine.capture("NIFTY")
    b = await engine.capture("NIFTY")
    assert a.snapshot_id != b.snapshot_id


async def test_get_returns_none_without_a_session_factory() -> None:
    engine = FeatureSnapshotEngine(session_factory=None)
    assert await engine.get("anything") is None


async def test_recent_returns_empty_list_without_a_session_factory() -> None:
    engine = FeatureSnapshotEngine(session_factory=None)
    assert await engine.recent("NIFTY") == []
