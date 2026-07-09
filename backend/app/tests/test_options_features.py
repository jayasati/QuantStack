"""Tests for the Prompt 2.4 completion: distributions, IV percentile,
Greeks enrichment, and market-hours gating."""

from datetime import UTC, datetime

import pytest

from app.collectors.base import (
    BaseCollector,
    CollectorPipeline,
    is_nse_market_open,
)
from app.collectors.domains.options import (
    IvHistoryProvider,
    OptionsChainSource,
    OptionsIntelligenceCollector,
)
from app.collectors.schema import CollectorCategory, CollectorOutput, Direction

CHAIN = {
    "spot": 100.0,
    "prev_spot": 99.0,
    "strikes": [
        {
            "strike": 90.0,
            "call": {"oi": 100, "oi_change": 10, "iv": 11.0, "volume": 500, "ltp": 11.0},
            "put": {"oi": 4000, "oi_change": 200, "iv": 14.0, "volume": 3000, "ltp": 0.5},
        },
        {
            "strike": 100.0,
            "call": {"oi": 1000, "oi_change": 50, "iv": 12.0, "volume": 8000, "ltp": 2.0},
            "put": {"oi": 1000, "oi_change": 50, "iv": 12.0, "volume": 7000, "ltp": 2.0},
        },
        {
            "strike": 110.0,
            "call": {"oi": 1500, "oi_change": 100, "iv": 12.5, "volume": 1000, "ltp": 0.3},
            "put": {"oi": 100, "oi_change": 5, "iv": 15.0, "volume": 200, "ltp": 10.5},
        },
    ],
}


class FixtureSource(OptionsChainSource):
    def __init__(self, chain: dict) -> None:
        self._chain = chain

    async def fetch_chain(self, instrument: str) -> dict:
        return self._chain


class FixtureIvHistory(IvHistoryProvider):
    def __init__(self, values: list[float]) -> None:
        self._values = values

    async def history(self, instrument: str) -> list[float]:
        return self._values


def make_collector(
    chain: dict = CHAIN, iv_values: list[float] | None = None
) -> OptionsIntelligenceCollector:
    collector = OptionsIntelligenceCollector(
        source=FixtureSource(chain),
        iv_history=FixtureIvHistory(iv_values or []),
    )
    collector.symbols = ["TEST"]
    return collector


async def features(collector: OptionsIntelligenceCollector) -> dict[str, CollectorOutput]:
    records = await collector.collect()
    return {r.metadata["feature"]: r for r in records}


# --- OI distribution ---------------------------------------------------------------


async def test_oi_distribution_support_heavy_is_bullish() -> None:
    got = await features(make_collector())
    record = got["oi_distribution"]
    # put OI below spot = 4000, call OI above spot = 1500 -> support-heavy
    assert record.normalized_value == pytest.approx((4000 - 1500) / 5500)
    assert record.direction is Direction.BULLISH
    assert record.metadata["support_put_oi_below_spot"] == 4000
    assert record.metadata["resistance_call_oi_above_spot"] == 1500


# --- Volume distribution -------------------------------------------------------------


async def test_volume_distribution_concentration_and_pcr() -> None:
    got = await features(make_collector())
    record = got["volume_distribution"]
    total = 500 + 3000 + 8000 + 7000 + 1000 + 200
    # top-3 strike volumes: 15000 (ATM) + 3500 (90) + 1200 (110) = all three strikes
    assert record.normalized_value == pytest.approx(1.0)
    assert record.metadata["call_volume"] == 500 + 8000 + 1000
    assert record.metadata["put_volume"] == 3000 + 7000 + 200
    assert record.metadata["volume_pcr"] == pytest.approx(10200 / 9500)
    assert 0 < record.metadata["volume_weighted_strike"] < 110
    assert total == 19700  # fixture sanity


# --- IV percentile -------------------------------------------------------------------


async def test_iv_percentile_emits_with_enough_history() -> None:
    history = [10.0] * 90 + [14.0] * 60  # 150 observations, current ATM IV = 12.0
    got = await features(make_collector(iv_values=history))
    record = got["iv_percentile"]
    assert record.normalized_value == pytest.approx(100.0 * 90 / 150)
    assert record.metadata["observations"] == 150


async def test_iv_percentile_silent_without_history() -> None:
    got = await features(make_collector(iv_values=[12.0] * 10))  # below minimum
    assert "iv_percentile" not in got


# --- Greeks flow through exposure features -------------------------------------------


