"""Tests for the options intelligence collector (Prompt 2.4)."""

from typing import Any

import pytest

from app.collectors.base import CollectionError
from app.collectors.domains.options import OptionsChainSource, OptionsIntelligenceCollector
from app.collectors.schema import CollectorOutput, Direction


def _leg(oi: float, oi_change: float, iv: float, volume: float, ltp: float) -> dict[str, float]:
    return {"oi": oi, "oi_change": oi_change, "iv": iv, "volume": volume, "ltp": ltp}


# Rising price (99 -> 100) with rising total OI across the chain.
# Total call OI = 1500, total put OI = 1600 -> PCR = 1600 / 1500.
# Max pain payout is minimized at strike 100 (put OI at 90 never pays above 90).
FIXTURE_CHAIN: dict[str, Any] = {
    "spot": 100.0,
    "prev_spot": 99.0,
    "strikes": [
        {
            "strike": 90.0,
            "call": _leg(100, 10, 22.0, 50, 10.5),
            "put": _leg(600, 60, 26.0, 40, 0.2),
        },
        {
            "strike": 95.0,
            "call": _leg(200, 20, 18.0, 80, 5.8),
            "put": _leg(400, 40, 22.0, 70, 0.7),
        },
        {
            "strike": 100.0,
            "call": _leg(300, 30, 16.0, 120, 2.1),
            "put": _leg(300, 30, 16.5, 110, 2.0),
        },
        {
            "strike": 105.0,
            "call": _leg(400, 40, 15.0, 90, 0.6),
            "put": _leg(200, 20, 20.0, 60, 5.5),
        },
        {
            "strike": 110.0,
            "call": _leg(500, 50, 14.0, 60, 0.2),
            "put": _leg(100, 10, 21.0, 30, 10.2),
        },
    ],
}


class FakeOptionsSource(OptionsChainSource):
    def __init__(self, chain: dict[str, Any]) -> None:
        self.chain = chain

    async def fetch_chain(self, instrument: str) -> dict[str, Any]:
        return self.chain


def make_collector(chain: dict[str, Any]) -> OptionsIntelligenceCollector:
    collector = OptionsIntelligenceCollector(source=FakeOptionsSource(chain))
    collector.symbols = ["NIFTY"]
    return collector


def feature(records: list[CollectorOutput], name: str) -> CollectorOutput:
    matches = [r for r in records if r.metadata["feature"] == name]
    assert len(matches) == 1, f"expected exactly one {name!r} record, got {len(matches)}"
    return matches[0]


async def test_pcr_matches_fixture() -> None:
    records = await make_collector(FIXTURE_CHAIN).collect()
    pcr = feature(records, "pcr")
    assert pcr.normalized_value == pytest.approx(1600 / 1500)
    assert pcr.metadata["total_call_oi"] == 1500
    assert pcr.metadata["total_put_oi"] == 1600
    assert pcr.instrument == "NIFTY"


async def test_max_pain_strike_correct() -> None:
    records = await make_collector(FIXTURE_CHAIN).collect()
    max_pain = feature(records, "max_pain")
    assert max_pain.normalized_value == 100.0
    assert max_pain.direction is Direction.NEUTRAL  # max pain sits exactly at spot


async def test_rising_price_and_oi_is_long_buildup() -> None:
    records = await make_collector(FIXTURE_CHAIN).collect()
    buildup = feature(records, "buildup")
    assert buildup.raw_value == "long_buildup"
    assert buildup.direction is Direction.BULLISH
    assert buildup.normalized_value == 1.0
    assert buildup.metadata["price_change"] == pytest.approx(1.0)
    assert buildup.metadata["total_oi_change"] > 0


async def test_atm_iv_skew_and_concentration() -> None:
    records = await make_collector(FIXTURE_CHAIN).collect()
    assert feature(records, "atm_iv").normalized_value == pytest.approx((16.0 + 16.5) / 2)
    # OTM puts (90, 95) mean 24.0; OTM calls (105, 110) mean 14.5.
    skew = feature(records, "iv_skew")
    assert skew.normalized_value == pytest.approx(24.0 - 14.5)
    assert skew.direction is Direction.BEARISH
    # Per-strike OI: 700, 600, 600, 600, 600 -> top-3 share = 1900 / 3100.
    assert feature(records, "oi_concentration").normalized_value == pytest.approx(1900 / 3100)


async def test_no_greeks_means_no_exposure_records() -> None:
    records = await make_collector(FIXTURE_CHAIN).collect()
    features = {r.metadata["feature"] for r in records}
    assert "gamma_exposure" not in features
    assert "delta_exposure" not in features


async def test_unconfigured_default_source_raises() -> None:
    collector = OptionsIntelligenceCollector()
    with pytest.raises(CollectionError, match="options chain source not configured"):
        await collector.collect()
