from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.features.macro import MacroFeatureEngine, compute_macro_factor_features
from app.features.snapshots import Snapshot

BASE_TS = datetime(2026, 7, 7, 9, 15, tzinfo=UTC)


def make_snapshot(i: int, **factors: dict) -> Snapshot:
    """factors: {name: {"score": ..., "value": ..., "change_1d_pct": ..., "zscore_20d": ...}}"""
    values = {name: f["score"] for name, f in factors.items() if "score" in f}
    metadata = {
        name: {k: v for k, v in f.items() if k != "score"} for name, f in factors.items()
    }
    return Snapshot(ts=BASE_TS + timedelta(minutes=i), values=values, metadata=metadata)


def usdinr(score=-0.3, value=95.5, change=0.2, zscore=-0.5) -> dict:
    return {"score": score, "value": value, "change_1d_pct": change, "zscore_20d": zscore}


def test_passthrough_score_and_sidecar_fields() -> None:
    snapshots = [make_snapshot(i, USDINR=usdinr()) for i in range(3)]
    series = compute_macro_factor_features(snapshots, "USDINR")
    assert series["macro_score"][1] == pytest.approx(-0.3)
    assert series["macro_value"][1] == pytest.approx(95.5)
    assert series["macro_return_1d_pct"][1] == pytest.approx(0.2)
    assert series["macro_zscore_20d"][1] == pytest.approx(-0.5)


def test_missing_factor_in_a_snapshot_stays_none() -> None:
    snapshots = [
        Snapshot(ts=BASE_TS, values={}, metadata={}),
        make_snapshot(1, USDINR=usdinr()),
    ]
    series = compute_macro_factor_features(snapshots, "USDINR")
    assert series["macro_score"][0] is None
    assert series["macro_score"][1] is not None


def test_every_feature_has_z_companion_and_registration() -> None:
    snapshots = [make_snapshot(i, USDINR=usdinr(score=-0.3 + i * 0.01)) for i in range(20)]
    series = compute_macro_factor_features(snapshots, "USDINR", normalization_window=10)
    raw = [name for name in series if not name.endswith("_z")]
    for name in raw:
        assert f"{name}_z" in series

    engine = MacroFeatureEngine(settings=Settings())
    definitions = engine.registry.list_definitions(category="macro")
    # 4 raw features, doubled by _z.
    assert len(definitions) == 4 * 2
    assert all(d.version == "v1" for d in definitions)


async def test_run_discovers_every_factor_present_in_the_data(monkeypatch) -> None:
    snapshots = [
        make_snapshot(i, USDINR=usdinr(), CRUDE=usdinr(value=75.0, score=0.1))
        for i in range(5)
    ]

    async def fake_load(self):
        return [
            (s.ts, name, s.values.get(name), s.metadata.get(name) or {})
            for s in snapshots
            for name in s.values
        ]

    monkeypatch.setattr(MacroFeatureEngine, "_load_macro_observations", fake_load)
    engine = MacroFeatureEngine(settings=Settings(feature_windows=[5, 10]))
    result = await engine.run()
    assert result["factors"] == 2
