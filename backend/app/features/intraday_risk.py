"""Intraday Risk Feature Engine (F&O same-day-trading gap fill, 2026-07-09).

RiskFeatureEngine (app/features/risk.py) computes annualized, daily-close-
based risk metrics -- built for evaluating a strategy over weeks/months, not
for a same-day F&O trader deciding whether to hold a position for the next
30 minutes or into the close. This engine fills that gap: one value PER
INTRADAY BAR (default 5m, Settings.feature_intraday_timeframe), computed
from a session-boundary-aware EXPANDING window that resets at every new
trading session -- unlike every other engine's fixed trailing-N-bar window,
today's 9:20am realized vol must never blend in yesterday's close.

Feature set (all session-relative, i.e. "as of right now, today"):
- Time Elapsed / Move From Open: context for interpreting everything else.
- Current / Max Drawdown: this session's decline from its own running high.
- Realized Vol: annualized (comparable scale to RiskFeatureEngine/Volatility
  Feature Engine), from the session so far.
- Expected Move / VaR 95%, at TWO horizons per Prompt discussion:
  - next_30m: a fixed tactical window, for "should I act right now" checks.
  - rest_of_session: shrinks as the day progresses, for same-day square-off
    framing ("what's still at risk between now and the close").

VaR/expected-move here use a PARAMETRIC (Gaussian sqrt-of-time) scaling of
the session-so-far realized volatility, not RiskFeatureEngine's empirical
quantiles: early in a session there are too few observations (as few as 2-3
bars) for an empirical quantile to mean anything, whereas realized-vol-times-
sqrt(horizon) is exactly how intraday risk desks and option market-makers
estimate short-horizon expected moves in practice.
"""

import math
from collections.abc import Sequence
from datetime import date, datetime
from statistics import pstdev
from zoneinfo import ZoneInfo

from app.core.logging import get_logger
from app.features.base import BaseFeatureEngine
from app.features.normalize import add_normalized_series, normalized_definition
from app.features.schema import Candle, FeatureDefinition, Series

logger = get_logger(__name__)

ENGINE_NAME = "intraday_risk_feature_engine"
ENGINE_VERSION = "v1"
CATEGORY = "intraday_risk"

IST = ZoneInfo("Asia/Kolkata")

TRADING_DAYS = 252
SESSION_MINUTES = 375  # NSE cash/derivatives trading window, 09:15-15:30 IST
DEFAULT_HORIZON_MINUTES = 30
VAR_Z_95 = 1.6448536269514722  # one-tailed 95% Gaussian z-score
MIN_CANDLES = 3  # need at least this many bars loaded before attempting anything
MIN_RETURNS_FOR_VOL = 3  # cold start within a session until this many returns exist

FEATURE_NAMES = (
    "intraday_time_elapsed_pct",
    "intraday_move_from_open_pct",
    "intraday_realized_vol_pct",
    "intraday_current_drawdown_pct",
    "intraday_max_drawdown_pct",
    "intraday_expected_move_next_30m_pct",
    "intraday_var95_next_30m_pct",
    "intraday_expected_move_rest_of_session_pct",
    "intraday_var95_rest_of_session_pct",
)


def timeframe_minutes(timeframe: str) -> int:
    """Parse a candle timeframe string ("5m", "15m", "1H") into minutes."""
    if timeframe.endswith("m"):
        return int(timeframe[:-1])
    if timeframe.endswith("H"):
        return int(timeframe[:-1]) * 60
    raise ValueError(f"unsupported intraday timeframe: {timeframe!r}")


# --- Feature definitions -------------------------------------------------------

