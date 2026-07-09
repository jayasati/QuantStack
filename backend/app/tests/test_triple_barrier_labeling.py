from datetime import UTC, date, datetime, timedelta
from statistics import pstdev

import pytest

from app.features.schema import Candle
from app.prediction.labeling import (
    K_PROFIT,
    K_STOP,
    MIN_TRAILING_VOL,
    TRAILING_VOL_WINDOW,
    BarrierConfig,
    TripleBarrierLabelingEngine,
    barrier_config_for,
    label_single_entry,
    trailing_volatility,
)

BASE_TS = datetime(2026, 7, 1, tzinfo=UTC)
ENTRY_PRICE = 100.0


def bar(day: int, o: float, h: float, low: float, close: float) -> Candle:
    return Candle(
        ts=BASE_TS + timedelta(days=day), open=o, high=h, low=low, close=close, volume=100
    )


def label_at(fwd, direction="long", cfg=None, **kwargs):
    return label_single_entry(
        fwd, ENTRY_PRICE, BASE_TS, direction, cfg or config(),
        symbol="X", timeframe="D", **kwargs,
    )


def config(**overrides) -> BarrierConfig:
    base = {"profit_target_pct": 0.05, "stop_pct": 0.025, "max_holding_bars": 5}
    base.update(overrides)
    return BarrierConfig(**base)


def test_clean_intrabar_win() -> None:
    fwd = [bar(1, 100, 102, 99, 101), bar(2, 101, 106, 100, 104)]
    label = label_at(fwd)
    assert label.exit_reason == "profit_target"
    assert label.label == "win"
    assert label.exit_return_pct == pytest.approx(5.0)
    assert label.bars_held == 2
    assert not label.gap and not label.ambiguous
    assert label.label_quality == 1.0


def test_clean_intrabar_loss() -> None:
    fwd = [bar(1, 100, 101, 96, 97)]
    label = label_at(fwd)
    assert label.exit_reason == "stop"
    assert label.label == "loss"
    assert label.exit_return_pct == pytest.approx(-2.5)
    assert label.bars_held == 1


def test_timeout_when_neither_barrier_touched() -> None:
    fwd = [bar(i, 100, 101, 99, 100.5) for i in range(1, 6)]
    label = label_at(fwd)
    assert label.exit_reason == "max_holding_time"
    assert label.label == "timeout"
    assert label.bars_held == 5
    assert label.label_quality == pytest.approx(0.8)  # 1.0 - 0.2 max_holding_time penalty


def test_gap_through_profit_target_fills_at_barrier_not_open() -> None:
    fwd = [bar(1, 106, 107, 105.5, 106)]
    label = label_at(fwd)
    assert label.exit_reason == "profit_target"
    assert label.label == "win"
    assert label.gap is True
    assert label.exit_price == pytest.approx(105.0)  # barrier price, not the gapped-through open
    assert label.label_quality == pytest.approx(0.7)  # 1.0 - 0.3 gap penalty


def test_gap_through_stop_is_a_loss() -> None:
    fwd = [bar(1, 96, 97, 95, 96.5)]
    label = label_at(fwd)
    assert label.exit_reason == "stop"
    assert label.label == "loss"
    assert label.gap is True


def test_same_bar_ambiguous_touch_defaults_to_the_worse_outcome() -> None:
    fwd = [bar(1, 100, 106, 96, 101)]
    label = label_at(fwd)
    assert label.exit_reason == "stop"
    assert label.label == "loss"
    assert label.ambiguous is True
    assert label.label_quality == pytest.approx(0.6)  # 1.0 - 0.4 ambiguous penalty


def test_short_direction_profits_from_a_price_decline() -> None:
    fwd = [bar(1, 100, 101, 94, 95)]
    label = label_at(fwd, direction="short")
    assert label.exit_reason == "profit_target"
    assert label.label == "win"
    assert label.exit_return_pct == pytest.approx(5.0)  # positive: short + price fall = profit


