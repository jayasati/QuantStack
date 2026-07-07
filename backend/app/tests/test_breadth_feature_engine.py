from datetime import UTC, datetime, timedelta
from statistics import fmean

import pytest

from app.core.config import Settings
from app.features.breadth import (
    BreadthFeatureEngine,
    compute_breadth_features,
)
from app.features.normalize import rolling_slope
from app.features.snapshots import Snapshot

BASE_TS = datetime(2026, 7, 7, 9, 15, tzinfo=UTC)


def val(x: float | None) -> float:
    assert x is not None
    return x


def make_snapshot(i: int, *, advances: float = 1200, declines: float = 700,
                  unchanged: float = 100, new_highs: float = 40, new_lows: float = 10,
                  divergence: float = 0.15, score: float = 62.0,
                  ad_line: float = 500.0,
                  pct_above: tuple[float, float, float, float] = (65, 60, 58, 55),
                  ) -> Snapshot:
    e20, e50, e100, e200 = pct_above
    return Snapshot(
        ts=BASE_TS + timedelta(minutes=i),
        values={
            "advances": advances,
            "declines": declines,
            "unchanged": unchanged,
            "new_highs_52w": new_highs,
            "new_lows_52w": new_lows,
            "breadth_divergence": divergence,
            "breadth_score": score,
            "pct_above_ema20": e20,
            "pct_above_ema50": e50,
            "pct_above_ema100": e100,
            "pct_above_ema200": e200,
        },
        metadata={"ad_line_delta": {"ad_line": ad_line}},
    )


def test_strength_participation_and_trend() -> None:
    snapshots = [make_snapshot(i) for i in range(5)]
    series = compute_breadth_features(snapshots, windows=(5,))
    assert series["breadth_strength"][2] == pytest.approx((1200 - 700) / 1900)
    assert series["breadth_participation_pct"][2] == pytest.approx(1200 / 2000 * 100)
    assert series["breadth_trend_pct"][2] == pytest.approx(fmean([65, 60, 58, 55]))


def test_divergence_and_health_passthrough() -> None:
    snapshots = [make_snapshot(i, divergence=-0.4, score=38.5) for i in range(3)]
    series = compute_breadth_features(snapshots, windows=(5,))
    assert series["breadth_divergence"][1] == pytest.approx(-0.4)
    assert series["breadth_health_score"][1] == pytest.approx(38.5)


def test_ad_momentum_positive_when_ad_line_rises() -> None:
    snapshots = [make_snapshot(i, ad_line=500.0 + 25.0 * i) for i in range(15)]
    series = compute_breadth_features(snapshots, windows=(10,))
    # AD line gains exactly 25 per snapshot -> slope 25.
    assert series["breadth_ad_momentum_10"][12] == pytest.approx(25.0)
    assert series["breadth_ad_momentum_10"][5] is None  # cold start


def test_breadth_momentum_tracks_strength_trend() -> None:
    snapshots = [
        make_snapshot(i, advances=800 + 40 * i, declines=1100 - 40 * i)
        for i in range(15)
    ]
    series = compute_breadth_features(snapshots, windows=(10,))
    assert val(series["breadth_momentum_10"][14]) > 0
    expected = rolling_slope(series["breadth_strength"], 10)[14]
    assert series["breadth_momentum_10"][14] == pytest.approx(val(expected))


def test_new_high_low_momentum_signs() -> None:
    snapshots = [
        make_snapshot(i, new_highs=10.0 + 3 * i, new_lows=50.0 - 2 * i)
        for i in range(12)
    ]
    series = compute_breadth_features(snapshots, windows=(10,))
    assert series["breadth_new_high_momentum_10"][11] == pytest.approx(3.0)
    assert series["breadth_new_low_momentum_10"][11] == pytest.approx(-2.0)


def test_missing_metrics_stay_none() -> None:
    empty = Snapshot(ts=BASE_TS, values={}, metadata={})
    series = compute_breadth_features([empty, make_snapshot(1)], windows=(5,))
    assert series["breadth_strength"][0] is None
    assert series["breadth_trend_pct"][0] is None
    assert series["breadth_strength"][1] is not None


def test_every_feature_has_z_companion_and_registration() -> None:
    snapshots = [make_snapshot(i) for i in range(20)]
    series = compute_breadth_features(snapshots, windows=(5,), normalization_window=10)
    raw = [name for name in series if not name.endswith("_z")]
    for name in raw:
        assert f"{name}_z" in series

    engine = BreadthFeatureEngine(settings=Settings(feature_windows=[5, 10]))
    definitions = engine.registry.list_definitions(category="breadth")
    # 5 per-snapshot + 4 windowed x 2 windows = 13 raw, doubled by _z.
    assert len(definitions) == 13 * 2
    assert all(d.version == "v1" for d in definitions)
    order = engine.registry.dependency_order()
    assert order.index("breadth_strength") < order.index("breadth_momentum_5")
