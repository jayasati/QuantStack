from datetime import UTC, date, datetime

from app.core.config import Settings
from app.features.timefeat import (
    IST,
    SESSION_MINUTES,
    TimeFeatureEngine,
    compute_clock_features,
    compute_time_features,
    monthly_expiry_date,
)


def ist_day(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=IST)


def test_monthly_expiry_is_last_expiry_weekday() -> None:
    # July 2026: Tuesdays are 7, 14, 21, 28 -> monthly expiry July 28.
    assert monthly_expiry_date(2026, 7, expiry_weekday=1) == date(2026, 7, 28)
    # Thursday convention: last Thursday of July 2026 is the 30th.
    assert monthly_expiry_date(2026, 7, expiry_weekday=3) == date(2026, 7, 30)


def test_calendar_basics_and_expiry_flags() -> None:
    timestamps = [ist_day(2026, 7, 7), ist_day(2026, 7, 28), ist_day(2026, 7, 29)]
    series = compute_time_features(timestamps, expiry_weekday=1)
    # July 7 2026 is a Tuesday in Q3.
    assert series["time_day_of_week"][0] == 1.0
    assert series["time_month"][0] == 7.0
    assert series["time_quarter"][0] == 3.0
    assert series["time_weekly_expiry_flag"][0] == 1.0
    assert series["time_monthly_expiry_flag"][0] == 0.0
    # July 28 is the monthly expiry; July 29 is not, and is past the window.
    assert series["time_monthly_expiry_flag"][1] == 1.0
    assert series["time_expiry_week"][1] == 1.0
    assert series["time_expiry_week"][2] == 0.0


def test_holiday_distance_and_budget_window() -> None:
    holidays = [date(2026, 8, 15), date(2026, 10, 2)]
    timestamps = [ist_day(2026, 8, 10), ist_day(2026, 1, 29), ist_day(2026, 6, 15)]
    series = compute_time_features(timestamps, holidays=holidays)
    assert series["time_holiday_distance"][0] == 5.0
    # Jan 29 is within 5 days of the Feb 1 budget.
    assert series["time_budget_window"][1] == 1.0
    assert series["time_budget_window"][2] == 0.0


def test_earnings_season_windows() -> None:
    series = compute_time_features(
        [ist_day(2026, 7, 15), ist_day(2026, 3, 15), ist_day(2026, 10, 20)]
    )
    assert series["time_earnings_season_flag"][0] == 1.0  # mid-July
    assert series["time_earnings_season_flag"][1] == 0.0  # March: quiet
    assert series["time_earnings_season_flag"][2] == 1.0  # late October


def test_clock_features_open_and_closed() -> None:
    mid_session = datetime(2026, 7, 7, 11, 15, tzinfo=IST)  # Tuesday 11:15 IST
    open_features = compute_clock_features(mid_session.astimezone(UTC))
    assert open_features["time_market_is_open"] == 1.0
    assert open_features["time_since_open"] == 120.0
    assert open_features["time_until_close"] == 255.0
    assert open_features["time_market_open_minutes"] == SESSION_MINUTES

    weekend = datetime(2026, 7, 5, 11, 15, tzinfo=IST)  # Sunday
    closed = compute_clock_features(weekend.astimezone(UTC))
    assert closed["time_market_is_open"] == 0.0
    assert closed["time_since_open"] == 0.0
    assert closed["time_until_close"] == 0.0


def test_engine_registration() -> None:
    engine = TimeFeatureEngine(settings=Settings())
    definitions = engine.registry.list_definitions(category="time")
    assert len(definitions) == 13
    assert all(d.version == "v1" for d in definitions)
    assert not any(d.feature_name.endswith("_z") for d in definitions)
