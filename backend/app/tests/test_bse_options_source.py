"""BSE option-chain mapping tests (offline, fixture-based).

Fixture rows are trimmed copies of a real DerivOptionChain_IV/SensexDeri
response captured live on 2026-07-13 against api.bseindia.com.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.collectors.base import CollectionError
from app.collectors.sources.bse_options import (
    enrich_with_computed_greeks,
    map_bse_chain,
    pick_nearest_expiry,
)

BSE_CHAIN_PAYLOAD = {
    "Table": [
        {
            "C_Series_Code": "SENSEX2671677300CE",
            "C_Open_Interest": "12345",
            "C_Absolute_Change_OI": "500",
            "C_Last_Trd_Price": "520.30",
            "C_Vol_Traded": "88000",
            "C_IV": "16.204",
            "Strike_Price": "77,300.00",
            "Strike_Price1": "77300.00",
            "Open_Interest": "",  # untraded put at this strike -> missing
            "Absolute_Change_OI": "",
            "Last_Trd_Price": "",
            "Vol_Traded": "",
            "IV": "",
            "UlaValue": "77397.77",
            "End_TimeStamp": "16 Jul 2026",
            "p_Series_Code": "SENSEX2671677300PE",
        },
        {
            "C_Series_Code": "SENSEX2671677400CE",
            "C_Open_Interest": "38518",
            "C_Absolute_Change_OI": "14641",
            "C_Last_Trd_Price": "451.20",
            "C_Vol_Traded": "274342",
            "C_IV": "15.803093228015799298624700",
            "Strike_Price": "77,400.00",
            "Strike_Price1": "77400.00",
            "Open_Interest": "40554",
            "Absolute_Change_OI": "1582",
            "Last_Trd_Price": "490.10",
            "Vol_Traded": "147340",
            "IV": "17.984007310053901074198279",
            "UlaValue": "77397.77",
            "End_TimeStamp": "16 Jul 2026",
            "p_Series_Code": "SENSEX2671677400PE",
        },
    ]
}


def test_maps_spot_and_strikes() -> None:
    chain = map_bse_chain(BSE_CHAIN_PAYLOAD, expiry="16 Jul 2026")
    assert chain["spot"] == 77397.77
    assert chain["expiry"] == "16 Jul 2026"
    assert len(chain["strikes"]) == 2
    assert chain["strikes"][0]["strike"] == 77300.0
    assert chain["strikes"][1]["strike"] == 77400.0


def test_leg_mapping_and_missing_values() -> None:
    chain = map_bse_chain(BSE_CHAIN_PAYLOAD, expiry="16 Jul 2026")
    first = chain["strikes"][0]
    assert first["call"]["oi"] == 12345
    assert first["call"]["oi_change"] == 500
    assert first["call"]["iv"] == pytest.approx(16.204)
    assert first["call"]["ltp"] == pytest.approx(520.30)
    # Put side untraded at this strike -> empty strings read as missing
    assert first["put"]["oi"] == 0
    assert first["put"]["iv"] is None
    assert first["put"]["ltp"] is None

    second = chain["strikes"][1]
    assert second["put"]["oi"] == 40554
    assert second["put"]["iv"] == pytest.approx(17.984007310053901074198279)


def test_empty_payload_raises() -> None:
    with pytest.raises(CollectionError):
        map_bse_chain({"Table": []}, expiry="16 Jul 2026")


def test_missing_spot_raises() -> None:
    with pytest.raises(CollectionError):
        map_bse_chain({"Table": [{"Strike_Price1": "77300.00"}]}, expiry="16 Jul 2026")


async def test_collector_derives_features_from_bse_shape() -> None:
    """End-to-end: BSE payload -> mapped chain -> derived features, same as
    the NSE-sourced equivalent in test_nse_options_source.py."""
    from app.collectors.domains.options import OptionsChainSource, OptionsIntelligenceCollector

    class FixtureSource(OptionsChainSource):
        async def fetch_chain(self, instrument: str) -> dict:
            chain = map_bse_chain(BSE_CHAIN_PAYLOAD, expiry="16 Jul 2026")
            chain["prev_spot"] = 77000.0
            return chain

    collector = OptionsIntelligenceCollector(source=FixtureSource())
    collector.symbols = ["SENSEX"]
    records = await collector.collect()
    features = {r.metadata["feature"]: r for r in records}

    assert "pcr" in features
    assert "atm_iv" in features
    assert "buildup" in features  # prev_spot present -> classification emitted


def test_nearest_expiry_picks_earliest_by_date_not_string_order() -> None:
    """'13 Aug 2026' sorts before '16 Jul 2026' lexicographically but is
    later chronologically -- must compare as real dates, not strings."""
    rows = [
        {"Scrip_cd": 1, "Expiryofcontract": "13 Aug 2026"},
        {"Scrip_cd": 1, "Expiryofcontract": "16 Jul 2026"},
    ]
    assert pick_nearest_expiry(rows, "1") == "16 Jul 2026"


def test_nearest_expiry_filters_by_scrip_cd() -> None:
    rows = [
        {"Scrip_cd": 12, "Expiryofcontract": "16 Jul 2026"},  # different underlying
        {"Scrip_cd": 1, "Expiryofcontract": "23 Jul 2026"},
    ]
    assert pick_nearest_expiry(rows, "1") == "23 Jul 2026"


def test_nearest_expiry_raises_when_none_for_scrip_cd() -> None:
    with pytest.raises(CollectionError):
        pick_nearest_expiry([{"Scrip_cd": 12, "Expiryofcontract": "16 Jul 2026"}], "1")


def _future_expiry_str(days: int) -> str:
    """A BSE-format expiry string N days out from now -- computed relative
    to 'now' so this test doesn't go stale once a fixed date passes."""
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%d %b %Y")


