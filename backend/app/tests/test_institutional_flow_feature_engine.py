from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.features.institutional_flow import (
    InstitutionalFlowFeatureEngine,
    compute_flow_features,
)
from app.features.normalize import rolling_slope
from app.features.snapshots import Snapshot

BASE_TS = datetime(2026, 7, 7, 9, 15, tzinfo=UTC)


def val(x: float | None) -> float:
    assert x is not None
    return x


def make_snapshot(
    i: int, *, fii: float = 0.4, dii: float = 0.2, etf: float | None = 0.1,
    deal_activity: float = 0.3, promoter: float = 0.15, insider: float = -0.1,
    sast: float = 0.2, participation: float = 0.35,
) -> Snapshot:
    values = {
        "fii_flow": fii,
        "dii_flow": dii,
        "promoter_net": promoter,
        "insider_net": insider,
        "sast_filings": sast,
        "participation_index": participation,
    }
    if etf is not None:
        values["etf_flow"] = etf
    return Snapshot(
        ts=BASE_TS + timedelta(hours=i),
        values=values,
        metadata={"participation_index": {"components": {"deal_activity": deal_activity}}},
    )


def test_passthrough_scores() -> None:
    snapshots = [make_snapshot(i) for i in range(3)]
    series = compute_flow_features(snapshots, windows=(5,))
    assert series["flow_fii_score"][1] == pytest.approx(0.4)
    assert series["flow_dii_score"][1] == pytest.approx(0.2)
    assert series["flow_etf_score"][1] == pytest.approx(0.1)
    assert series["flow_promoter_score"][1] == pytest.approx(0.15)
    assert series["flow_insider_score"][1] == pytest.approx(-0.1)
    assert series["flow_sast_score"][1] == pytest.approx(0.2)


def test_deal_activity_pulled_from_participation_metadata_sidecar() -> None:
    snapshots = [make_snapshot(i, deal_activity=0.55) for i in range(3)]
    series = compute_flow_features(snapshots, windows=(5,))
    assert series["flow_deal_activity_score"][1] == pytest.approx(0.55)


def test_participation_index_reconstructed_from_normalized_score() -> None:
    snapshots = [make_snapshot(i, participation=0.6) for i in range(3)]
    series = compute_flow_features(snapshots, windows=(5,))
    assert series["flow_participation_score"][1] == pytest.approx(0.6)
    assert series["flow_participation_index"][1] == pytest.approx(50 + 50 * 0.6)


def test_etf_missing_stays_none_when_absent() -> None:
    snapshots = [make_snapshot(i, etf=None) for i in range(3)]
    series = compute_flow_features(snapshots, windows=(5,))
    assert series["flow_etf_score"][1] is None


def test_fii_momentum_tracks_rising_flow() -> None:
    snapshots = [make_snapshot(i, fii=0.1 + 0.05 * i) for i in range(15)]
    series = compute_flow_features(snapshots, windows=(10,))
    assert val(series["flow_fii_score_momentum_10"][14]) > 0
    expected = rolling_slope(series["flow_fii_score"], 10)[14]
    assert series["flow_fii_score_momentum_10"][14] == pytest.approx(val(expected))


def test_momentum_none_before_window_fills() -> None:
    snapshots = [make_snapshot(i) for i in range(5)]
    series = compute_flow_features(snapshots, windows=(10,))
    assert series["flow_fii_score_momentum_10"][4] is None


def test_missing_metrics_stay_none() -> None:
    empty = Snapshot(ts=BASE_TS, values={}, metadata={})
    series = compute_flow_features([empty, make_snapshot(1)], windows=(5,))
    assert series["flow_fii_score"][0] is None
    assert series["flow_deal_activity_score"][0] is None
    assert series["flow_fii_score"][1] is not None


def test_every_feature_has_z_companion_and_registration() -> None:
    snapshots = [make_snapshot(i) for i in range(20)]
    series = compute_flow_features(snapshots, windows=(5,), normalization_window=10)
    raw = [name for name in series if not name.endswith("_z")]
    for name in raw:
        assert f"{name}_z" in series

    engine = InstitutionalFlowFeatureEngine(settings=Settings(feature_windows=[5, 10]))
    definitions = engine.registry.list_definitions(category="institutional_flow")
    # 9 per-snapshot + 3 momentum bases x 2 windows = 15 raw, doubled by _z.
    assert len(definitions) == 15 * 2
    assert all(d.version == "v1" for d in definitions)
    order = engine.registry.dependency_order()
    assert order.index("flow_fii_score") < order.index("flow_fii_score_momentum_5")