def intraday_risk_feature_definitions(
    normalization_window: int,
    horizon_minutes: int = DEFAULT_HORIZON_MINUTES,
    calculation_frequency: str = "on_schedule",
) -> list[FeatureDefinition]:
    def define(name: str, description: str, unit: str,
               expected: tuple[float | None, float | None],
               dependencies: tuple[str, ...] = (),
               ) -> FeatureDefinition:
        return FeatureDefinition(
            feature_name=name,
            category=CATEGORY,
            description=description,
            version=ENGINE_VERSION,
            dependencies=dependencies,
            calculation_frequency=calculation_frequency,
            owner=ENGINE_NAME,
            unit=unit,
            expected_range=expected,
        )

    definitions = [
        define("intraday_time_elapsed_pct",
               "Fraction of today's trading session elapsed (0 at open, 1 at close).",
               "ratio", (0.0, 1.0)),
        define("intraday_move_from_open_pct",
               "Signed close vs today's session open, in %.",
               "%", (-25.0, 25.0)),
        define("intraday_realized_vol_pct",
               "Annualized realized volatility of the session-so-far, in %.",
               "%", (0.0, 200.0)),
        define("intraday_current_drawdown_pct",
               "Decline from today's session-high-so-far to the current close, in %.",
               "%", (0.0, 25.0)),
        define("intraday_max_drawdown_pct",
               "Worst intraday_current_drawdown_pct seen so far this session, in %.",
               "%", (0.0, 25.0), ("intraday_current_drawdown_pct",)),
        define(f"intraday_expected_move_next_{horizon_minutes}m_pct",
               f"1-sigma expected move over the next {horizon_minutes} minutes, in %.",
               "%", (0.0, 10.0)),
        define(f"intraday_var95_next_{horizon_minutes}m_pct",
               f"95% one-tailed VaR over the next {horizon_minutes} minutes, in %.",
               "%", (0.0, 15.0), (f"intraday_expected_move_next_{horizon_minutes}m_pct",)),
        define("intraday_expected_move_rest_of_session_pct",
               "1-sigma expected move between now and today's close, in % "
               "(shrinks toward 0 as the session ends).",
               "%", (0.0, 15.0)),
        define("intraday_var95_rest_of_session_pct",
               "95% one-tailed VaR between now and today's close, in % "
               "(shrinks toward 0 as the session ends).",
               "%", (0.0, 20.0), ("intraday_expected_move_rest_of_session_pct",)),
    ]
    definitions.extend(
        normalized_definition(d, normalization_window) for d in list(definitions)
    )
    return definitions


# --- Pure calculations -----------------------------------------------------------

def compute_intraday_risk_features(
    candles: Sequence[Candle],
    bar_minutes: int,
    horizon_minutes: int = DEFAULT_HORIZON_MINUTES,
    session_minutes: int = SESSION_MINUTES,
    normalization_window: int = 100,
) -> tuple[list[datetime], dict[str, Series]]:
    """One row per intraday bar, grouped and reset by IST trading session.

    ``candles`` may span many sessions (whatever _load_candles returned);
    each session's expanding window is independent of every other session's.
    """
    sessions: dict[date, list[Candle]] = {}
    for candle in candles:
        sessions.setdefault(candle.ts.astimezone(IST).date(), []).append(candle)

    bars_per_session = max(1, round(session_minutes / bar_minutes))
    horizon_bars = max(1, round(horizon_minutes / bar_minutes))
    horizon_name = f"intraday_expected_move_next_{horizon_minutes}m_pct"
    var_horizon_name = f"intraday_var95_next_{horizon_minutes}m_pct"

    timestamps: list[datetime] = []
    rows: list[dict[str, float | None]] = []

    for day in sorted(sessions):
        bars = sorted(sessions[day], key=lambda c: c.ts)
        if len(bars) < MIN_CANDLES:
            continue

        session_open = bars[0].open
        returns: list[float] = []
        peak = bars[0].close
        running_max_dd = 0.0

        for idx, bar in enumerate(bars):
            row: dict[str, float | None] = {}
            elapsed_bars = idx + 1

            row["intraday_time_elapsed_pct"] = min(1.0, elapsed_bars / bars_per_session)
            row["intraday_move_from_open_pct"] = (
                (bar.close / session_open - 1) * 100 if session_open > 0 else None
            )

            if idx > 0 and bars[idx - 1].close > 0:
                returns.append(bar.close / bars[idx - 1].close - 1)

            peak = max(peak, bar.close)
            current_dd = (peak - bar.close) / peak * 100 if peak > 0 else 0.0
            running_max_dd = max(running_max_dd, current_dd)
            row["intraday_current_drawdown_pct"] = current_dd
            row["intraday_max_drawdown_pct"] = running_max_dd

            if len(returns) >= MIN_RETURNS_FOR_VOL:
                std = pstdev(returns)
                row["intraday_realized_vol_pct"] = (
                    std * math.sqrt(TRADING_DAYS * bars_per_session) * 100
                )
                row[horizon_name] = std * math.sqrt(horizon_bars) * 100
                row[var_horizon_name] = std * math.sqrt(horizon_bars) * VAR_Z_95 * 100

                bars_remaining = max(0, bars_per_session - elapsed_bars)
                row["intraday_expected_move_rest_of_session_pct"] = (
                    std * math.sqrt(bars_remaining) * 100
                )
                row["intraday_var95_rest_of_session_pct"] = (
                    std * math.sqrt(bars_remaining) * VAR_Z_95 * 100
                )
            else:
                row["intraday_realized_vol_pct"] = None
                row[horizon_name] = None
                row[var_horizon_name] = None
                row["intraday_expected_move_rest_of_session_pct"] = None
                row["intraday_var95_rest_of_session_pct"] = None

            timestamps.append(bar.ts)
            rows.append(row)

    names = [
        "intraday_time_elapsed_pct", "intraday_move_from_open_pct",
        "intraday_realized_vol_pct", "intraday_current_drawdown_pct",
        "intraday_max_drawdown_pct", horizon_name, var_horizon_name,
        "intraday_expected_move_rest_of_session_pct",
        "intraday_var95_rest_of_session_pct",
    ]
    series: dict[str, Series] = {name: [row.get(name) for row in rows] for name in names}
    return timestamps, add_normalized_series(series, normalization_window)