def test_trailing_stop_locks_in_a_partial_success_after_pullback() -> None:
    cfg = config(max_holding_bars=6, trail_activation_pct=0.5, trail_pct=0.4)
    fwd = [
        bar(1, 100, 103, 99.5, 102.5),   # +3% favorable move, past the 2.5% activation
        bar(2, 102.5, 103.5, 101, 102),  # new favorable extreme 103.5
        bar(3, 102, 102, 99, 99.5),      # pulls back through the trailing stop
    ]
    label = label_at(fwd, cfg=cfg)
    assert label.exit_reason == "trailing_stop"
    assert label.label == "partial_success"
    assert 0 < label.exit_return_pct < 5.0  # profitable, but short of the full 5% target


def test_event_barrier_forces_an_early_exit_on_the_flagged_date() -> None:
    fwd = [bar(1, 100, 101, 99, 100.5), bar(2, 100.5, 101, 100, 100.8)]
    label = label_at(fwd, event_barrier_dates=frozenset({date(2026, 7, 2)}))
    assert label.exit_reason == "event_barrier"
    assert label.bars_held == 1
    assert label.exit_price == pytest.approx(100.5)  # the bar's close, not a barrier price


def test_liquidity_barrier_forces_an_early_exit_on_the_flagged_date() -> None:
    fwd = [bar(1, 100, 101, 99, 100.5), bar(2, 100.5, 101, 100, 100.8)]
    label = label_at(fwd, liquidity_barrier_dates=frozenset({date(2026, 7, 2)}))
    assert label.exit_reason == "liquidity_barrier"
    assert label.bars_held == 1


def test_no_forward_candles_is_insufficient_data_not_a_crash() -> None:
    label = label_at([])
    assert label.exit_reason == "insufficient_data"
    assert label.label == "timeout"
    assert label.label_quality == 0.1
    assert label.bars_held == 0


def test_fewer_forward_candles_than_max_holding_is_insufficient_data() -> None:
    fwd = [bar(1, 100, 101, 99, 100.2), bar(2, 100.2, 101, 99, 100.3)]
    label = label_at(fwd, cfg=config(max_holding_bars=10))
    assert label.exit_reason == "insufficient_data"
    assert label.label_quality == 0.1


def test_trailing_volatility_matches_manual_pstdev_of_log_returns() -> None:
    closes = [100.0, 101.0, 99.5, 102.0, 100.5, 103.0]
    candles = [bar(i, c, c + 1, c - 1, c) for i, c in enumerate(closes)]
    import math
    manual_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    expected = max(pstdev(manual_returns), MIN_TRAILING_VOL)
    assert trailing_volatility(candles, len(candles) - 1) == pytest.approx(expected)


def test_trailing_volatility_floors_at_minimum_for_a_flat_series() -> None:
    candles = [bar(i, 100, 100, 100, 100) for i in range(5)]
    assert trailing_volatility(candles, 4) == MIN_TRAILING_VOL


def test_barrier_config_for_scales_with_trailing_volatility() -> None:
    closes = [100.0 + (2.0 if i % 2 == 0 else -1.5) for i in range(TRAILING_VOL_WINDOW + 1)]
    candles = [bar(i, c, c + 1, c - 1, c) for i, c in enumerate(closes)]
    cfg = barrier_config_for(candles, len(candles) - 1, max_holding_bars=7)
    vol = trailing_volatility(candles, len(candles) - 1)
    assert cfg.profit_target_pct == pytest.approx(K_PROFIT * vol, rel=1e-4)
    assert cfg.stop_pct == pytest.approx(K_STOP * vol, rel=1e-4)
    assert cfg.max_holding_bars == 7


async def test_label_history_runs_cleanly_without_a_db() -> None:
    engine = TripleBarrierLabelingEngine(session_factory=None)
    labels = await engine.label_history("NIFTY")
    assert labels == []


async def test_recent_returns_empty_list_without_a_session_factory() -> None:
    engine = TripleBarrierLabelingEngine(session_factory=None)
    assert await engine.recent("NIFTY") == []
