import math
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.features.options import (
    TRADING_DAYS,
    ChainSnapshot,
    OptionsFeatureEngine,
    bucket_snapshots,
    compute_options_features,
)

BASE_TS = datetime(2026, 7, 7, 9, 15, tzinfo=UTC)


def val(x: float | None) -> float:
    assert x is not None
    return x


def make_snapshot(i: int, *, atm_iv: float = 12.0, total_call_oi: float = 1_000_000,
                  total_put_oi: float = 1_200_000, spot: float = 25_000.0,
                  max_pain: float = 25_100.0, call_writing: float = 0.02,
                  put_writing: float = 0.05, volume_pcr: float = 1.1,
                  gamma: float | None = None) -> ChainSnapshot:
    values = {
        "pcr": total_put_oi / total_call_oi,
        "atm_iv": atm_iv,
        "call_writing": call_writing,
        "put_writing": put_writing,
    }
    if gamma is not None:
        values["gamma_exposure"] = gamma
    return ChainSnapshot(
        ts=BASE_TS + timedelta(minutes=i),
        values=values,
        metadata={
            "pcr": {"total_call_oi": total_call_oi, "total_put_oi": total_put_oi},
            "max_pain": {"spot": spot, "distance_from_spot": max_pain - spot},
            "volume_distribution": {"volume_pcr": volume_pcr},
        },
    )


def test_bucket_snapshots_groups_one_collector_run() -> None:
    ts = BASE_TS
    observations = [
        (ts, "pcr", 1.2, {"total_call_oi": 100, "total_put_oi": 120}),
        (ts + timedelta(seconds=1), "atm_iv", 12.5, {}),
        (ts + timedelta(minutes=2), "pcr", 1.3, {}),  # next run
    ]
    snapshots = bucket_snapshots(observations)
    assert len(snapshots) == 2
    assert snapshots[0].values == {"pcr": 1.2, "atm_iv": 12.5}
    assert snapshots[0].ts == ts + timedelta(seconds=1)  # latest ts in the bucket
    assert snapshots[1].values == {"pcr": 1.3}


def test_pcr_and_writing_passthrough() -> None:
    snapshots = [make_snapshot(i) for i in range(5)]
    series = compute_options_features(snapshots)
    assert series["options_pcr"][2] == pytest.approx(1.2)
    assert series["options_call_writing_score"][2] == pytest.approx(0.02)
    assert series["options_put_writing_score"][2] == pytest.approx(0.05)


def test_oi_change_pct_between_snapshots() -> None:
    snapshots = [
        make_snapshot(0, total_call_oi=1_000_000, total_put_oi=1_000_000),
        make_snapshot(1, total_call_oi=1_050_000, total_put_oi=1_050_000),
    ]
    series = compute_options_features(snapshots)
    assert series["options_oi_change_pct"][0] is None
    assert series["options_oi_change_pct"][1] == pytest.approx(5.0)


def test_max_pain_distance_and_expected_move() -> None:
    snapshots = [make_snapshot(i, spot=25_000.0, max_pain=25_250.0, atm_iv=16.0)
                 for i in range(3)]
    series = compute_options_features(snapshots)
    assert series["options_max_pain_distance_pct"][1] == pytest.approx(250 / 25_000 * 100)
    expected = 25_000.0 * 16.0 / 100 / math.sqrt(TRADING_DAYS)
    assert series["options_expected_move"][1] == pytest.approx(expected)


def test_iv_rank_and_percentile_on_ramp() -> None:
    # IV climbs steadily: the newest snapshot is the max of its window.
    snapshots = [make_snapshot(i, atm_iv=10.0 + i * 0.1) for i in range(40)]
    series = compute_options_features(snapshots, normalization_window=30)
    last = len(snapshots) - 1
    assert series["options_iv_rank"][last] == pytest.approx(100.0)
    assert series["options_iv_percentile"][last] == pytest.approx(100.0)
    assert series["options_iv_rank"][0] is None  # cold start


def test_dealer_positioning_balance() -> None:
    put_heavy = compute_options_features(
        [make_snapshot(i, call_writing=0.01, put_writing=0.05) for i in range(3)]
    )
    assert val(put_heavy["options_dealer_positioning"][1]) == pytest.approx(0.04 / 0.06)
    call_heavy = compute_options_features(
        [make_snapshot(i, call_writing=0.06, put_writing=0.02) for i in range(3)]
    )
    assert val(call_heavy["options_dealer_positioning"][1]) < 0


def test_greeks_pass_through_only_when_present() -> None:
    snapshots = [
        make_snapshot(0, gamma=None),
        make_snapshot(1, gamma=1_500.0),
    ]
    series = compute_options_features(snapshots)
    assert series["options_gamma_exposure"][0] is None
    assert series["options_gamma_exposure"][1] == pytest.approx(1_500.0)


def test_volume_ratio_from_metadata() -> None:
    series = compute_options_features([make_snapshot(0, volume_pcr=0.85),
                                       make_snapshot(1, volume_pcr=0.85)])
    assert series["options_volume_ratio"][1] == pytest.approx(0.85)


def test_every_feature_has_z_companion_and_registration() -> None:
    series = compute_options_features([make_snapshot(i) for i in range(20)],
                                      normalization_window=10)
    raw = [name for name in series if not name.endswith("_z")]
    assert len(raw) == 13
    for name in raw:
        assert f"{name}_z" in series

    engine = OptionsFeatureEngine(settings=Settings())
    definitions = engine.registry.list_definitions(category="options")
    assert len(definitions) == 13 * 2
    assert all(d.version == "v1" for d in definitions)
    order = engine.registry.dependency_order()
    assert order.index("options_atm_iv") < order.index("options_iv_rank")
    assert order.index("options_call_writing_score") < order.index(
        "options_dealer_positioning"
    )
