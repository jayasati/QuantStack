"""Tests for the Macro Intelligence Collector (Prompt 2.8)."""

import pytest

from app.collectors.base import CollectionError
from app.collectors.domains.macro import (
    COMPOSITE_INSTRUMENT,
    FACTOR_SIGNS,
    MacroFactorPayload,
    MacroIntelligenceCollector,
    MacroSource,
)
from app.collectors.schema import CollectorCategory, CollectorOutput, Direction


class FakeMacroSource(MacroSource):
    def __init__(self, data: dict[str, MacroFactorPayload]) -> None:
        self._data = data

    async def fetch_macro(self) -> dict[str, MacroFactorPayload]:
        return dict(self._data)


def _payload(value: float, change: float, zscore: float) -> MacroFactorPayload:
    return {"value": value, "change_1d_pct": change, "zscore_20d": zscore}


# Deterministic risk-off regime: strong dollar, weak rupee, high crude,
# risk-off gold bid, and weak global equities -> clear negative macro pressure.
NEGATIVE_PRESSURE: dict[str, MacroFactorPayload] = {
    "USDINR": _payload(84.9, 0.6, 2.0),
    "DXY": _payload(107.2, 0.8, 2.5),
    "US10Y": _payload(4.8, 1.2, 1.5),
    "INDIA10Y": _payload(7.3, 0.5, 1.0),
    "CRUDE": _payload(92.4, 2.5, 2.0),
    "GOLD": _payload(2450.0, 1.1, 1.5),
    "SILVER": _payload(31.2, 0.9, 0.5),
    "NATGAS": _payload(3.4, 1.8, 1.0),
    "SPX": _payload(5400.0, -1.9, -2.0),
    "NDX": _payload(19200.0, -2.6, -2.5),
    "NIKKEI": _payload(38000.0, -1.4, -1.5),
    "HANGSENG": _payload(17500.0, -1.0, -1.0),
    "DAX": _payload(18200.0, -1.3, -1.5),
    "CRYPTO_MCAP": _payload(2.1e12, -3.5, -2.0),
}


def _by_instrument(records: list[CollectorOutput]) -> dict[str, CollectorOutput]:
    return {record.instrument: record for record in records}


async def _collect(data: dict[str, MacroFactorPayload]) -> dict[str, CollectorOutput]:
    collector = MacroIntelligenceCollector(macro_source=FakeMacroSource(data))
    return _by_instrument(await collector.collect())


async def test_per_factor_sign_conventions() -> None:
    records = _by_instrument(
        await MacroIntelligenceCollector(
            macro_source=FakeMacroSource(NEGATIVE_PRESSURE)
        ).collect()
    )

    # Rising USDINR / DXY / US10Y / CRUDE = negative pressure for Indian equities.
    for factor in ("USDINR", "DXY", "US10Y", "INDIA10Y", "CRUDE", "NATGAS"):
        value = records[factor].normalized_value
        assert value is not None and value < 0, factor
        assert records[factor].direction is Direction.BEARISH, factor

    # Rising gold = risk-off tilt -> negative score.
    assert records["GOLD"].normalized_value is not None
    assert records["GOLD"].normalized_value < 0

    # Falling global equities = risk-off -> negative score.
    for factor in ("SPX", "NDX", "NIKKEI", "HANGSENG", "DAX", "CRYPTO_MCAP"):
        value = records[factor].normalized_value
        assert value is not None and value < 0, factor


async def test_usdinr_up_exact_score_and_metadata() -> None:
    records = await _collect(NEGATIVE_PRESSURE)
    usdinr = records["USDINR"]
    # zscore 2.0 clamped to [-3, 3], scaled to [-1, 1], sign -1 -> -2/3.
    assert usdinr.normalized_value == pytest.approx(-2.0 / 3.0)
    assert usdinr.collector_category is CollectorCategory.MACRO
    assert usdinr.metadata["sign"] == -1.0
    assert usdinr.metadata["zscore_20d"] == 2.0


async def test_rising_global_equities_positive() -> None:
    records = await _collect({"SPX": _payload(5600.0, 1.5, 1.8)})
    spx = records["SPX"]
    assert spx.normalized_value == pytest.approx(1.8 / 3.0)
    assert spx.direction is Direction.BULLISH


async def test_composite_negative_pressure_is_bearish() -> None:
    records = await _collect(NEGATIVE_PRESSURE)
    composite = records[COMPOSITE_INSTRUMENT]

    assert composite.normalized_value is not None
    assert composite.normalized_value < -0.3
    assert composite.direction is Direction.BEARISH
    assert set(composite.metadata["components"]) == set(NEGATIVE_PRESSURE)
    assert composite.metadata["coverage"] == pytest.approx(1.0)
    # Every input factor score is bounded.
    for score in composite.metadata["components"].values():
        assert -1.0 <= score <= 1.0


async def test_clamping_extreme_zscores() -> None:
    records = await _collect(
        {
            "USDINR": _payload(90.0, 5.0, 10.0),  # extreme dollar strength
            "SPX": _payload(4000.0, -9.0, -12.0),  # extreme equity crash
        }
    )
    assert records["USDINR"].normalized_value == pytest.approx(-1.0)
    assert records["SPX"].normalized_value == pytest.approx(-1.0)
    composite = records[COMPOSITE_INSTRUMENT]
    assert composite.normalized_value == pytest.approx(-1.0)
    assert composite.direction is Direction.BEARISH


async def test_unknown_factor_skipped_and_weights_renormalized() -> None:
    records = await _collect(
        {
            "USDINR": _payload(84.0, 0.1, 1.5),
            "PLUTONIUM": _payload(1.0, 0.0, 0.0),
        }
    )
    assert "PLUTONIUM" not in records
    composite = records[COMPOSITE_INSTRUMENT]
    # Only USDINR present -> composite equals its factor score.
    assert composite.normalized_value == pytest.approx(-1.5 / 3.0)


async def test_unconfigured_default_raises_collection_error() -> None:
    collector = MacroIntelligenceCollector()
    with pytest.raises(CollectionError, match="macro source not configured"):
        await collector.collect()


def test_sign_map_covers_all_spec_factors() -> None:
    expected = {
        "USDINR",
        "DXY",
        "US10Y",
        "INDIA10Y",
        "CRUDE",
        "GOLD",
        "SILVER",
        "NATGAS",
        "SPX",
        "NDX",
        "NIKKEI",
        "HANGSENG",
        "DAX",
        "CRYPTO_MCAP",
    }
    assert set(FACTOR_SIGNS) == expected
