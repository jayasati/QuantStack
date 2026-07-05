"""Tests for the sector rotation collector (Prompt 2.6)."""

import pytest

from app.collectors.base import CollectionError
from app.collectors.domains.sector import (
    NSE_SECTORS,
    SectorRotationCollector,
    SectorSource,
)
from app.collectors.schema import CollectorCategory, Direction

LEADER = "IT"
LAGGARD = "Realty"


class FakeSectorSource(SectorSource):
    """Deterministic fixture: IT clearly leads, Realty clearly lags."""

    async def fetch_sectors(self) -> dict:
        neutral = {"return_1d": 0.2, "return_5d": 0.2, "return_20d": 0.2, "volume_ratio": 1.0}
        sectors = {name: dict(neutral) for name in NSE_SECTORS}
        sectors[LEADER] = {
            "return_1d": 2.0,
            "return_5d": 4.0,
            "return_20d": 1.0,
            "volume_ratio": 1.5,
        }
        sectors[LAGGARD] = {
            "return_1d": -2.5,
            "return_5d": -4.0,
            "return_20d": -1.0,
            "volume_ratio": 0.6,
        }
        return {
            "benchmark": {
                "return_1d": 0.0,
                "return_5d": 0.0,
                "return_20d": 0.0,
                "volume_ratio": 1.0,
            },
            "sectors": sectors,
        }


def make_collector() -> SectorRotationCollector:
    return SectorRotationCollector(sector_source=FakeSectorSource())


def test_collector_configuration() -> None:
    collector = make_collector()
    assert collector.category is CollectorCategory.SECTOR
    assert collector.interval_seconds == 60
    assert collector.priority == 15


async def test_emits_one_record_per_sector_plus_summary() -> None:
    records = await make_collector().collect()
    assert len(records) == len(NSE_SECTORS) + 1 == 13
    instruments = {record.instrument for record in records}
    assert set(NSE_SECTORS) <= instruments
    assert "SECTORS" in instruments


async def test_leader_and_laggard_identified_in_summary() -> None:
    records = await make_collector().collect()
    summary = next(record for record in records if record.instrument == "SECTORS")
    assert summary.metadata["leader"] == LEADER
    assert summary.metadata["laggard"] == LAGGARD

    heatmap = summary.metadata["heatmap"]
    assert set(heatmap) == set(NSE_SECTORS)
    assert max(heatmap, key=lambda name: heatmap[name]) == LEADER
    assert min(heatmap, key=lambda name: heatmap[name]) == LAGGARD


async def test_leader_relative_strength_is_positive_and_bullish() -> None:
    records = await make_collector().collect()
    leader = next(record for record in records if record.instrument == LEADER)
    assert leader.metadata["relative_strength"] > 0
    assert leader.metadata["relative_momentum"] > 0
    assert leader.direction is Direction.BULLISH

    laggard = next(record for record in records if record.instrument == LAGGARD)
    assert laggard.metadata["relative_strength"] < 0
    assert laggard.direction is Direction.BEARISH


async def test_unconfigured_default_source_raises() -> None:
    collector = SectorRotationCollector()
    with pytest.raises(CollectionError, match="sector source not configured"):
        await collector.collect()


async def test_incomplete_payload_is_rejected_not_fabricated() -> None:
    class MissingSectorSource(SectorSource):
        async def fetch_sectors(self) -> dict:
            payload = await FakeSectorSource().fetch_sectors()
            del payload["sectors"]["Defence"]
            return payload

    collector = SectorRotationCollector(sector_source=MissingSectorSource())
    with pytest.raises(CollectionError, match="Defence"):
        await collector.collect()
