"""Offline tests for the NSE institutional flow source."""

import pytest

from app.collectors.base import CollectionError
from app.collectors.sources.nse_flows import NseFlowSource, parse_deals, parse_fiidii

FIIDII_ROWS = [
    {"buyValue": "18676.35", "category": "DII", "date": "03-Jul-2026",
     "netValue": "-1953.89", "sellValue": "20630.24"},
    {"buyValue": "13337.33", "category": "FII/FPI", "date": "03-Jul-2026",
     "netValue": "1355.33", "sellValue": "11982"},
]

DEALS_PAYLOAD = {
    "as_on_date": "06-Jul-2026",
    "BLOCK_DEALS_DATA": [
        {"buySell": "BUY", "symbol": "GENUSPOWER", "qty": "2241379",
         "watp": "290", "date": "30-Jun-2026"},
        {"buySell": "SELL", "symbol": "RELIANCE", "qty": "1000000",
         "watp": "2900", "date": "06-Jul-2026"},
    ],
    "BULK_DEALS_DATA": [
        {"buySell": "BUY", "symbol": "RAMCOSYS", "qty": "236397",
         "watp": "799.51", "date": "06-Jul-2026"},
        {"buySell": "BUY", "symbol": "BADQTY", "qty": None,
         "watp": "10", "date": "06-Jul-2026"},
    ],
}


def test_parse_fiidii_categories_and_gross() -> None:
    parsed = parse_fiidii(FIIDII_ROWS)
    assert parsed["fii"]["net"] == pytest.approx(1355.33)
    assert parsed["fii"]["gross"] == pytest.approx(13337.33 + 11982)
    assert parsed["dii"]["net"] == pytest.approx(-1953.89)


def test_parse_deals_latest_date_only_and_value_math() -> None:
    block = parse_deals(DEALS_PAYLOAD, "BLOCK_DEALS_DATA")
    # 30-Jun row dropped; only the 06-Jul deal kept
    assert len(block) == 1
    assert block[0]["symbol"] == "RELIANCE"
    assert block[0]["side"] == "sell"
    assert block[0]["value_cr"] == pytest.approx(1_000_000 * 2900 / 1e7)

    bulk = parse_deals(DEALS_PAYLOAD, "BULK_DEALS_DATA")
    assert len(bulk) == 1  # bad-qty row dropped
    assert bulk[0]["value_cr"] == pytest.approx(236397 * 799.51 / 1e7, rel=1e-4)


class FakeSession:
    def __init__(self, responses: dict[str, dict | list]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def get_json(self, path: str):
        self.calls.append(path)
        for prefix, response in self._responses.items():
            if path.startswith(prefix):
                return response
        return {}

    async def close(self) -> None:
        pass


async def test_fetch_flows_builds_full_contract() -> None:
    session = FakeSession(
        {
            "/api/fiidiiTradeReact": FIIDII_ROWS,
            "/api/snapshot-capital-market-largedeal": DEALS_PAYLOAD,
            "/api/corporate-sast-reg29": {"data": [{"acquirerDate": "x"}] * 14},
            "/api/corporates-pit": {"data": []},
        }
    )
    source = NseFlowSource(session=session)
    flows = await source.fetch_flows()

    assert flows["fii_cash_cr"] == pytest.approx(1355.33)
    assert flows["dii_cash_cr"] == pytest.approx(-1953.89)
    # No stored history in tests -> bootstrap scale = gross/4
    assert flows["fii_20d_avg_cr"] == pytest.approx((13337.33 + 11982) / 4)
    assert flows["avg_source"] == "same_day_gross/4"
    assert flows["insider_data_available"] is False
    assert flows["promoter_buys_cr"] == 0.0
    assert len(flows["block_deals"]) == 1
    assert flows["sast_filings"] == 2  # 14 filings over a week -> daily average

    # Cached second call: no extra HTTP
    calls = len(session.calls)
    await source.fetch_flows()
    assert len(session.calls) == calls


async def test_fetch_flows_fails_without_categories() -> None:
    session = FakeSession({"/api/fiidiiTradeReact": [{"category": "OTHER"}]})
    source = NseFlowSource(session=session)
    with pytest.raises(CollectionError, match="missing categories"):
        await source.fetch_flows()


async def test_collector_end_to_end_with_nse_shapes() -> None:
    from app.collectors.domains.flows import InstitutionalFlowCollector

    session = FakeSession(
        {
            "/api/fiidiiTradeReact": FIIDII_ROWS,
            "/api/snapshot-capital-market-largedeal": DEALS_PAYLOAD,
            "/api/corporate-sast-reg29": {"data": []},
            "/api/corporates-pit": {"data": []},
        }
    )
    collector = InstitutionalFlowCollector(flow_source=NseFlowSource(session=session))
    records = await collector.collect()
    metrics = {r.metadata["metric"] for r in records}
    assert "fii_flow" in metrics
    assert "participation_index" in metrics
    fii = next(r for r in records if r.metadata["metric"] == "fii_flow")
    assert fii.normalized_value is not None and fii.normalized_value > 0


async def test_records_carry_as_of_and_data_age() -> None:
    from app.collectors.domains.flows import InstitutionalFlowCollector

    session = FakeSession(
        {
            "/api/fiidiiTradeReact": FIIDII_ROWS,  # dated 03-Jul-2026
            "/api/snapshot-capital-market-largedeal": DEALS_PAYLOAD,
            "/api/corporate-sast-reg29": {"data": []},
            "/api/corporates-pit": {"data": []},
        }
    )
    collector = InstitutionalFlowCollector(flow_source=NseFlowSource(session=session))
    records = await collector.collect()
    assert records
    for record in records:
        assert record.metadata["as_of"] == "03-Jul-2026"
        assert record.metadata["data_age_days"] >= 0
