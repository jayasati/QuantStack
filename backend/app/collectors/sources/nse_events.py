"""NSE + scheduled event calendar source (real feed for Prompt 2.9).

Real feeds:
- Corporate actions (DIVIDEND / BONUS / SPLIT ex-dates): NSE corporateActions
- Financial results (RESULTS board meetings): NSE event-calendar
- IPO issues: NSE ipo-current-issue
- F&O expiries (FNO_EXPIRY): NSE option-chain contract-info per index

Scheduled macro events (RBI, Fed, ECB, BoJ, CPI, GDP, Budget, elections,
MSCI/FTSE rebalances) have no public API. They load from
``configs/event_calendar.yaml`` — static entries maintained by hand from
official calendars, plus recurring monthly/annual rules. The loader is code;
the truth stays in the file, with its source documented there.
"""

import re
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from app.collectors.base import CollectionError
from app.collectors.domains.events import EventCalendarSource
from app.core.config import REPO_ROOT
from app.core.logging import get_logger

logger = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")
SCHEDULE_FILE = REPO_ROOT / "configs" / "event_calendar.yaml"
CACHE_TTL_SECONDS = 900
WINDOW_DAYS = 30
EXPIRY_INDICES = ("NIFTY", "BANKNIFTY")

_SUBJECT_KINDS = (
    (re.compile(r"bonus", re.I), "BONUS"),
    (re.compile(r"split|face\s*value", re.I), "SPLIT"),
    (re.compile(r"dividend", re.I), "DIVIDEND"),
)


def _nse_date(value: str, hour: int = 9, minute: int = 15) -> datetime | None:
    """Parse NSE's dd-Mon-yyyy into an aware IST datetime."""
    try:
        parsed = datetime.strptime(value.strip(), "%d-%b-%Y")
    except (ValueError, AttributeError):
        return None
    return parsed.replace(hour=hour, minute=minute, tzinfo=IST)


def map_corporate_actions(rows: list[dict]) -> list[dict[str, Any]]:
    """Ex-date corporate actions -> DIVIDEND/BONUS/SPLIT events."""
    events: list[dict[str, Any]] = []
    for row in rows or []:
        subject = str(row.get("subject") or "")
        kind = next((k for pattern, k in _SUBJECT_KINDS if pattern.search(subject)), None)
        if kind is None:
            continue
        scheduled = _nse_date(str(row.get("exDate") or ""))
        if scheduled is None:
            continue
        events.append(
            {
                "name": f"{row.get('symbol')}: {subject}"[:120],
                "kind": kind,
                "scheduled_at": scheduled.isoformat(),
                "country": "IN",
                "instrument": row.get("symbol"),
            }
        )
    return events


def map_board_meetings(rows: list[dict]) -> list[dict[str, Any]]:
    """Board meetings considering financial results -> RESULTS events."""
    events: list[dict[str, Any]] = []
    for row in rows or []:
        purpose = str(row.get("purpose") or "")
        if "financial results" not in purpose.lower():
            continue
        scheduled = _nse_date(str(row.get("date") or ""))
        if scheduled is None:
            continue
        events.append(
            {
                "name": f"{row.get('symbol')}: {purpose}"[:120],
                "kind": "RESULTS",
                "scheduled_at": scheduled.isoformat(),
                "country": "IN",
                "instrument": row.get("symbol"),
            }
        )
    return events


