import math
from datetime import datetime, timedelta
from statistics import pstdev
from zoneinfo import ZoneInfo

import pytest

from app.core.config import Settings
from app.features.intraday_risk import (
    MIN_RETURNS_FOR_VOL,
    SESSION_MINUTES,
    TRADING_DAYS,
    VAR_Z_95,
    IntradayRiskFeatureEngine,
    compute_intraday_risk_features,
    timeframe_minutes,
)
from app.features.schema import Candle

IST = ZoneInfo("Asia/Kolkata")
BAR_MINUTES = 5
HORIZON_MINUTES = 30
BARS_PER_SESSION = round(SESSION_MINUTES / BAR_MINUTES)
HORIZON_BARS = round(HORIZON_MINUTES / BAR_MINUTES)

MOVE_NAME = f"intraday_expected_move_next_{HORIZON_MINUTES}m_pct"
VAR_NAME = f"intraday_var95_next_{HORIZON_MINUTES}m_pct"


def val(x: float | None) -> float:
    assert x is not None
    return x


def make_session(closes: list[float], day_offset: int = 0, start_hour: int = 9,
                  start_minute: int = 15) -> list[Candle]:
    base = datetime(2026, 7, 6, start_hour, start_minute, tzinfo=IST) + timedelta(days=day_offset)
    candles = []
    for i, close in enumerate(closes):
        ts = base + timedelta(minutes=BAR_MINUTES * i)
        open_ = closes[i - 1] if i > 0 else close
        candles.append(
            Candle(ts=ts, open=open_, high=max(open_, close) + 0.1,
                   low=min(open_, close) - 0.1, close=close, volume=100)
        )
    return candles


def compute(closes: list[float], day_offset: int = 0):
    return compute_intraday_risk_features(
        make_session(closes, day_offset=day_offset),
        bar_minutes=BAR_MINUTES,
        horizon_minutes=HORIZON_MINUTES,
        normalization_window=50,
    )


def test_timeframe_minutes_parses_minute_and_hour_suffixes() -> None:
    assert timeframe_minutes("5m") == 5
    assert timeframe_minutes("15m") == 15
    assert timeframe_minutes("1H") == 60
    with pytest.raises(ValueError):
        timeframe_minutes("1D")


def test_time_elapsed_and_move_from_open_manual() -> None:
    closes = [100.0, 100.5, 100.2, 100.8, 100.3]
    _, series = compute(closes)
    assert series["intraday_time_elapsed_pct"][0] == pytest.approx(1 / BARS_PER_SESSION)
    assert series["intraday_time_elapsed_pct"][4] == pytest.approx(5 / BARS_PER_SESSION)
    assert series["intraday_move_from_open_pct"][0] == pytest.approx(0.0)
    assert series["intraday_move_from_open_pct"][4] == pytest.approx((100.3 / 100.0 - 1) * 100)


def test_current_and_max_drawdown_manual() -> None:
    # Rises to a peak at bar 2, then declines for the rest of the session.
    closes = [100.0, 105.0, 110.0, 108.0, 104.0, 99.0]
    _, series = compute(closes)
    last = len(closes) - 1
    peak = 110.0
    expected_current_dd = (peak - closes[last]) / peak * 100
    assert series["intraday_current_drawdown_pct"][2] == pytest.approx(0.0)  # bar AT the peak
    assert series["intraday_current_drawdown_pct"][last] == pytest.approx(expected_current_dd)
    # Monotonic decline after the peak -> max drawdown equals the final current drawdown.
    assert series["intraday_max_drawdown_pct"][last] == pytest.approx(expected_current_dd)


def test_vol_and_expected_move_cold_start_then_manual() -> None:
    closes = [100.0, 100.5, 100.2, 100.8, 100.3, 100.9, 100.4]
    _, series = compute(closes)

    # Fewer than MIN_RETURNS_FOR_VOL returns collected -> still None.
    for i in range(MIN_RETURNS_FOR_VOL):
        assert series["intraday_realized_vol_pct"][i] is None
        assert series[MOVE_NAME][i] is None
        assert series[VAR_NAME][i] is None

    i = MIN_RETURNS_FOR_VOL  # first bar with exactly MIN_RETURNS_FOR_VOL returns behind it
    rets = [closes[j] / closes[j - 1] - 1 for j in range(1, i + 1)]
    std = pstdev(rets)
    expected_vol = std * math.sqrt(TRADING_DAYS * BARS_PER_SESSION) * 100
    expected_move = std * math.sqrt(HORIZON_BARS) * 100
    expected_var = std * math.sqrt(HORIZON_BARS) * VAR_Z_95 * 100

    assert series["intraday_realized_vol_pct"][i] == pytest.approx(expected_vol)
    assert series[MOVE_NAME][i] == pytest.approx(expected_move)
    assert series[VAR_NAME][i] == pytest.approx(expected_var)
    # VaR is exactly the 1-sigma expected move scaled by the 95% z-score.
    assert val(series[VAR_NAME][i]) == pytest.approx(val(series[MOVE_NAME][i]) * VAR_Z_95)


