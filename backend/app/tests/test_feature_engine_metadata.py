"""Tests for feature-row metadata wiring (data foundation audit 2026-07-17).

BaseFeatureEngine._process_series attaches each batch's own quality score to
every value before writing (dataclasses.replace, since FeatureValue is
frozen) and build_values_at stamps collector_version at construction time
from the engine's own `engine_version`. These are pure-logic checks via a
synthetic engine with session_factory=None and a monkeypatched store.write
to capture what would have been persisted -- DB round-trip coverage of the
same fields (write -> latest/history) lives in test_db_persistence.py.
"""

from datetime import UTC, datetime, timedelta

from app.features.base import BaseFeatureEngine
from app.features.schema import Candle, FeatureDefinition, FeatureValue, Series


class _MetadataTestEngine(BaseFeatureEngine):
    name = "metadata_test_engine"
    category = "test"
    engine_version = "2.3.1"

    def _definitions(self) -> list[FeatureDefinition]:
        return [
            FeatureDefinition(
                feature_name="in_range_feature", category="test",
                description="test", expected_range=(0.0, 100.0),
            ),
            FeatureDefinition(
                feature_name="out_of_range_feature", category="test",
                description="test", expected_range=(0.0, 1.0),
            ),
        ]

    def _compute(self, candles, benchmark=None) -> dict[str, Series]:
        return {
            "in_range_feature": [50.0 for _ in candles],
            "out_of_range_feature": [999.0 for _ in candles],  # always outside its own range
        }


def _synthetic_candles(n: int = 5) -> list[Candle]:
    base = datetime(2026, 7, 1, tzinfo=UTC)
    return [
        Candle(ts=base + timedelta(days=i), open=100.0, high=101.0, low=99.0, close=100.5)
        for i in range(n)
    ]


async def _run_and_capture(monkeypatch) -> list[FeatureValue]:
    engine = _MetadataTestEngine(session_factory=None)

    async def fake_load_candles(symbol: str, timeframe: str, start=None, end=None) -> list[Candle]:
        return _synthetic_candles()

    monkeypatch.setattr(engine, "_load_candles", fake_load_candles)

    captured: list[FeatureValue] = []

    async def fake_write(values: list[FeatureValue]) -> dict:
        captured.extend(values)
        return {"offline_rows": len(values), "online_entries": 0}

    monkeypatch.setattr(engine.store, "write", fake_write)
    await engine.run("TESTSYM", "D")
    return captured


async def test_build_values_stamps_collector_version_from_the_engine(monkeypatch) -> None:
    captured = await _run_and_capture(monkeypatch)
    assert captured  # actually produced values, not the <2-candle skip path
    assert all(v.collector_version == "2.3.1" for v in captured)


async def test_process_series_attaches_the_batchs_quality_score_per_feature(
    monkeypatch,
) -> None:
    captured = await _run_and_capture(monkeypatch)

    in_range = [v for v in captured if v.feature_name == "in_range_feature"]
    out_of_range = [v for v in captured if v.feature_name == "out_of_range_feature"]
    assert in_range and all(v.feature_quality_score == 100.0 for v in in_range)
    assert out_of_range and all(v.feature_quality_score == 0.0 for v in out_of_range)
