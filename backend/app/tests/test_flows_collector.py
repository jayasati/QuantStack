import pytest

from app.collectors.base import CollectionError
from app.collectors.domains.flows import FlowSource, InstitutionalFlowCollector
from app.collectors.schema import CollectorOutput, Direction


class FakeFlowSource(FlowSource):
    """Strong FII buying, DII selling, one large block deal."""

    def __init__(self, overrides: dict | None = None) -> None:
        self.flows = {
            "fii_cash_cr": 1800.0,  # well above the 20-day average -> strong buying
            "dii_cash_cr": -1200.0,  # net selling
            "fii_20d_avg_cr": 2000.0,
            "dii_20d_avg_cr": 1500.0,
            "etf_flows_cr": 500.0,
            "block_deals": [{"symbol": "RELIANCE", "side": "buy", "value_cr": 750.0}],
            "bulk_deals": [],
            "promoter_buys_cr": 120.0,
            "promoter_sells_cr": 40.0,
            "sast_filings": 3,
            "insider_net_cr": 60.0,
        }
        self.flows.update(overrides or {})

    async def fetch_flows(self) -> dict:
        return self.flows


def by_metric(records: list[CollectorOutput], metric: str) -> CollectorOutput:
    matches = [r for r in records if r.metadata.get("metric") == metric]
    assert len(matches) == 1, f"expected exactly one {metric!r} record, got {len(matches)}"
    return matches[0]


async def test_fii_score_positive_and_dii_score_negative() -> None:
    collector = InstitutionalFlowCollector(flow_source=FakeFlowSource())
    records = await collector.collect()

    fii = by_metric(records, "fii_flow")
    assert fii.normalized_value is not None and fii.normalized_value > 0
    assert fii.direction is Direction.BULLISH

    dii = by_metric(records, "dii_flow")
    assert dii.normalized_value is not None and dii.normalized_value < 0
    assert dii.direction is Direction.BEARISH


async def test_participation_index_above_50_with_components() -> None:
    collector = InstitutionalFlowCollector(flow_source=FakeFlowSource())
    records = await collector.collect()

    index = by_metric(records, "participation_index")
    assert index.instrument == "MARKET"
    assert index.raw_value > 50.0
    assert index.direction is Direction.BULLISH
    assert index.normalized_value is not None and 0.0 < index.normalized_value <= 1.0

    components = index.metadata["components"]
    assert components["fii_flow"] > 0
    assert components["dii_flow"] < 0
    assert set(index.metadata["weights"]) == set(components)


async def test_block_deal_record_carries_symbol_and_side() -> None:
    collector = InstitutionalFlowCollector(flow_source=FakeFlowSource())
    records = await collector.collect()

    deals = [r for r in records if r.metadata.get("metric") == "block_deal"]
    assert len(deals) == 1
    deal = deals[0]
    assert deal.instrument == "RELIANCE"
    assert deal.metadata["side"] == "buy"
    assert deal.metadata["value_cr"] == 750.0
    assert deal.direction is Direction.BULLISH
    assert deal.normalized_value == 1.0  # 750 cr saturates the 500 cr deal scale


async def test_extreme_flows_are_clamped() -> None:
    source = FakeFlowSource(
        overrides={
            "fii_cash_cr": 50_000.0,
            "dii_cash_cr": -50_000.0,
            "fii_20d_avg_cr": 1_000.0,
            "dii_20d_avg_cr": 1_000.0,
        }
    )
    collector = InstitutionalFlowCollector(flow_source=source)
    records = await collector.collect()

    assert by_metric(records, "fii_flow").normalized_value == 1.0
    assert by_metric(records, "dii_flow").normalized_value == -1.0
    for record in records:
        if record.normalized_value is not None:
            assert -1.0 <= record.normalized_value <= 1.0


async def test_unconfigured_default_source_raises() -> None:
    collector = InstitutionalFlowCollector()
    with pytest.raises(CollectionError, match="institutional flow source not configured"):
        await collector.collect()
