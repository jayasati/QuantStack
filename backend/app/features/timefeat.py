"""Time Feature Engine (Volume 3, Prompt 3.12).

Calendar and session-clock context for the Indian market, on two passes:

Daily pass (symbol MARKET, timeframe "D", one row per benchmark trading day):
- Day of week (0 = Monday), month, quarter.
- Weekly expiry flag (the configured expiry weekday, Tuesday for NSE index
  derivatives since Sep 2025), monthly expiry flag (last expiry weekday of
  the month), and expiry week (any day in the Monday..expiry stretch of a
  monthly expiry week).
- Holiday distance: calendar days to the next configured NSE holiday.
- Budget window: within +/- feature_budget_window_days of Feb 1.
- Earnings season: mid-Jan..mid-Feb, mid-Apr..mid-May, mid-Jul..mid-Aug,
  mid-Oct..mid-Nov (Indian quarterly result windows).

Clock pass (symbol MARKET, timeframe "clock", one row per engine run):
- Market open minutes (session length, 375 for NSE), minutes since open and
  minutes until close (0 when the market is closed), and an is-open flag.
  These give live models the session clock at inference time.

Calendar flags are stored raw; z-score companions are intentionally omitted
here — a z-scored weekday is noise, and Prompt 3.13's normalization engine
serves consumers that need scaled variants.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.logging import get_logger
from app.features.base import BaseFeatureEngine
from app.features.schema import Candle, FeatureDefinition, Series

logger = get_logger(__name__)

ENGINE_NAME = "time_feature_engine"
ENGINE_VERSION = "v1"
CATEGORY = "time"

MARKET_SYMBOL = "MARKET"
CLOCK_TIMEFRAME = "clock"

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)
SESSION_MINUTES = 375.0

# Mid-month earnings windows per quarter: (start month, start day, end month, end day).
EARNINGS_WINDOWS = (
    (1, 10, 2, 15),
    (4, 10, 5, 15),
    (7, 10, 8, 15),
    (10, 10, 11, 15),
)


# --- Feature definitions -------------------------------------------------------

def time_feature_definitions(calculation_frequency: str = "on_schedule") -> list[FeatureDefinition]:
    def define(name: str, description: str, unit: str,
               expected: tuple[float | None, float | None],
               ) -> FeatureDefinition:
        return FeatureDefinition(
            feature_name=name,
            category=CATEGORY,
            description=description,
            version=ENGINE_VERSION,
            calculation_frequency=calculation_frequency,
            owner=ENGINE_NAME,
            unit=unit,
            expected_range=expected,
        )

    return [
        define("time_day_of_week", "0 = Monday .. 6 = Sunday.", "index", (0.0, 6.0)),
        define("time_month", "Calendar month, 1..12.", "index", (1.0, 12.0)),
        define("time_quarter", "Calendar quarter, 1..4.", "index", (1.0, 4.0)),
        define("time_expiry_week",
               "1 during the week leading into a monthly expiry.",
               "flag", (0.0, 1.0)),
        define("time_monthly_expiry_flag",
               "1 on the monthly expiry day (last expiry weekday of the month).",
               "flag", (0.0, 1.0)),
        define("time_weekly_expiry_flag",
               "1 on the weekly expiry weekday.",
               "flag", (0.0, 1.0)),
        define("time_holiday_distance",
               "Calendar days until the next configured market holiday (can "
               "exceed a year for history before the configured calendar).",
               "days", (0.0, None)),
        define("time_budget_window",
               "1 within the configured window around the Feb 1 Union Budget.",
               "flag", (0.0, 1.0)),
        define("time_earnings_season_flag",
               "1 inside the quarterly results windows.",
               "flag", (0.0, 1.0)),
        define("time_market_open_minutes",
               "Session length in minutes (clock pass).",
               "minutes", (0.0, 600.0)),
        define("time_since_open",
               "Minutes since the session open, 0 when closed (clock pass).",
               "minutes", (0.0, 600.0)),
        define("time_until_close",
               "Minutes until the session close, 0 when closed (clock pass).",
               "minutes", (0.0, 600.0)),
        define("time_market_is_open", "1 during market hours (clock pass).",
               "flag", (0.0, 1.0)),
    ]


# --- Pure calculations -----------------------------------------------------------

def monthly_expiry_date(year: int, month: int, expiry_weekday: int) -> date:
    """Last occurrence of the expiry weekday in the month."""
    if month == 12:
        last_day = date(year, 12, 31)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    offset = (last_day.weekday() - expiry_weekday) % 7
    return last_day - timedelta(days=offset)


def _in_earnings_season(day: date) -> bool:
    for start_month, start_day, end_month, end_day in EARNINGS_WINDOWS:
        if date(day.year, start_month, start_day) <= day <= date(day.year, end_month, end_day):
            return True
    return False


def compute_time_features(
    timestamps: Sequence[datetime],
    expiry_weekday: int = 1,
    holidays: Sequence[date] = (),
    budget_window_days: int = 5,
) -> dict[str, Series]:
    """Calendar features per trading-day timestamp (session dates in IST)."""
    n = len(timestamps)
    sorted_holidays = sorted(holidays)

    day_of_week: Series = [None] * n
    month: Series = [None] * n
    quarter: Series = [None] * n
    expiry_week: Series = [None] * n
    monthly_expiry: Series = [None] * n
    weekly_expiry: Series = [None] * n
    holiday_distance: Series = [None] * n
    budget_window: Series = [None] * n
    earnings_season: Series = [None] * n

    for i, ts in enumerate(timestamps):
        day = ts.astimezone(IST).date()
        day_of_week[i] = float(day.weekday())
        month[i] = float(day.month)
        quarter[i] = float((day.month - 1) // 3 + 1)

        weekly_expiry[i] = 1.0 if day.weekday() == expiry_weekday else 0.0
        expiry = monthly_expiry_date(day.year, day.month, expiry_weekday)
        monthly_expiry[i] = 1.0 if day == expiry else 0.0
        week_start = expiry - timedelta(days=expiry.weekday())
        expiry_week[i] = 1.0 if week_start <= day <= expiry else 0.0

        upcoming = [h for h in sorted_holidays if h >= day]
        if upcoming:
            holiday_distance[i] = float((upcoming[0] - day).days)

        budget_day = date(day.year, 2, 1)
        budget_window[i] = (
            1.0 if abs((day - budget_day).days) <= budget_window_days else 0.0
        )
        earnings_season[i] = 1.0 if _in_earnings_season(day) else 0.0

    return {
        "time_day_of_week": day_of_week,
        "time_month": month,
        "time_quarter": quarter,
        "time_expiry_week": expiry_week,
        "time_monthly_expiry_flag": monthly_expiry,
        "time_weekly_expiry_flag": weekly_expiry,
        "time_holiday_distance": holiday_distance,
        "time_budget_window": budget_window,
        "time_earnings_season_flag": earnings_season,
    }


def compute_clock_features(now: datetime) -> dict[str, float]:
    """Session-clock features for one instant."""
    local = now.astimezone(IST)
    open_dt = local.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1],
                            second=0, microsecond=0)
    close_dt = local.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1],
                             second=0, microsecond=0)
    is_open = local.weekday() < 5 and open_dt <= local <= close_dt
    since_open = (local - open_dt).total_seconds() / 60 if is_open else 0.0
    until_close = (close_dt - local).total_seconds() / 60 if is_open else 0.0
    return {
        "time_market_open_minutes": SESSION_MINUTES,
        "time_since_open": since_open,
        "time_until_close": until_close,
        "time_market_is_open": 1.0 if is_open else 0.0,
    }


# --- Engine -------------------------------------------------------------------------

class TimeFeatureEngine(BaseFeatureEngine):
    name = ENGINE_NAME
    category = CATEGORY

    def _definitions(self) -> list[FeatureDefinition]:
        return time_feature_definitions(
            calculation_frequency=f"{self._settings.feature_engine_interval}s"
        )

    def _compute(
        self, candles: Sequence[Candle], benchmark: Sequence[Candle] | None = None
    ) -> dict[str, Series]:
        return {}  # run() drives both passes directly

    def _holidays(self) -> list[date]:
        holidays: list[date] = []
        for raw in self._settings.feature_market_holidays:
            try:
                holidays.append(date.fromisoformat(raw))
            except ValueError:
                logger.warning("ignoring malformed holiday date", extra={"value": raw})
        return holidays

    async def run(
        self, symbol: str = MARKET_SYMBOL, timeframe: str = "D", full: bool = False
    ) -> dict:
        """Time features are market-wide: symbol argument is ignored."""
        benchmark = await self._load_candles(
            self._settings.feature_benchmark_symbol, "D"
        )
        summary: dict = {"symbol": MARKET_SYMBOL, "timeframe": "D", "stored": 0}
        if len(benchmark) >= 2:
            timestamps = [c.ts for c in benchmark]
            series = compute_time_features(
                timestamps,
                self._settings.feature_expiry_weekday,
                self._holidays(),
                self._settings.feature_budget_window_days,
            )
            summary = await self._process_series(
                MARKET_SYMBOL, "D", timestamps, series, full=full
            )

        now = datetime.now(UTC).replace(second=0, microsecond=0)
        clock_values = compute_clock_features(now)
        clock_series: dict[str, Series] = {
            name: [value] for name, value in clock_values.items()
        }
        summary["clock_pass"] = await self._process_series(
            MARKET_SYMBOL, CLOCK_TIMEFRAME, [now], clock_series, full=full
        )
        return summary

    async def run_all(self) -> list[dict]:
        try:
            return [await self.run()]
        except Exception as exc:
            logger.error("time feature run failed", extra={"error": str(exc)})
            return [{"symbol": MARKET_SYMBOL, "error": str(exc)}]
