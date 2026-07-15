"""Tests for the BSE intraday candle fallback source (SENSEX)."""

import json

import httpx

from app.collectors.sources.bse_candles import (
    BSE_INDEX_CODES,
    BseCandleSource,
    _parse_sensex_graph_response,
)

# Real observed shape (2026-07-15): the HTTP body is itself a JSON *string*
# containing two JSON arrays joined by "#@#" -- a snapshot summary, then the
# actual per-minute tick series.
SAMPLE_BODY = (
    '[{"currDate":"09:00:59","LatestVal":"77185.43"}]'
    "#@#"
    '[{"date":"Wed Jul 15 2026 09:00:59","value1":"77579.95"},'
    '{"date":"Wed Jul 15 2026 09:01:59","value1":"77394.76"}]'
)


def test_parses_the_hash_at_hash_delimited_double_json_shape() -> None:
    ticks = _parse_sensex_graph_response(SAMPLE_BODY)
    assert len(ticks) == 2
    (ts0, price0), (ts1, price1) = ticks
    assert price0 == 77579.95
    assert price1 == 77394.76
    assert ts0.hour == 9 and ts0.minute == 0
    assert ts1.minute == 1


def test_malformed_body_without_delimiter_yields_no_ticks() -> None:
    assert _parse_sensex_graph_response("not the expected shape at all") == []


def test_unparseable_second_segment_yields_no_ticks() -> None:
    assert _parse_sensex_graph_response("[]#@#not json") == []


async def test_fetch_today_returns_empty_for_unmapped_symbol_without_any_request() -> None:
    source = BseCandleSource()
    assert "HDFCBANK" not in BSE_INDEX_CODES
    assert await source.fetch_today("HDFCBANK", "1m") == []


async def test_fetch_today_parses_and_buckets_sensex_ticks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sensex/code/16":
            return httpx.Response(200, text="<html>ok</html>")
        if "SensexGraphData" in request.url.path:
            return httpx.Response(200, text=json.dumps(SAMPLE_BODY))
        raise AssertionError(f"unexpected request: {request.url}")

    api_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.bseindia.com",
    )
    warm_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://www.bseindia.com",
    )
    source = BseCandleSource(client=api_client, warm_client=warm_client)
    candles = await source.fetch_today("SENSEX", "1m")
    assert len(candles) == 2
    assert candles[0].close == 77579.95
    await source.close()
