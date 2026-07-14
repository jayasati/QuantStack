"""Tests for the Feature Store's Parquet archival sink (Volume 3, Chapter 4:
offline store is "Parquet, PostgreSQL" -- this is the Parquet half).
Postgres remains the sole read path; these tests only exercise the
write-only Parquet export, using session_factory=None/cache=None so the
Postgres/Redis writes honestly no-op (same degradation pattern used
elsewhere in this test suite) and only the Parquet path is under test.
"""

from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

import app.features.store as store_module
from app.features.schema import FeatureValue
from app.features.store import FeatureStore

BASE_TS = datetime(2026, 7, 14, tzinfo=UTC)


def make_values(symbol: str = "NIFTY", timeframe: str = "D", n: int = 3) -> list[FeatureValue]:
    return [
        FeatureValue(
            feature_name=f"price_momentum_{i}",
            feature_version="v1",
            symbol=symbol,
            timeframe=timeframe,
            ts=BASE_TS,
            value=float(i),
            window=i,
        )
        for i in range(n)
    ]


@pytest.fixture
def parquet_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "feature_store_parquet"
    monkeypatch.setattr(store_module, "PARQUET_ROOT", root)
    return root


async def test_write_creates_a_partitioned_parquet_file(parquet_root: Path) -> None:
    store = FeatureStore(session_factory=None, cache=None)
    values = make_values(symbol="NIFTY", timeframe="D", n=3)
    await store.write(values)

    partition_dir = parquet_root / "symbol=NIFTY" / "timeframe=D"
    files = list(partition_dir.glob("*.parquet"))
    assert len(files) == 1


async def test_written_rows_roundtrip_correctly(parquet_root: Path) -> None:
    """symbol/timeframe are deliberately NOT stored as columns inside the
    file -- they're Hive partition-path columns instead, reconstructed by a
    partition-aware reader. Reading via pq.read_table(root, partitioning=
    "hive") is the correct/intended consumption pattern, and also proves
    the file's own columns don't conflict with the inferred partition
    columns (they did, before symbol/timeframe were dropped from the
    per-row payload -- see the fix this test guards against)."""
    store = FeatureStore(session_factory=None, cache=None)
    values = make_values(symbol="NIFTY", timeframe="D", n=3)
    await store.write(values)

    table = pq.read_table(str(parquet_root), partitioning="hive")
    rows = table.to_pylist()

    assert len(rows) == 3
    by_name = {row["feature_name"]: row for row in rows}
    assert by_name["price_momentum_1"]["value"] == 1.0
    assert by_name["price_momentum_1"]["symbol"] == "NIFTY"
    assert by_name["price_momentum_1"]["timeframe"] == "D"
    assert by_name["price_momentum_1"]["window"] == 1
    assert by_name["price_momentum_1"]["feature_version"] == "v1"


async def test_write_partitions_separately_by_symbol_and_timeframe(parquet_root: Path) -> None:
    store = FeatureStore(session_factory=None, cache=None)
    values = make_values(symbol="NIFTY", timeframe="D", n=1) + make_values(
        symbol="BANKNIFTY", timeframe="chain", n=1
    )
    await store.write(values)

    assert list((parquet_root / "symbol=NIFTY" / "timeframe=D").glob("*.parquet"))
    assert list((parquet_root / "symbol=BANKNIFTY" / "timeframe=chain").glob("*.parquet"))


async def test_empty_values_writes_nothing(parquet_root: Path) -> None:
    store = FeatureStore(session_factory=None, cache=None)
    await store.write([])
    assert not parquet_root.exists()


async def test_write_failure_is_swallowed_not_raised(
    parquet_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Parquet write failure (e.g. an unwritable location) must never
    propagate into write()'s caller -- same tolerance as _write_offline/
    _write_online already have for their own backends being unavailable."""

    def boom(_values: list[FeatureValue]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store_module, "_write_parquet_sync", boom)

    store = FeatureStore(session_factory=None, cache=None)
    result = await store.write(make_values())  # must not raise
    assert result == {"offline_rows": 0, "online_entries": 0}
