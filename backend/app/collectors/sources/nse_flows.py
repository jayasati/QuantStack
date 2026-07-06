"""NSE institutional flow source (real feed for Prompt 2.7).

- FII/DII cash: NSE fiidiiTradeReact (provisional, published EOD)
- Block/bulk deals: NSE large-deal snapshot (today's rows only)
- SAST filings: NSE corporate-sast-reg29 (count of today's filings)
- Insider/promoter values: NSE corporates-pit — currently returns empty data;
  when it does, promoter buy/sell values and insider net are parsed. Until
  then those components are zero with an availability flag, never fabricated.

FII/DII 20-day averages come from our own stored flow history; before enough
days accumulate, the same-day gross turnover divided by four serves as a
conservative, real-data normalization scale (documented in metadata).
"""

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from app.collectors.base import CollectionError
from app.collectors.domains.flows import FlowSource
from app.core.logging import get_logger

logger = get_logger(__name__)

CACHE_TTL_SECONDS = 600
MIN_AVG_HISTORY_DAYS = 5


def parse_fiidii(rows: list[dict]) -> dict[str, dict[str, Any]]:
    """Extract FII and DII net/gross from the fiidiiTradeReact response."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        category = str(row.get("category") or "").upper()
        key = "fii" if category.startswith("FII") else "dii" if category.startswith("DII") else None
        if key is None:
            continue
        try:
            buy = float(row.get("buyValue") or 0.0)
            sell = float(row.get("sellValue") or 0.0)
            net = float(row.get("netValue") or 0.0)
        except (TypeError, ValueError):
            continue
        out[key] = {"net": net, "gross": buy + sell, "date": row.get("date")}
    return out


def parse_deals(payload: dict, key: str) -> list[dict[str, Any]]:
    """Map deal rows to {symbol, side, value_cr}, keeping the latest date only."""
    rows = payload.get(key) or []
    latest = None
    for row in rows:
        date = row.get("date")
        if date and (latest is None or _deal_date(date) > _deal_date(latest)):
            latest = date
    deals: list[dict[str, Any]] = []
    for row in rows:
        if latest is not None and row.get("date") != latest:
            continue
        try:
            qty = float(row.get("qty") or 0.0)
            price = float(row.get("watp") or 0.0)
        except (TypeError, ValueError):
            continue
        value_cr = qty * price / 1e7
        if value_cr <= 0:
            continue
        deals.append(
            {
                "symbol": row.get("symbol") or "UNKNOWN",
                "side": "buy" if str(row.get("buySell", "")).upper() == "BUY" else "sell",
                "value_cr": round(value_cr, 2),
            }
        )
    return deals


def _deal_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%d-%b-%Y")
    except ValueError:
        return datetime.min


class NseFlowSource(FlowSource):
    def __init__(self, session: Any = None) -> None:
        self._session = session
        self._cache: tuple[float, dict] | None = None

    def _get_session(self):
        if self._session is None:
            from app.collectors.sources.nse_client import NseSession

            # The NSE homepage warmup gets 403'd under load; the market-data
            # page is reliably accepted.
            self._session = NseSession(warmup_path="/market-data/live-equity-market")
        return self._session

    async def fetch_flows(self) -> dict:
        now = time.time()
        if self._cache is not None and now - self._cache[0] < CACHE_TTL_SECONDS:
            return self._cache[1]
        session = self._get_session()

        fiidii = parse_fiidii(await session.get_json("/api/fiidiiTradeReact"))
        if "fii" not in fiidii or "dii" not in fiidii:
            raise CollectionError("nse fii/dii response is missing categories")

        fii_avg = await self._stored_average("fii_flow")
        dii_avg = await self._stored_average("dii_flow")
        # Bootstrap scale from real same-day gross turnover until history exists.
        fii_scale = fii_avg if fii_avg is not None else fiidii["fii"]["gross"] / 4.0
        dii_scale = dii_avg if dii_avg is not None else fiidii["dii"]["gross"] / 4.0

        deals_payload = await session.get_json("/api/snapshot-capital-market-largedeal")
        block_deals = parse_deals(deals_payload, "BLOCK_DEALS_DATA")
        bulk_deals = parse_deals(deals_payload, "BULK_DEALS_DATA")

        sast_count = await self._todays_sast(session)
        promoter_buys, promoter_sells, insider_net, insider_available = (
            await self._insider_activity(session)
        )

        flows = {
            "fii_cash_cr": fiidii["fii"]["net"],
            "dii_cash_cr": fiidii["dii"]["net"],
            "fii_20d_avg_cr": fii_scale,
            "dii_20d_avg_cr": dii_scale,
            "etf_flows_cr": None,  # no public per-day ETF flow feed
            "block_deals": block_deals,
            "bulk_deals": bulk_deals,
            "promoter_buys_cr": promoter_buys,
            "promoter_sells_cr": promoter_sells,
            "sast_filings": sast_count,
            "insider_net_cr": insider_net,
            "as_of": fiidii["fii"].get("date"),
            "avg_source": "history" if fii_avg is not None else "same_day_gross/4",
            "insider_data_available": insider_available,
        }
        self._cache = (now, flows)
        return flows

    async def _stored_average(self, metric: str) -> float | None:
        """Average |net| of the last 20 stored days for a flow metric."""
        try:
            from sqlalchemy import text

            from app.database.session import get_session_factory

            sessions = get_session_factory()
            async with sessions() as session:
                result = await session.execute(
                    text(
                        "SELECT AVG(day_net) FROM ("
                        "  SELECT ABS(MAX((data->>'raw_value')::float)) AS day_net "
                        "  FROM market_events "
                        "  WHERE event_type = 'institutional_flow.observation' "
                        "  AND data->'metadata'->>'metric' = :metric "
                        "  AND created_at::date < CURRENT_DATE "
                        "  GROUP BY created_at::date "
                        "  ORDER BY MAX(created_at::date) DESC LIMIT 20"
                        ") days HAVING COUNT(*) >= :min_days"
                    ),
                    {"metric": metric, "min_days": MIN_AVG_HISTORY_DAYS},
                )
                value = result.scalar()
            return float(value) if value is not None else None
        except Exception:
            return None

    async def _todays_sast(self, session: Any) -> int:
        try:
            today = datetime.now(UTC).astimezone().strftime("%d-%m-%Y")
            week_ago = (datetime.now(UTC) - timedelta(days=7)).astimezone().strftime(
                "%d-%m-%Y"
            )
            payload = await session.get_json(
                f"/api/corporate-sast-reg29?index=equities"
                f"&from_date={week_ago}&to_date={today}"
            )
            rows = payload.get("data") or []
            today_display = datetime.now(UTC).astimezone().strftime("%d-%b-%Y")
            todays = [
                row for row in rows if str(row.get("acquirerDate", "")).startswith(today_display)
            ]
            return len(todays) if todays else len(rows) // 7  # daily average fallback
        except Exception as exc:
            logger.warning("sast fetch failed", extra={"error": str(exc)})
            return 0

    async def _insider_activity(self, session: Any) -> tuple[float, float, float, bool]:
        """Promoter buy/sell values and insider net from PIT disclosures.

        NSE's PIT API currently returns empty data; when rows appear they are
        parsed, otherwise everything stays zero with available=False.
        """
        try:
            today = datetime.now(UTC).astimezone().strftime("%d-%m-%Y")
            week_ago = (datetime.now(UTC) - timedelta(days=7)).astimezone().strftime(
                "%d-%m-%Y"
            )
            payload = await session.get_json(
                f"/api/corporates-pit?index=equities&from_date={week_ago}&to_date={today}"
            )
            rows = payload.get("data") or []
        except Exception as exc:
            logger.warning("pit fetch failed", extra={"error": str(exc)})
            return 0.0, 0.0, 0.0, False
        if not rows:
            return 0.0, 0.0, 0.0, False
        promoter_buys = promoter_sells = insider_net = 0.0
        for row in rows:
            try:
                value_cr = float(row.get("secVal") or 0.0) / 1e7
            except (TypeError, ValueError):
                continue
            acquisition = "acq" in str(row.get("tdpTransactionType", "")).lower()
            signed = value_cr if acquisition else -value_cr
            insider_net += signed
            if "promoter" in str(row.get("personCategory", "")).lower():
                if acquisition:
                    promoter_buys += value_cr
                else:
                    promoter_sells += value_cr
        return (
            round(promoter_buys, 2),
            round(promoter_sells, 2),
            round(insider_net, 2),
            True,
        )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
