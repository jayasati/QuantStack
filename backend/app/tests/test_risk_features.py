import math
from datetime import UTC, datetime, timedelta
from statistics import fmean

import pytest

from app.core.config import Settings
from app.features.risk import TRADING_DAYS, RiskFeatureEngine, compute_risk_features
from app.features.schema import Candle

BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def val(x: float | None) -> float:
    assert x is not None
    return x


def make_candles(n: int = 60, start: float = 100.0, wild_first_half: bool = False) -> list[Candle]:
    """Deterministic series; optionally violent swings early and a calm tail
    (same shape as the volatility engine's fixture, for a comparable skew)."""
    candles = []
    close = start
    for i in range(n):
        if wild_first_half and i < n // 2:
            step = 8.0 if i % 2 == 0 else -7.0
        else:
            step = 0.5 if i % 2 == 0 else -0.2
        prev_close = close
        close = max(close + step, 1.0)
        open_ = prev_close
        high = max(open_, close) + 0.5
        low = min(open_, close) - 0.5
        candles.append(
            Candle(ts=BASE_TS + timedelta(days=i), open=open_, high=high,
                   low=low, close=close, volume=1000)
        )
    return candles


def manual_quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    n = len(ordered)
    position = q * (n - 1)
    lower = int(position)
    upper = min(lower + 1, n - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def simple_returns(candles: list[Candle]) -> list[float]:
    return [candles[i].close / candles[i - 1].close - 1 for i in range(1, len(candles))]


def test_var_and_cvar_manual() -> None:
    candles = make_candles(30, wild_first_half=True)
    series = compute_risk_features(candles, windows=(10,))
    i = 20
    rets = simple_returns(candles)[i - 10 : i]  # returns at bars i-9..i
    q05 = manual_quantile(rets, 0.05)
    q01 = manual_quantile(rets, 0.01)
    expected_var95 = max(0.0, -q05 * 100)
    expected_var99 = max(0.0, -q01 * 100)
    tail_losses = [r for r in rets if r <= q05]
    expected_cvar95 = max(0.0, -fmean(tail_losses) * 100) if tail_losses else None

    assert series["risk_var_95_10"][i] == pytest.approx(expected_var95)
    assert series["risk_var_99_10"][i] == pytest.approx(expected_var99)
    if expected_cvar95 is None:
        assert series["risk_cvar_95_10"][i] is None
    else:
        assert series["risk_cvar_95_10"][i] == pytest.approx(expected_cvar95)
    assert series["risk_var_95_10"][5] is None  # cold start


def test_var99_at_least_var95() -> None:
    """The 1% tail cutoff can never be a smaller loss than the 5% cutoff."""
    candles = make_candles(80, wild_first_half=True)
    series = compute_risk_features(candles, windows=(20,))
    for v95, v99 in zip(series["risk_var_95_20"], series["risk_var_99_20"], strict=True):
        if v95 is not None and v99 is not None:
            assert v99 >= v95 - 1e-9


def test_max_drawdown_and_current_drawdown_manual() -> None:
    # A leading pad bar (outside the 7-bar window) plus: rises to a peak,
    # then declines steadily to the end. compute_risk_features needs at
    # least one bar before the window starts (bar 0 has no return), so an
    # exactly-w-length candle list produces no output at index w-1.
    window_closes = [100.0, 105.0, 110.0, 108.0, 104.0, 99.0, 95.0]
    closes = [100.0, *window_closes]
    candles = [
        Candle(ts=BASE_TS + timedelta(days=i), open=c, high=c + 1, low=c - 1, close=c, volume=1)
        for i, c in enumerate(closes)
    ]
    series = compute_risk_features(candles, windows=(7,))
    i = 7
    peak = window_closes[2]  # 110.0, the running high within the window
    expected_max_dd = (peak - min(window_closes[2:])) / peak * 100
    expected_cur_dd = (peak - window_closes[-1]) / peak * 100
    assert series["risk_max_drawdown_7"][i] == pytest.approx(expected_max_dd)
    assert series["risk_current_drawdown_7"][i] == pytest.approx(expected_cur_dd)
    assert series["risk_max_drawdown_7"][i] == pytest.approx(series["risk_current_drawdown_7"][i])


def test_downside_deviation_zero_and_sortino_none_without_losses() -> None:
    # Strictly increasing closes: every return is positive, so the
    # target-downside-deviation (target=0) is exactly 0, and Sortino must
    # stay None rather than divide by zero.
    closes = [100.0 + i for i in range(15)]
    candles = [
        Candle(ts=BASE_TS + timedelta(days=i), open=c, high=c + 1, low=c - 1, close=c, volume=1)
        for i, c in enumerate(closes)
    ]
    series = compute_risk_features(candles, windows=(10,))
    i = 12
    assert series["risk_downside_deviation_10"][i] == pytest.approx(0.0)
    assert series["risk_sortino_10"][i] is None
    assert val(series["risk_sharpe_10"][i]) > 0  # positive drift, positive Sharpe


def test_ulcer_index_manual() -> None:
    window_closes = [100.0, 90.0, 95.0, 80.0, 85.0]
    closes = [100.0, *window_closes]  # leading pad bar, see drawdown test above
    candles = [
        Candle(ts=BASE_TS + timedelta(days=i), open=c, high=c + 1, low=c - 1, close=c, volume=1)
        for i, c in enumerate(closes)
    ]
    series = compute_risk_features(candles, windows=(5,))
    i = 5
    peak = window_closes[0]
    drawdowns = []
    for c in window_closes:
        peak = max(peak, c)
        drawdowns.append((peak - c) / peak)
    expected = math.sqrt(fmean([d * d for d in drawdowns])) * 100
    assert series["risk_ulcer_index_5"][i] == pytest.approx(expected)


def build_from_returns(returns: list[float], start: float = 100.0) -> list[Candle]:
    closes = [start]
    for r in returns:
        closes.append(closes[-1] * (1 + r))
    return [
        Candle(ts=BASE_TS + timedelta(days=i), open=c, high=c + 1, low=c - 1, close=c, volume=1)
        for i, c in enumerate(closes)
    ]


def test_skew_sign_matches_which_tail_the_outlier_is_in() -> None:
    """One rare large negative return among small gains -> negative skew
    (crash-prone left tail); mirrored for a rare large positive return."""
    crash_series = compute_risk_features(
        build_from_returns([0.01] * 9 + [-0.15]), windows=(10,)
    )
    spike_series = compute_risk_features(
        build_from_returns([-0.01] * 9 + [0.15]), windows=(10,)
    )
    i = 10
    assert val(crash_series["risk_skew_10"][i]) < 0
    assert val(spike_series["risk_skew_10"][i]) > 0
    # A single extreme outlier fattens the tail either way (excess kurtosis > 0).
    assert val(crash_series["risk_kurtosis_10"][i]) > 0
    assert val(spike_series["risk_kurtosis_10"][i]) > 0


def test_tail_ratio_matches_quantile_magnitudes() -> None:
    candles = make_candles(40, wild_first_half=True)
    series = compute_risk_features(candles, windows=(15,))
    i = 30
    rets = simple_returns(candles)[i - 15 : i]
    q05 = manual_quantile(rets, 0.05)
    q95 = manual_quantile(rets, 0.95)
    if q05 != 0:
        assert series["risk_tail_ratio_15"][i] == pytest.approx(abs(q95) / abs(q05))


def test_calmar_uses_annualized_return_over_max_drawdown() -> None:
    candles = make_candles(40)
    series = compute_risk_features(candles, windows=(20,))
    i = 30
    rets = simple_returns(candles)[i - 20 : i]
    mean_r = fmean(rets)
    max_dd = val(series["risk_max_drawdown_20"][i])
    if max_dd > 0:
        expected = (mean_r * TRADING_DAYS * 100) / max_dd
        assert series["risk_calmar_20"][i] == pytest.approx(expected)


def test_every_feature_has_z_companion() -> None:
    candles = make_candles(60)
    series = compute_risk_features(candles, windows=(5,), normalization_window=30)
    raw_names = [name for name in series if not name.endswith("_z")]
    assert len(raw_names) == 13
    for name in raw_names:
        assert f"{name}_z" in series


def test_engine_registration_and_dependency_order() -> None:
    engine = RiskFeatureEngine(settings=Settings(feature_windows=[5, 10]))
    definitions = engine.registry.list_definitions(category="risk")
    # 13 windowed features x 2 windows, doubled by _z companions.
    assert len(definitions) == 13 * 2 * 2
    assert all(d.version == "v1" for d in definitions)
    order = engine.registry.dependency_order()
    assert order.index("risk_var_95_5") < order.index("risk_cvar_95_5")
    assert order.index("risk_downside_deviation_5") < order.index("risk_sortino_5")
    assert order.index("risk_max_drawdown_5") < order.index("risk_calmar_5")