# --- Engine -------------------------------------------------------------------------

class IntradayRiskFeatureEngine(BaseFeatureEngine):
    """Intraday-only: run()/run_all() are fully overridden since values are
    one-per-bar on the intraday timeframe, not the daily timeframe every
    other engine defaults to -- there is no meaningful daily pass here."""

    name = ENGINE_NAME
    category = CATEGORY

    def _definitions(self) -> list[FeatureDefinition]:
        return intraday_risk_feature_definitions(
            self._settings.feature_normalization_window,
            DEFAULT_HORIZON_MINUTES,
            calculation_frequency=f"{self._settings.feature_engine_interval}s",
        )

    def _compute(
        self, candles: Sequence[Candle], benchmark: Sequence[Candle] | None = None
    ) -> dict[str, Series]:
        _, series = compute_intraday_risk_features(
            candles,
            bar_minutes=timeframe_minutes(self._settings.feature_intraday_timeframe),
            horizon_minutes=DEFAULT_HORIZON_MINUTES,
            normalization_window=self._settings.feature_normalization_window,
        )
        return series

    async def run(
        self,
        symbol: str,
        timeframe: str | None = None,
        full: bool = False,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict:
        """`start`/`end`: data foundation audit 2026-07-17, historical
        regeneration item."""
        tf = timeframe or self._settings.feature_intraday_timeframe
        candles = await self._load_candles(symbol, tf, start=start, end=end)
        if start is not None or end is not None:
            full = True
        if len(candles) < MIN_CANDLES:
            logger.info(
                "intraday risk run skipped: not enough candles",
                extra={"engine": self.name, "symbol": symbol, "timeframe": tf},
            )
            return {"symbol": symbol, "timeframe": tf, "stored": 0, "skipped": True}
        timestamps, series = compute_intraday_risk_features(
            candles,
            bar_minutes=timeframe_minutes(tf),
            horizon_minutes=DEFAULT_HORIZON_MINUTES,
            normalization_window=self._settings.feature_normalization_window,
        )
        if not timestamps:
            return {"symbol": symbol, "timeframe": tf, "stored": 0, "skipped": True}
        return await self._process_series(symbol, tf, timestamps, series, full=full)

    async def run_all(
        self,
        full: bool = False,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict]:
        tf = self._settings.feature_intraday_timeframe
        results: list[dict] = []
        for symbol in self._settings.watchlist:
            try:
                results.append(await self.run(symbol, tf, full=full, start=start, end=end))
            except Exception as exc:
                logger.error(
                    "feature run failed",
                    extra={
                        "engine": self.name, "symbol": symbol,
                        "timeframe": tf, "error": str(exc),
                    },
                )
                results.append({"symbol": symbol, "timeframe": tf, "error": str(exc)})
        return results
