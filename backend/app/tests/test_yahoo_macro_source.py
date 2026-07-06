"""Offline tests for the Yahoo macro source (mocked transport)."""

import json
from statistics import fmean, pstdev

import httpx
import pytest

from app.collectors.base import CollectionError
from app.collectors.sources.yahoo_macro import (
    TICKERS,
    YahooMacroSource,
    factor_metrics,
)


def chart_response(closes: list[float], live: float | None = None) -> dict:
    return {
        "chart": {
            "result": [
                {
                    "meta": {"regularMarketPrice": live},
                    "indicators": {"quote": [{"close": closes}]},
                }
            ]
        }
    }


def test_factor_metrics_math() -> None:
    closes = [100.0 + i * 0.5 for i in range(30)]
    live = 120.0
    metrics = factor_metrics(list(closes), live)
    assert metrics is not None
    assert metrics["value"] == 120.0
    assert metrics["change_1d_pct"] == pytest.approx((120.0 / closes[-1] - 1) * 100)
    window = [*closes, live][-20:]
    expected_z = (120.0 - fmean(window)) / pstdev(window)
    assert metrics["zscore_20d"] == pytest.approx(expected_z)


def test_factor_metrics_handles_nones_and_short_series() -> None:
    assert factor_metrics([None, None], None) is None
    assert factor_metrics([100.0], None) is None  # one point is not a series
    assert factor_metrics([100.0], 101.0) is not None  # live value adds the 2nd point
    metrics = factor_metrics([100.0, None, 101.0], None)
    assert metrics is not None
    assert metrics["value"] == 101.0
    assert metrics["zscore_20d"] is None  # window too short for a z-score


def make_source(handler) -> YahooMacroSource:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        transport=transport, base_url="https://query1.finance.yahoo.com"
    )
    return YahooMacroSource(client=client)


async def test_fetch_macro_maps_all_factors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=chart_response([100.0 + i for i in range(30)], live=131.0)
        )

    source = make_source(handler)
    factors = await source.fetch_macro()
    assert set(factors) == set(TICKERS)
    for payload in factors.values():
        assert payload["value"] == 131.0


async def test_partial_failures_tolerated_and_cached() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if "INR" in str(request.url) or "TNX" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, json=chart_response([10.0] * 25, live=10.5))

    source = make_source(handler)
    factors = await source.fetch_macro()
    assert "USDINR" not in factors
    assert "US10Y" not in factors
    assert len(factors) == len(TICKERS) - 2

    before = calls["n"]
    await source.fetch_macro()  # cached
    assert calls["n"] == before


async def test_too_few_factors_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    source = make_source(handler)
    with pytest.raises(CollectionError, match="macro factors available"):
        await source.fetch_macro()


def test_factor_metrics_handles_live_none() -> None:
    metrics = factor_metrics([float(i) for i in range(100, 130)], None)
    assert metrics is not None
    assert metrics["value"] == 129.0


def test_chart_parse_error_shape() -> None:
    parsed = YahooMacroSource._parse_chart(json.loads('{"chart": {"result": null}}'))
    assert parsed is None
