"""NSE option-chain mapping tests (offline, fixture-based)."""

from datetime import datetime

import pytest

from app.collectors.base import CollectionError
from app.collectors.sources.nse_options import NseOptionChainSource, map_nse_chain
from app.core.container import container
from app.market.broker import BrokerInterface, Candle, Quote

NSE_PAYLOAD = {
    "records": {
        "expiryDates": ["10-Jul-2026", "17-Jul-2026"],
        "underlyingValue": 24270.85,
        "timestamp": "05-Jul-2026 15:30:00",
        "data": [
            {
                "strikePrice": 24200,
                "expiryDate": "10-Jul-2026",
                "CE": {
                    "openInterest": 1000,
                    "changeinOpenInterest": 150,
                    "impliedVolatility": 12.5,
                    "totalTradedVolume": 5000,
                    "lastPrice": 180.5,
                },
                "PE": {
                    "openInterest": 2200,
                    "changeinOpenInterest": -50,
                    "impliedVolatility": 14.1,
                    "totalTradedVolume": 7000,
                    "lastPrice": 95.0,
                },
            },
            {
                "strikePrice": 24300,
                "expiryDate": "10-Jul-2026",
                "CE": {
                    "openInterest": 3000,
                    "changeinOpenInterest": 900,
                    "impliedVolatility": 0,  # NSE uses 0 for missing IV
                    "totalTradedVolume": 12000,
                    "lastPrice": 120.0,
                },
                # No PE side at this strike
            },
            {
                # Different expiry — must be excluded
                "strikePrice": 24200,
                "expiryDate": "17-Jul-2026",
                "CE": {"openInterest": 99999},
                "PE": {"openInterest": 99999},
            },
        ],
    }
}


def test_maps_nearest_expiry_only() -> None:
    chain = map_nse_chain(NSE_PAYLOAD)
    assert chain["expiry"] == "10-Jul-2026"
    assert len(chain["strikes"]) == 2
    assert chain["spot"] == 24270.85


def test_leg_mapping_and_missing_iv() -> None:
    chain = map_nse_chain(NSE_PAYLOAD)
    first = chain["strikes"][0]
    assert first["call"]["oi"] == 1000
    assert first["call"]["oi_change"] == 150
    assert first["call"]["iv"] == 12.5
    assert first["put"]["oi"] == 2200

    second = chain["strikes"][1]
    assert second["call"]["iv"] is None  # 0 -> missing
    assert second["put"]["oi"] == 0  # absent leg -> zeroed


def test_empty_payload_raises() -> None:
    with pytest.raises(CollectionError):
        map_nse_chain({"records": {}})


async def test_collector_derives_features_from_nse_shape() -> None:
    """End-to-end: NSE payload -> mapped chain -> derived features."""
    from app.collectors.domains.options import OptionsChainSource, OptionsIntelligenceCollector

    class FixtureSource(OptionsChainSource):
        async def fetch_chain(self, instrument: str) -> dict:
            chain = map_nse_chain(NSE_PAYLOAD)
            chain["prev_spot"] = 24150.0
            return chain

    collector = OptionsIntelligenceCollector(source=FixtureSource())
    collector.symbols = ["NIFTY"]
    records = await collector.collect()
    features = {r.metadata["feature"]: r for r in records}

    assert "pcr" in features
    assert features["pcr"].normalized_value == pytest.approx(2200 / 4000)
    assert "max_pain" in features
    assert "buildup" in features  # prev_spot present -> classification emitted
    assert features["buildup"].raw_value == "long_buildup"  # price up, OI up


class FakeGreeksBroker(BrokerInterface):
    """Returns full Greeks (delta/gamma/theta/vega) for one known strike."""

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def is_connected(self) -> bool:
        return True

    async def get_quote(self, symbol: str, exchange: str = "NSE") -> Quote:
        raise NotImplementedError

    async def get_historical(
        self, symbol: str, interval: str, start: datetime, end: datetime,
        exchange: str = "NSE",
    ) -> list[Candle]:
        raise NotImplementedError

    async def get_option_greeks(
        self, name: str, expiry: str
    ) -> dict[tuple[float, str], dict[str, float]]:
        return {
            (100.0, "CE"): {"delta": 0.5, "gamma": 0.05, "theta": -0.3, "vega": 0.12},
            (100.0, "PE"): {"delta": -0.5, "gamma": 0.05, "theta": -0.28, "vega": 0.12},
        }


async def test_enrich_with_greeks_passes_theta_and_vega_through() -> None:
    """Chapter 9 gap fill: theta/vega used to be dropped during enrichment
    (only delta/gamma survived), even though the broker already returns
    them — Options Feature Engine's ATM Theta/Vega features need this."""
    container.register(BrokerInterface, FakeGreeksBroker)
    source = NseOptionChainSource()
    chain = {"strikes": [{"strike": 100.0, "call": {}, "put": {}}]}

    await source._enrich_with_greeks("NIFTY", "07-Jul-2026", chain)

    call = chain["strikes"][0]["call"]
    put = chain["strikes"][0]["put"]
    assert call == {"delta": 0.5, "gamma": 0.05, "theta": -0.3, "vega": 0.12}
    assert put == {"delta": -0.5, "gamma": 0.05, "theta": -0.28, "vega": 0.12}
    assert chain["greeks_enriched_legs"] == 2
