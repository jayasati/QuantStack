"""Offline tests for the NSE event calendar source."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.collectors.sources.nse_events import (
    load_scheduled_events,
    map_board_meetings,
    map_corporate_actions,
    map_ipos,
)

IST = ZoneInfo("Asia/Kolkata")


def test_corporate_actions_mapping() -> None:
    rows = [
        {"symbol": "TCS", "subject": "Dividend - Rs 10 Per Share", "exDate": "10-Jul-2026"},
        {"symbol": "ABC", "subject": "Bonus 1:1", "exDate": "12-Jul-2026"},
        {"symbol": "XYZ", "subject": "Face Value Split From Rs 10 to Rs 1",
         "exDate": "15-Jul-2026"},
        {"symbol": "SKIP", "subject": "Annual General Meeting", "exDate": "16-Jul-2026"},
        {"symbol": "BADDATE", "subject": "Dividend", "exDate": "-"},
    ]
    events = map_corporate_actions(rows)
    kinds = [e["kind"] for e in events]
    assert kinds == ["DIVIDEND", "BONUS", "SPLIT"]
    assert events[0]["instrument"] == "TCS"
    assert events[0]["scheduled_at"].startswith("2026-07-10T09:15")


def test_board_meetings_results_only() -> None:
    rows = [
        {"symbol": "INFY", "purpose": "Financial Results", "date": "14-Jul-2026"},
        {"symbol": "HDFC", "purpose": "Financial Results/Dividend", "date": "18-Jul-2026"},
        {"symbol": "SKIP", "purpose": "Fund Raising", "date": "14-Jul-2026"},
    ]
    events = map_board_meetings(rows)
    assert len(events) == 2
    assert all(e["kind"] == "RESULTS" for e in events)


def test_ipo_mapping_uses_end_date_when_started() -> None:
    rows = [
        {"companyName": "NewCo", "symbol": "NEWCO",
         "issueStartDate": "01-Jan-2020", "issueEndDate": "31-Dec-2099"},
    ]
    events = map_ipos(rows)
    assert len(events) == 1
    assert events[0]["kind"] == "IPO"
    assert events[0]["scheduled_at"].startswith("2099-12-31")


def test_scheduled_events_recurring_rules(tmp_path: Path) -> None:
    schedule = tmp_path / "cal.yaml"
    schedule.write_text(
        """
events:
  - name: RBI MPC Decision
    kind: RBI
    scheduled_at: "2026-08-06T10:00:00+05:30"
    country: IN
recurring:
  - name: India CPI Release
    kind: INDIA_CPI
    day_of_month: 12
    time: "17:30"
    country: IN
  - name: Union Budget
    kind: BUDGET
    month: 2
    day_of_month: 1
    time: "11:00"
    country: IN
""",
        encoding="utf-8",
    )
    now = datetime(2026, 7, 6, 10, 0, tzinfo=IST)
    events = load_scheduled_events(schedule, now=now)
    by_kind = {e["kind"]: e for e in events}
    assert by_kind["RBI"]["scheduled_at"] == "2026-08-06T10:00:00+05:30"
    # Next CPI: 12-Jul-2026 17:30 IST
    assert by_kind["INDIA_CPI"]["scheduled_at"].startswith("2026-07-12T17:30")
    # Next budget: 01-Feb-2027
    assert by_kind["BUDGET"]["scheduled_at"].startswith("2027-02-01T11:00")


def test_missing_schedule_file_is_empty(tmp_path: Path) -> None:
    assert load_scheduled_events(tmp_path / "missing.yaml") == []