def test_enrich_with_computed_greeks_populates_legs() -> None:
    expiry = _future_expiry_str(3)
    chain = {
        "spot": 77400.0,
        "expiry": expiry,
        "strikes": [
            {
                "strike": 77400.0,
                "call": {"oi": 100, "iv": 17.0, "ltp": 450.0},
                "put": {"oi": 100, "iv": 18.0, "ltp": 470.0},
            }
        ],
    }
    enrich_with_computed_greeks(chain, expiry=expiry)

    assert chain["greeks_enriched_legs"] == 2
    assert chain["greeks_source"] == "computed_black_scholes"
    call = chain["strikes"][0]["call"]
    put = chain["strikes"][0]["put"]
    assert 0.4 < call["delta"] < 0.6
    assert -0.6 < put["delta"] < -0.4
    assert call["gamma"] > 0
    assert call["vega"] > 0
    assert call["theta"] < 0  # long option decays


def test_enrich_with_computed_greeks_skips_legs_without_iv() -> None:
    expiry = _future_expiry_str(3)
    chain = {
        "spot": 77400.0,
        "expiry": expiry,
        "strikes": [
            {
                "strike": 78000.0,
                "call": {"oi": 0, "iv": None, "ltp": None},  # untraded strike
                "put": {"oi": 50, "iv": 19.0, "ltp": 300.0},
            }
        ],
    }
    enrich_with_computed_greeks(chain, expiry=expiry)

    assert chain["greeks_enriched_legs"] == 1  # only the put leg
    assert "delta" not in chain["strikes"][0]["call"]
    assert "delta" in chain["strikes"][0]["put"]


async def test_collector_atm_greeks_risk_features_use_computed_greeks() -> None:
    """Chapter 9's ATM Theta/Gamma/Vega risk features only fire when the
    chain carries Greeks -- confirms computed BSE Greeks unlock them, same
    as broker-fetched Greeks do for NSE."""
    from app.collectors.domains.options import OptionsChainSource, OptionsIntelligenceCollector

    expiry = _future_expiry_str(3)

    class FixtureSource(OptionsChainSource):
        async def fetch_chain(self, instrument: str) -> dict:
            chain = map_bse_chain(BSE_CHAIN_PAYLOAD, expiry=expiry)
            enrich_with_computed_greeks(chain, expiry=expiry)
            return chain

    collector = OptionsIntelligenceCollector(source=FixtureSource())
    collector.symbols = ["SENSEX"]
    records = await collector.collect()
    features = {r.metadata["feature"]: r for r in records}

    assert "atm_theta_pct" in features
    assert "atm_gamma" in features
    assert "atm_vega" in features
