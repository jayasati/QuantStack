"""Instrument lookup service.

Maps friendly symbols (NIFTY, RELIANCE, ...) to SmartAPI symbol tokens.
Index tokens are known constants; equities resolve through the Angel One
scrip master, downloaded once and cached on disk with a daily refresh.
"""

import json
import time
from pathlib import Path

import httpx

from app.core.config import REPO_ROOT
from app.core.logging import get_logger

logger = get_logger(__name__)

SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)
CACHE_FILE = REPO_ROOT / "data" / "instruments.json"
CACHE_MAX_AGE_SECONDS = 24 * 3600

# NSE index tokens are stable, published constants.
INDEX_TOKENS = {
    "NIFTY": ("99926000", "NSE", "Nifty 50"),
    "BANKNIFTY": ("99926009", "NSE", "Nifty Bank"),
    "FINNIFTY": ("99926037", "NSE", "Nifty Fin Service"),
    "MIDCPNIFTY": ("99926074", "NSE", "NIFTY MID SELECT"),
    "INDIAVIX": ("99926017", "NSE", "India VIX"),
    "SENSEX": ("99919000", "BSE", "SENSEX"),
}


class InstrumentService:
    def __init__(self, cache_file: Path = CACHE_FILE) -> None:
        self._cache_file = cache_file
        self._by_symbol: dict[str, tuple[str, str, str]] = {}
        self._loaded = False

    def resolve(self, symbol: str, exchange: str = "NSE") -> tuple[str, str, str]:
        """Return (token, exchange, trading_symbol) for a friendly symbol."""
        upper = symbol.upper()
        if upper in INDEX_TOKENS:
            return INDEX_TOKENS[upper]
        if not self._loaded:
            self._load()
        key = f"{exchange}:{upper}"
        if key in self._by_symbol:
            return self._by_symbol[key]
        raise KeyError(f"unknown instrument: {exchange}:{symbol}")

    def _load(self) -> None:
        rows = self._read_cache()
        if rows is None:
            rows = self._download()
        for row in rows:
            exch = row.get("exch_seg", "")
            trading_symbol = row.get("symbol", "")
            name = row.get("name", "")
            token = row.get("token", "")
            if not (exch and trading_symbol and token):
                continue
            # Equities appear as e.g. RELIANCE-EQ; register both spellings.
            self._by_symbol[f"{exch}:{trading_symbol.upper()}"] = (token, exch, trading_symbol)
            if trading_symbol.upper().endswith("-EQ"):
                self._by_symbol.setdefault(
                    f"{exch}:{name.upper()}", (token, exch, trading_symbol)
                )
        self._loaded = True
        logger.info("instrument master loaded", extra={"instruments": len(self._by_symbol)})

    def _read_cache(self) -> list[dict] | None:
        try:
            if not self._cache_file.exists():
                return None
            if time.time() - self._cache_file.stat().st_mtime > CACHE_MAX_AGE_SECONDS:
                return None
            return json.loads(self._cache_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("instrument cache unreadable", extra={"error": str(exc)})
            return None

    def _download(self) -> list[dict]:
        logger.info("downloading instrument master (first run or stale cache)")
        response = httpx.get(SCRIP_MASTER_URL, timeout=120.0)
        response.raise_for_status()
        rows = response.json()
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            self._cache_file.write_text(json.dumps(rows), encoding="utf-8")
        except Exception as exc:
            logger.warning("could not cache instrument master", extra={"error": str(exc)})
        return rows
