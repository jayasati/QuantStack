from datetime import UTC, datetime, timedelta
from statistics import fmean, pstdev

import pytest

from app.core.config import Settings
from app.features.sector import (
    SECTORS_SYMBOL,
    SectorFeatureEngine,
    compute_sector_features,
)
from app.features.snapshots import Snapshot

BASE_TS = datetime(2026, 7, 7, 9, 15, tzinfo=UTC)


def val(x: float | None) -> float:
    assert x is not None
    return x


def make_snapshot(i: int, sectors: dict[str, dict] | None = None,
                  rotation_intensity: float = 0.8) -> Snapshot:
    """sectors: {name: {rs, momentum, capital, heat}}"""
    if sectors is None:
        sectors = {
            "Banking": {"rs": 0.5, "momentum": 0.3, "capital": 0.4, "heat": 62.0},
            "IT": {"rs": -0.4, "momentum": -0.2, "capital": -0.3, "heat": 44.0},
            "Pharma": {"rs": 0.1, "momentum": 0.05, "capital": 0.05, "heat": 52.0},
        }
    values = {name: cfg["heat"] for name, cfg in sectors.items()}
    values[SECTORS_SYMBOL] = max(cfg["heat"] for cfg in sectors.values())
    metadata: dict[str, dict] = {
        name: {
            "relative_strength": cfg["rs"],
            "relative_momentum": cfg["momentum"],
            "capital_rotation": cfg["capital"],
        }
        for name, cfg in sectors.items()
    }
    metadata[SECTORS_SYMBOL] = {"rotation_intensity": rotation_intensity}
    return Snapshot(ts=BASE_TS + timedelta(minutes=i), values=values, metadata=metadata)


def test_per_sector_passthroughs() -> None:
    snapshots = [make_snapshot(i) for i in range(5)]
    per_sector, _ = compute_sector_features(snapshots, windows=(5,))
    banking = per_sector["Banking"]
    assert banking["sector_relative_strength"][2] == pytest.approx(0.5)
    assert banking["sector_momentum"][2] == pytest.approx(0.3)
    assert banking["sector_capital_rotation"][2] == pytest.approx(0.4)
    assert banking["sector_heat_score"][2] == pytest.approx(62.0)


def test_leadership_is_cross_sectional_zscore() -> None:
    snapshots = [make_snapshot(i) for i in range(3)]
    per_sector, _ = compute_sector_features(snapshots, windows=(5,))
    heats = [62.0, 44.0, 52.0]
    expected = (62.0 - fmean(heats)) / pstdev(heats)
    assert per_sector["Banking"]["sector_leadership"][1] == pytest.approx(expected)


def test_winning_sector_rank_orders_by_heat() -> None:
    snapshots = [make_snapshot(i) for i in range(3)]
    per_sector, _ = compute_sector_features(snapshots, windows=(5,))
    assert per_sector["Banking"]["sector_rank"][1] == 1.0
    assert per_sector["Pharma"]["sector_rank"][1] == 2.0
    assert per_sector["IT"]["sector_rank"][1] == 3.0


def test_market_wide_rotation_index_and_participation() -> None:
    snapshots = [make_snapshot(i, rotation_intensity=1.25) for i in range(3)]
    _, market = compute_sector_features(snapshots, windows=(5,))
    assert market["sector_rotation_index"][1] == pytest.approx(1.25)
    # 2 of 3 sectors have positive relative strength.
    assert market["sector_participation_pct"][1] == pytest.approx(2 / 3 * 100)


def test_sector_correlation_vs_cross_sector_mean() -> None:
    # Banking's RS tracks the mean perfectly; IT moves against it.
    snapshots = []
    for i in range(15):
        drift = 0.1 * i
        snapshots.append(make_snapshot(i, sectors={
            "Banking": {"rs": 1.0 + drift, "momentum": 0.1, "capital": 0.1, "heat": 60.0},
            "IT": {"rs": -1.0 - drift, "momentum": -0.1, "capital": -0.1, "heat": 40.0},
            "Pharma": {"rs": 0.5 + drift, "momentum": 0.0, "capital": 0.0, "heat": 50.0},
        }))
    per_sector, _ = compute_sector_features(snapshots, windows=(10,))
    assert val(per_sector["Banking"]["sector_correlation_10"][12]) > 0.99
    assert val(per_sector["IT"]["sector_correlation_10"][12]) < -0.99
    assert per_sector["Banking"]["sector_correlation_10"][5] is None  # cold start
    # Perfectly collinear windows must clamp float overshoot at exactly +/-1.
    for sector in ("Banking", "IT", "Pharma"):
        observed = [v for v in per_sector[sector]["sector_correlation_10"] if v is not None]
        assert all(-1.0 <= v <= 1.0 for v in observed)


def test_missing_sector_data_stays_none() -> None:
    empty = Snapshot(ts=BASE_TS, values={}, metadata={})
    snapshots = [empty, make_snapshot(1)]
    per_sector, market = compute_sector_features(snapshots, windows=(5,))
    assert per_sector["Banking"]["sector_relative_strength"][0] is None
    assert per_sector["Banking"]["sector_rank"][0] is None
    assert market["sector_participation_pct"][0] is None


def test_every_feature_has_z_companion_and_registration() -> None:
    snapshots = [make_snapshot(i) for i in range(20)]
    per_sector, market = compute_sector_features(
        snapshots, windows=(5,), normalization_window=10
    )
    for series in [per_sector["Banking"], market]:
        raw = [name for name in series if not name.endswith("_z")]
        for name in raw:
            assert f"{name}_z" in series

    engine = SectorFeatureEngine(settings=Settings(feature_windows=[5, 10]))
    definitions = engine.registry.list_definitions(category="sector")
    # 8 base + 1 correlation x 2 windows = 10 raw, doubled by _z.
    assert len(definitions) == 10 * 2
    assert all(d.version == "v1" for d in definitions)
    order = engine.registry.dependency_order()
    assert order.index("sector_heat_score") < order.index("sector_leadership")
    assert order.index("sector_relative_strength") < order.index("sector_correlation_5")