async def test_greeks_in_chain_activate_exposures() -> None:
    chain = {
        "spot": 100.0,
        "strikes": [
            {
                "strike": 100.0,
                "call": {
                    "oi": 1000, "oi_change": 0, "iv": 12.0, "volume": 10, "ltp": 2.0,
                    "gamma": 0.05, "delta": 0.5,
                },
                "put": {
                    "oi": 800, "oi_change": 0, "iv": 12.0, "volume": 10, "ltp": 2.0,
                    "gamma": 0.05, "delta": -0.5,
                },
            }
        ],
    }
    got = await features(make_collector(chain))
    assert got["gamma_exposure"].normalized_value == pytest.approx(0.05 * 1000 - 0.05 * 800)
    assert got["delta_exposure"].normalized_value == pytest.approx(0.5 * 1000 + -0.5 * 800)
    assert got["delta_exposure"].direction is Direction.BULLISH


async def test_atm_greeks_risk_theta_gamma_vega() -> None:
    chain = {
        "spot": 100.0,
        "strikes": [
            {
                "strike": 90.0,  # far OTM; excluded from ATM selection
                "call": {"oi": 100, "oi_change": 0, "iv": 20.0, "volume": 10, "ltp": 11.0,
                         "gamma": 0.01, "delta": 0.9, "theta": -0.02, "vega": 0.03},
                "put": {"oi": 100, "oi_change": 0, "iv": 20.0, "volume": 10, "ltp": 0.1,
                        "gamma": 0.01, "delta": -0.1, "theta": -0.01, "vega": 0.03},
            },
            {
                "strike": 100.0,  # ATM: nearest to spot
                "call": {"oi": 1000, "oi_change": 0, "iv": 15.0, "volume": 10, "ltp": 2.0,
                         "gamma": 0.05, "delta": 0.5, "theta": -0.30, "vega": 0.12},
                "put": {"oi": 900, "oi_change": 0, "iv": 15.0, "volume": 10, "ltp": 1.8,
                        "gamma": 0.05, "delta": -0.5, "theta": -0.28, "vega": 0.12},
            },
        ],
    }
    got = await features(make_collector(chain))

    # Theta % of premium uses the ATM strike only, magnitude-expressed.
    avg_theta = (0.30 + 0.28) / 2
    avg_premium = (2.0 + 1.8) / 2
    assert got["atm_theta_pct"].normalized_value == pytest.approx(avg_theta / avg_premium * 100)
    assert got["atm_theta_pct"].metadata["atm_strike"] == 100.0

    assert got["atm_gamma"].normalized_value == pytest.approx(0.05 + 0.05)
    assert got["atm_vega"].normalized_value == pytest.approx(0.12 + 0.12)


async def test_atm_greeks_risk_absent_without_greeks() -> None:
    got = await features(make_collector(CHAIN))  # module-level CHAIN carries no Greeks
    assert "atm_theta_pct" not in got
    assert "atm_gamma" not in got
    assert "atm_vega" not in got


# --- Market-hours gating --------------------------------------------------------------


def test_market_hours_calendar() -> None:
    # Saturday
    assert not is_nse_market_open(datetime(2026, 7, 4, 11, 0, tzinfo=UTC))
    # Monday 10:30 IST == 05:00 UTC
    assert is_nse_market_open(datetime(2026, 7, 6, 5, 0, tzinfo=UTC))
    # Monday 08:00 IST (pre-open) == 02:30 UTC
    assert not is_nse_market_open(datetime(2026, 7, 6, 2, 30, tzinfo=UTC))
    # Monday 16:30 IST (after close) == 11:00 UTC
    assert not is_nse_market_open(datetime(2026, 7, 6, 11, 0, tzinfo=UTC))


class _NullPipeline(CollectorPipeline):
    async def process(self, collector, records, latency_ms):
        return records

    async def record_failure(self, collector, error):
        pass


class GatedCollector(BaseCollector):
    name = "gated"
    category = CollectorCategory.OPTIONS
    source = "test"
    market_hours_only = True

    def __init__(self) -> None:
        super().__init__()
        self.collected = 0

    async def collect(self) -> list[CollectorOutput]:
        self.collected += 1
        return []


async def test_scheduled_run_skips_outside_market_hours(monkeypatch) -> None:
    import app.collectors.base as base_module

    monkeypatch.setattr(base_module, "is_nse_market_open", lambda now=None: False)
    collector = GatedCollector()
    await collector.run_once(_NullPipeline())
    assert collector.collected == 0
    assert collector.health.run_count == 0
    assert collector.health.extras["skipped_market_closed"] == 1

    # force=True bypasses the gate (manual /run endpoint)
    await collector.run_once(_NullPipeline(), force=True)
    assert collector.collected == 1


async def test_options_collector_is_market_hours_only() -> None:
    assert OptionsIntelligenceCollector.market_hours_only is True
