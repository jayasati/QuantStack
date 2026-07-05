"""Tests for the market breadth collector (Prompt 2.5)."""

import pytest

from app.collectors.base import CollectionError
from app.collectors.domains.breadth import BreadthSource, MarketBreadthCollector
from app.collectors.schema import CollectorOutput, Direction


def _row(
    symbol: str,
    last: float,
    prev_close: float,
    ema20: float,
    ema50: float,
    ema100: float,
    ema200: float,
    high_252: float,
    low_252: float,
) -> dict:
    return {
        "symbol": symbol,
        "last": last,
        "prev_close": prev_close,
        "ema20": ema20,
        "ema50": ema50,
        "ema100": ema100,
        "ema200": ema200,
        "high_252": high_252,
        "low_252": low_252,
        "volume": 1_000_000,
        "mcap": 1_000_000_000.0,
    }


class FakeBreadthSource(BreadthSource):
    """Deterministic 8-stock universe: 6 advancers (above every EMA, two at a
    fresh 52-week high) and 2 decliners (below every EMA)."""

    async def fetch_universe(self) -> list[dict]:
        return [
            _row("ADV1", 110.0, 100.0, 105.0, 103.0, 101.0, 98.0, 110.0, 80.0),  # new high
            _row("ADV2", 220.0, 200.0, 210.0, 206.0, 202.0, 196.0, 220.0, 160.0),  # new high
            _row("ADV3", 55.0, 50.0, 52.5, 51.5, 50.5, 49.0, 60.0, 40.0),
            _row("ADV4", 330.0, 300.0, 315.0, 309.0, 303.0, 294.0, 360.0, 240.0),
            _row("ADV5", 88.0, 80.0, 84.0, 82.4, 80.8, 78.4, 96.0, 64.0),
            _row("ADV6", 440.0, 400.0, 420.0, 412.0, 404.0, 392.0, 480.0, 320.0),
            _row("DEC1", 90.0, 100.0, 95.0, 97.0, 99.0, 102.0, 130.0, 70.0),
            _row("DEC2", 180.0, 200.0, 190.0, 194.0, 198.0, 204.0, 260.0, 140.0),
        ]


def _metric(records: list[CollectorOutput], name: str) -> CollectorOutput:
    matches = [r for r in records if r.metadata.get("metric") == name]
    assert len(matches) == 1, f"expected exactly one '{name}' record, got {len(matches)}"
    return matches[0]


async def test_advance_decline_ratio() -> None:
    collector = MarketBreadthCollector(breadth_source=FakeBreadthSource())
    records = await collector.collect()

    assert _metric(records, "advances").normalized_value == 6.0
    assert _metric(records, "declines").normalized_value == 2.0
    ad_ratio = _metric(records, "ad_ratio")
    assert ad_ratio.normalized_value == pytest.approx(3.0)
    assert ad_ratio.direction is Direction.BULLISH
    assert _metric(records, "ad_line_delta").normalized_value == pytest.approx(4.0)


async def test_pct_above_200_ema() -> None:
    collector = MarketBreadthCollector(breadth_source=FakeBreadthSource())
    records = await collector.collect()

    pct = _metric(records, "pct_above_ema200")
    assert pct.normalized_value == pytest.approx(75.0)
    assert pct.direction is Direction.BULLISH


async def test_new_highs_and_lows() -> None:
    collector = MarketBreadthCollector(breadth_source=FakeBreadthSource())
    records = await collector.collect()

    assert _metric(records, "new_highs_52w").normalized_value == 2.0
    assert _metric(records, "new_lows_52w").normalized_value == 0.0


async def test_composite_score_is_bullish() -> None:
    collector = MarketBreadthCollector(breadth_source=FakeBreadthSource())
    records = await collector.collect()

    summary = _metric(records, "breadth_score")
    assert summary.instrument == "MARKET"
    assert summary.normalized_value is not None and summary.normalized_value > 60.0
    assert summary.direction is Direction.BULLISH
    # All four components agree on the bullish side -> full confidence.
    assert summary.confidence == pytest.approx(1.0)
    components = summary.metadata["components"]
    assert set(components) == {"advancer_pct", "ema_breadth", "highs_lows", "momentum"}
    assert summary.metadata["breadth_momentum"] == pytest.approx(0.5)
    assert summary.metadata["universe_size"] == 8


async def test_every_record_targets_market_instrument() -> None:
    collector = MarketBreadthCollector(breadth_source=FakeBreadthSource())
    records = await collector.collect()

    assert records, "collector emitted no records"
    assert all(record.instrument == "MARKET" for record in records)


async def test_unconfigured_source_raises() -> None:
    from app.collectors.domains.breadth import UnconfiguredBreadthSource

    collector = MarketBreadthCollector(breadth_source=UnconfiguredBreadthSource())
    with pytest.raises(CollectionError, match="breadth source not configured"):
        await collector.collect()


def test_default_source_is_nse() -> None:
    from app.collectors.sources.nse_breadth import NseBreadthSource

    assert isinstance(MarketBreadthCollector()._breadth_source, NseBreadthSource)