def map_ipos(rows: list[dict]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    now = datetime.now(IST)
    for row in rows or []:
        start = _nse_date(str(row.get("issueStartDate") or ""))
        end = _nse_date(str(row.get("issueEndDate") or ""), hour=17, minute=0)
        scheduled = start if start and start >= now else end
        if scheduled is None:
            continue
        events.append(
            {
                "name": f"IPO: {row.get('companyName')}"[:120],
                "kind": "IPO",
                "scheduled_at": scheduled.isoformat(),
                "country": "IN",
                "instrument": row.get("symbol"),
            }
        )
    return events


def load_scheduled_events(path=SCHEDULE_FILE, now: datetime | None = None) -> list[dict]:
    """Static + recurring macro events from the maintained schedule file."""
    if not path.exists():
        return []
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("event schedule file unreadable", extra={"error": str(exc)})
        return []
    current = now or datetime.now(IST)
    events: list[dict[str, Any]] = list(config.get("events") or [])

    for rule in config.get("recurring") or []:
        day = int(rule.get("day_of_month", 1))
        month = rule.get("month")  # None = monthly
        hour, minute = (int(x) for x in str(rule.get("time", "09:00")).split(":"))
        candidates = []
        for offset in range(0, 13):
            year = current.year + (current.month + offset - 1) // 12
            candidate_month = (current.month + offset - 1) % 12 + 1
            if month is not None and candidate_month != int(month):
                continue
            try:
                candidates.append(
                    datetime(year, candidate_month, day, hour, minute, tzinfo=IST)
                )
            except ValueError:
                continue
        upcoming = next((c for c in sorted(candidates) if c >= current), None)
        if upcoming is None:
            continue
        events.append(
            {
                "name": rule.get("name", rule.get("kind", "scheduled event")),
                "kind": rule.get("kind"),
                "scheduled_at": upcoming.isoformat(),
                "country": rule.get("country", "IN"),
            }
        )
    return events


class NseEventCalendarSource(EventCalendarSource):
    def __init__(self, session: Any = None, schedule_path=SCHEDULE_FILE) -> None:
        self._session = session
        self._schedule_path = schedule_path
        self._cache: tuple[float, list[dict]] | None = None

    def _get_session(self):
        if self._session is None:
            from app.collectors.sources.nse_client import NseSession

            self._session = NseSession(warmup_path="/market-data/live-equity-market")
        return self._session

    async def fetch_events(self) -> list[dict[str, Any]]:
        now = time.time()
        if self._cache is not None and now - self._cache[0] < CACHE_TTL_SECONDS:
            return self._cache[1]
        session = self._get_session()
        today = datetime.now(IST)
        from_date = today.strftime("%d-%m-%Y")
        to_date = (today + timedelta(days=WINDOW_DAYS)).strftime("%d-%m-%Y")

        events: list[dict[str, Any]] = []
        sources_ok = 0

        for label, fetcher in (
            (
                "corporate_actions",
                lambda: session.get_json(
                    f"/api/corporates-corporateActions?index=equities"
                    f"&from_date={from_date}&to_date={to_date}"
                ),
            ),
            (
                "board_meetings",
                lambda: session.get_json(
                    f"/api/event-calendar?index=equities"
                    f"&from_date={from_date}&to_date={to_date}"
                ),
            ),
            ("ipos", lambda: session.get_json("/api/ipo-current-issue")),
        ):
            try:
                rows = await fetcher()
                mapper = {
                    "corporate_actions": map_corporate_actions,
                    "board_meetings": map_board_meetings,
                    "ipos": map_ipos,
                }[label]
                events.extend(mapper(rows if isinstance(rows, list) else []))
                sources_ok += 1
            except Exception as exc:
                logger.warning(
                    "event feed failed", extra={"feed": label, "error": str(exc)}
                )

        events.extend(await self._expiry_events(session))
        events.extend(load_scheduled_events(self._schedule_path))

        if sources_ok == 0 and not events:
            raise CollectionError("no event calendar feeds available")
        self._cache = (now, events)
        return events

    async def _expiry_events(self, session: Any) -> list[dict[str, Any]]:
        """F&O expiries from the option-chain contract info (real NSE data)."""
        events: list[dict[str, Any]] = []
        horizon = datetime.now(IST) + timedelta(days=WINDOW_DAYS)
        for index in EXPIRY_INDICES:
            try:
                info = await session.get_json(
                    f"/api/option-chain-contract-info?symbol={index}"
                )
                for expiry in info.get("expiryDates") or []:
                    scheduled = _nse_date(expiry, hour=15, minute=30)
                    if scheduled is None or scheduled > horizon:
                        continue
                    events.append(
                        {
                            "name": f"{index} F&O expiry {expiry}",
                            "kind": "FNO_EXPIRY",
                            "scheduled_at": scheduled.isoformat(),
                            "country": "IN",
                            "instrument": index,
                        }
                    )
            except Exception as exc:
                logger.warning(
                    "expiry fetch failed", extra={"index": index, "error": str(exc)}
                )
        return events

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
