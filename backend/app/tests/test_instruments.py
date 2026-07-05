import json
from pathlib import Path

from app.market.instruments import InstrumentService


def test_index_tokens_resolve_without_download(tmp_path: Path) -> None:
    service = InstrumentService(cache_file=tmp_path / "missing.json")
    token, exchange, name = service.resolve("NIFTY")
    assert token == "99926000"
    assert exchange == "NSE"
    assert name == "Nifty 50"
    assert service.resolve("banknifty")[0] == "99926009"


def test_equity_resolves_from_cached_master(tmp_path: Path) -> None:
    cache = tmp_path / "instruments.json"
    cache.write_text(
        json.dumps(
            [
                {
                    "token": "2885",
                    "symbol": "RELIANCE-EQ",
                    "name": "RELIANCE",
                    "exch_seg": "NSE",
                }
            ]
        ),
        encoding="utf-8",
    )
    service = InstrumentService(cache_file=cache)
    token, exchange, trading_symbol = service.resolve("RELIANCE")
    assert token == "2885"
    assert trading_symbol == "RELIANCE-EQ"
    # Full trading symbol spelling also resolves
    assert service.resolve("RELIANCE-EQ")[0] == "2885"


def test_unknown_symbol_raises(tmp_path: Path) -> None:
    cache = tmp_path / "instruments.json"
    cache.write_text("[]", encoding="utf-8")
    service = InstrumentService(cache_file=cache)
    try:
        service.resolve("NOSUCHSYMBOL")
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
