from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.features.schema import Candle
from app.features.structure import (
    IST,
    MarketStructureEngine,
    compute_structure_daily,
    compute_structure_sessions,
)

BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def val(x: float | None) -> float:
    assert x is not None
    return x


def bar(i: int, o: float, h: float, low: float, c: float, volume: int = 1000) -> Candle:
    return Candle(ts=BASE_TS + timedelta(days=i), open=o, high=h, low=low,
                  close=c, volume=volume)


def zigzag(n: int = 40, amplitude: float = 5.0, drift: float = 0.6) -> list[Candle]:
    """Rising zigzag: swings every 5 bars, higher highs and higher lows."""
    candles = []
    price = 100.0
    for i in range(n):
        phase = i % 10
        direction = 1.0 if phase < 5 else -1.0
        move = direction * amplitude / 5 + drift / 5
        close = price + move
        high = max(price, close) + 0.3
        low = min(price, close) - 0.3
        candles.append(bar(i, price, high, low, close))
        price = close
    return candles


def test_swings_confirm_with_fractal_lag() -> None:
    candles = zigzag(40)
    series = compute_structure_daily(candles, fractal=2)
    assert series["ms_swing_high"][2] is None  # nothing confirmed yet
    confirmed = [v for v in series["ms_swing_high"] if v is not None]
    assert confirmed
    # Rising zigzag: consecutive swing highs increase.
    assert confirmed[-1] > confirmed[0]
    assert val(series["ms_higher_highs"][39]) >= 1


def test_trend_direction_up_in_rising_zigzag() -> None:
    candles = zigzag(40)
    series = compute_structure_daily(candles, fractal=2)
    tail = [v for v in series["ms_trend_direction"][-10:] if v is not None]
    assert tail and all(v == 1.0 for v in tail)
    bias_tail = [v for v in series["ms_structural_bias"][-5:] if v is not None]
    assert bias_tail and all(v > 0 for v in bias_tail)


def test_break_of_structure_fires_once_per_level() -> None:
    candles = zigzag(40)
    series = compute_structure_daily(candles, fractal=2)
    breaks = [v for v in series["ms_break_of_structure"] if v]
    assert breaks  # the rising pattern breaks prior swing highs
    assert all(v == 1.0 for v in breaks)
    # Never two consecutive +1 on the same level: each break re-arms only
    # after a new swing confirms, so BOS bars are sparse.
    bos = series["ms_break_of_structure"]
    assert sum(1 for v in bos if v) < len([v for v in bos if v == 0.0])


def test_change_of_character_against_trend() -> None:
    # Up-zigzag, then a hard breakdown below the last swing low.
    candles = zigzag(30)
    price = candles[-1].close
    for i in range(30, 36):
        price -= 6.0
        candles.append(bar(i, price + 6.0, price + 6.3, price - 0.3, price))
    series = compute_structure_daily(candles, fractal=2)
    assert -1.0 in series["ms_change_of_character"]


def test_fair_value_gap_forms_and_fills() -> None:
    candles = [
        bar(0, 100, 101, 99, 100.5),
        bar(1, 100.5, 102, 100, 101.5),
        bar(2, 104, 106, 103.5, 105.5),  # low 103.5 > high[0] 101 -> bullish FVG
        bar(3, 105.5, 107, 105, 106.5),
    ]
    series = compute_structure_daily(candles, fractal=2)
    assert series["ms_fvg_net_unfilled"][2] == 1.0
    # Price trades back through the gap midpoint -> filled.
    candles.append(bar(4, 106.5, 106.6, 101.5, 102.0))
    filled = compute_structure_daily(candles, fractal=2)
    assert filled["ms_fvg_net_unfilled"][4] == 0.0


def test_probabilities_bounded_and_present() -> None:
    candles = zigzag(60)
    series = compute_structure_daily(candles, fractal=2)
    for name in ("ms_breakout_probability", "ms_sweep_probability"):
        observed = [v for v in series[name] if v is not None]
        assert observed
        assert all(0.05 <= v <= 0.95 for v in observed)


def session_bars(session_day: int, n: int = 75, trend: float = 0.2,
                 volume: int = 0) -> list[Candle]:
    """One 5m-bar session in IST market hours."""
    start = datetime(2026, 7, 6 + session_day, 9, 15, tzinfo=IST)
    bars = []
    price = 100.0
    for i in range(n):
        close = price + trend
        high = max(price, close) + 0.2
        low = min(price, close) - 0.2
        bars.append(Candle(ts=start + timedelta(minutes=5 * i), open=price,
                           high=high, low=low, close=close, volume=volume))
        price = close
    return bars


def test_session_features_computed_per_session() -> None:
    intraday = session_bars(0) + session_bars(1)
    timestamps, series = compute_structure_sessions(intraday)
    assert len(timestamps) == 2
    assert timestamps[0] == datetime(2026, 7, 6, tzinfo=IST)
    # Steady uptrend: close finishes at the top of the session range.
    assert val(series["ms_auction_imbalance"][0]) > 0.8
    assert val(series["ms_ib_extension"][0]) > 1.0
    assert val(series["ms_opening_range_pct"][0]) > 0
    # Zero-volume session falls back to time-weighted profile: still present.
    assert series["ms_poc_distance_pct"][0] is not None
    assert series["ms_vwap_band_position"][0] is not None


def test_incomplete_sessions_are_skipped() -> None:
    intraday = session_bars(0) + session_bars(1)[:5]  # second session truncated
    timestamps, _ = compute_structure_sessions(intraday)
    assert len(timestamps) == 1


def test_registration_and_z_companions() -> None:
    candles = zigzag(40)
    series = compute_structure_daily(candles, fractal=2, normalization_window=20)
    raw = [name for name in series if not name.endswith("_z")]
    assert len(raw) == 13
    for name in raw:
        assert f"{name}_z" in series

    engine = MarketStructureEngine(settings=Settings())
    definitions = engine.registry.list_definitions(category="structure")
    # 13 daily + 9 session = 22 raw, doubled by _z companions.
    assert len(definitions) == 22 * 2
    assert all(d.version == "v1" for d in definitions)
    order = engine.registry.dependency_order()
    assert order.index("ms_structural_bias") < order.index("ms_breakout_probability")
