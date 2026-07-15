"""Tests for the NSE intraday candle fallback source."""

from typing import Any

from app.collectors.sources.nse_candles import NSE_INDEX_NAMES, NseCandleSource


class FakeNseSession:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.requested_paths: list[str] = []
        self.closed = False

    async def get_json(self, path: str) -> Any:
        self.requested_paths.append(path)
        if path not in self.responses:
            raise AssertionError(f"unexpected path: {path}")
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        return response

    async def close(self) -> None:
        self.closed = True


EQUITY_PATH = (
    "/api/NextApi/apiClient/GetQuoteApi"
    "?functionName=getSymbolChartData&symbol=HDFCBANKEQN&days=1D"
)
INDEX_PATH = "/api/NextApi/apiClient?functionName=getGraphChart&&type=NIFTY 50&flag=1D"


async def test_equity_symbol_uses_symbol_chart_data_and_flat_response_shape() -> None:
    session = FakeNseSession({
        EQUITY_PATH: {
            "identifier": "HDFCBANKEQN",
            "grapthData": [[1752555600000, 810.5, "PO", "0", "0"], [1752555660000, 812.0, "PO", "0", "0"]],
        }
    })
    source = NseCandleSource(session=session)
    candles = await source.fetch_today("HDFCBANK", "1m")
    assert len(candles) >= 1
    assert session.requested_paths == [EQUITY_PATH]


async def test_index_symbol_uses_graph_chart_and_nested_response_shape() -> None:
    session = FakeNseSession({
        INDEX_PATH: {
            "data": {
                "identifier": "NIFTY 50",
                "grapthData": [[1752555600000, 24000.0, "PO", "0", "0"], [1752555660000, 24010.0, "PO", "0", "0"]],
            }
        }
    })
    source = NseCandleSource(session=session)
    candles = await source.fetch_today("NIFTY", "1m")
    assert len(candles) >= 1
    assert session.requested_paths == [INDEX_PATH]
    assert "NIFTY" in NSE_INDEX_NAMES  # sanity: this test exercises the real dispatch key


async def test_returns_empty_list_on_session_failure_never_raises() -> None:
    session = FakeNseSession({EQUITY_PATH: RuntimeError("nse rejected the request")})
    source = NseCandleSource(session=session)
    assert await source.fetch_today("HDFCBANK", "1m") == []


async def test_returns_empty_list_when_nse_has_no_data_yet() -> None:
    """Real observed shape before market open / for an unrecognized symbol:
    HTTP 200 with an empty grapthData, not an error."""
    session = FakeNseSession({EQUITY_PATH: {"identifier": "HDFCBANKEQN", "grapthData": []}})
    source = NseCandleSource(session=session)
    assert await source.fetch_today("HDFCBANK", "1m") == []
