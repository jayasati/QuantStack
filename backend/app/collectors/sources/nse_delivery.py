"""NSE security-wise delivery source (daily full bhavcopy).

NSE's quote-equity API sits behind aggressive bot protection, but the daily
"security-wise bhavcopy with delivery" CSV on the archives host is served
openly and carries the same numbers — plus history for any past session.
Files publish end-of-day; weekends, holidays, and not-yet-published dates
return 404. Session dates are normalized to midnight IST to match the daily
bar convention.
"""

import csv
import io
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")

ARCHIVE_URL = (
    "https://archives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
)

HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "accept": "*/*",
}


def parse_bhavcopy(
    text: str, symbols: set[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Extract per-symbol delivery records from a sec_bhavdata_full CSV.

    Only the EQ series carries meaningful delivery; rows with '-' delivery
    (new listings, suspended) are skipped. Restrict with `symbols` when given.
    """
    reader = csv.reader(io.StringIO(text))
    try:
        header = [column.strip() for column in next(reader)]
    except StopIteration:
        return {}
    index = {name: i for i, name in enumerate(header)}
    required = ("SYMBOL", "SERIES", "DATE1", "TTL_TRD_QNTY", "DELIV_QTY", "DELIV_PER")
    if any(column not in index for column in required):
        return {}

    out: dict[str, dict[str, Any]] = {}
    for row in reader:
        if len(row) < len(header):
            continue
        symbol = row[index["SYMBOL"]].strip().upper()
        if row[index["SERIES"]].strip() != "EQ":
            continue
        if symbols is not None and symbol not in symbols:
            continue
        deliv_per = row[index["DELIV_PER"]].strip()
        if deliv_per in ("", "-"):
            continue
        try:
            position_date = datetime.strptime(
                row[index["DATE1"]].strip(), "%d-%b-%Y"
            ).replace(tzinfo=IST)
            out[symbol] = {
                "symbol": symbol,
                "delivery_pct": float(deliv_per),
                "traded_qty": float(row[index["TTL_TRD_QNTY"]].strip() or 0),
                "delivered_qty": float(row[index["DELIV_QTY"]].strip() or 0),
                "position_date": position_date,
            }
        except (TypeError, ValueError):
            continue
    return out


class NseDeliverySource:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(headers=HEADERS, timeout=30.0)

    async def fetch_day(
        self, session_date: date, symbols: set[str] | None = None
    ) -> dict[str, dict[str, Any]] | None:
        """Delivery records for one session; None when no file exists (holiday,
        weekend, or not yet published)."""
        url = ARCHIVE_URL.format(ddmmyyyy=session_date.strftime("%d%m%Y"))
        response = await self._client.get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return parse_bhavcopy(response.text, symbols)

    async def close(self) -> None:
        await self._client.aclose()