def test_rest_of_session_horizon_shrinks_to_zero_at_session_close() -> None:
    # A stable alternating step keeps realized vol roughly constant across the
    # session, isolating the bars_remaining -> 0 effect from vol noise: the
    # scaling factor (sqrt(bars_remaining)) itself must shrink to exactly 0
    # by the last bar of the session, regardless of what vol was measured.
    closes = [100.0]
    for i in range(BARS_PER_SESSION - 1):
        step = 0.001 if i % 2 == 0 else -0.001
        closes.append(closes[-1] * (1 + step))
    _, series = compute(closes)
    values = series["intraday_expected_move_rest_of_session_pct"]
    first_populated = next(v for v in values if v is not None)
    assert first_populated > 0
    assert values[-1] == pytest.approx(0.0)  # zero bars remain at the session's last bar


def test_rest_of_session_uses_bars_remaining_manual() -> None:
    closes = [100.0, 100.5, 100.2, 100.8, 100.3, 100.9]
    _, series = compute(closes)
    i = 5
    rets = [closes[j] / closes[j - 1] - 1 for j in range(1, i + 1)]
    std = pstdev(rets)
    bars_remaining = BARS_PER_SESSION - (i + 1)
    expected = std * math.sqrt(bars_remaining) * 100
    assert series["intraday_expected_move_rest_of_session_pct"][i] == pytest.approx(expected)


def test_sessions_are_independent_no_cross_day_leakage() -> None:
    day1 = make_session([100.0, 90.0, 80.0, 70.0, 60.0], day_offset=0)  # steep decline
    day2 = make_session([50.0, 50.1, 50.2, 50.3, 50.4], day_offset=1)  # calm, different level
    _, series = compute_intraday_risk_features(
        [*day1, *day2], bar_minutes=BAR_MINUTES, horizon_minutes=HORIZON_MINUTES,
        normalization_window=50,
    )
    # Day 2's first bar: fresh peak at day 2's own open, not day 1's much higher peak.
    day2_start = len(day1)
    assert series["intraday_current_drawdown_pct"][day2_start] == pytest.approx(0.0)
    assert series["intraday_max_drawdown_pct"][day2_start] == pytest.approx(0.0)
    assert series["intraday_time_elapsed_pct"][day2_start] == pytest.approx(1 / BARS_PER_SESSION)
    # Vol resets to cold-start None at the start of day 2 too.
    assert series["intraday_realized_vol_pct"][day2_start] is None


def test_every_feature_has_z_companion() -> None:
    closes = [100.0 + 0.2 * (i % 4) for i in range(15)]
    _, series = compute(closes)
    raw_names = [name for name in series if not name.endswith("_z")]
    assert len(raw_names) == 9
    for name in raw_names:
        assert f"{name}_z" in series


def test_engine_registration_and_dependency_order() -> None:
    engine = IntradayRiskFeatureEngine(settings=Settings())
    definitions = engine.registry.list_definitions(category="intraday_risk")
    assert len(definitions) == 9 * 2  # 9 raw + 9 _z companions, single "timeframe" (no windows)
    assert all(d.version == "v1" for d in definitions)
    order = engine.registry.dependency_order()
    assert order.index("intraday_current_drawdown_pct") < order.index("intraday_max_drawdown_pct")
    assert order.index(MOVE_NAME) < order.index(VAR_NAME)


async def test_run_uses_intraday_timeframe_not_daily() -> None:
    engine = IntradayRiskFeatureEngine(settings=Settings())
    result = await engine.run("NIFTY")  # no session_factory -> _load_candles returns []
    assert result["timeframe"] == engine._settings.feature_intraday_timeframe
    assert result["skipped"] is True
