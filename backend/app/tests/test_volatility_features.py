import math
from datetime import UTC, datetime, timedelta
from statistics import fmean, pstdev

import pytest

from app.core.config import Settings
from app.features.schema import Candle
from app.features.volatility import (
    TRADING_DAYS,
    VolatilityFeatureEngine,
    compute_volatility_features,
)

BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def val(x: float | None) -> float:
    assert x is not None
    return x


def make_candles(n: int = 60, start: float = 100.0, wild_first_half: bool = False) -> list[Candle]:
    """Deterministic series; optionally violent swings early and a calm tail."""
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


def log_returns(candles: list[Candle]) -> list[float]:
    return [
        math.log(candles[i].close / candles[i - 1].close)
        for i in range(1, len(candles))
    ]


def test_historical_and_realized_volatility_manual() -> None:
    candles = make_candles(20)
    series = compute_volatility_features(candles, windows=(5,))
    i = 10
    rets = log_returns(candles)[i - 5 : i]  # returns at bars i-4..i
    expected_hist = pstdev(rets) * math.sqrt(TRADING_DAYS) * 100
    expected_realized = math.sqrt(fmean([r * r for r in rets]) * TRADING_DAYS) * 100
    assert series["volatility_hist_5"][i] == pytest.approx(expected_hist)
    assert series["volatility_realized_5"][i] == pytest.approx(expected_realized)
    assert series["volatility_hist_5"][4] is None  # cold start


def test_rolling_volatility_manual() -> None:
    candles = make_candles(20)
    series = compute_volatility_features(candles, windows=(5,))
    i = 12
    rets = [candles[j].close / candles[j - 1].close - 1 for j in range(i - 4, i + 1)]
    assert series["volatility_rolling_5"][i] == pytest.approx(pstdev(rets) * 100)


def test_vol_of_vol_needs_two_windows() -> None:
    candles = make_candles(30)
    series = compute_volatility_features(candles, windows=(5,))
    assert series["volatility_of_volatility_5"][8] is None
    i = 15
    vols = [val(series["volatility_rolling_5"][j]) for j in range(i - 4, i + 1)]
    assert series["volatility_of_volatility_5"][i] == pytest.approx(pstdev(vols))


def test_atr_pct_manual() -> None:
    candles = make_candles(15)
    series = compute_volatility_features(candles, windows=(5,))
    i = 10
    trs = []
    for j in range(i - 4, i + 1):
        c, p = candles[j], candles[j - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    expected = sum(trs) / 5 / candles[i].close * 100
    assert series["volatility_atr_pct_5"][i] == pytest.approx(expected)


def test_expected_move_manual() -> None:
    candles = make_candles(20)
    series = compute_volatility_features(candles, windows=(10,))
    i = 15
    hv = val(series["volatility_hist_10"][i])
    expected = candles[i].close * hv / 100 * math.sqrt(10 / TRADING_DAYS)
    assert series["volatility_expected_move_10"][i] == pytest.approx(expected)


def test_regime_and_compression_after_calm_tail() -> None:
    candles = make_candles(80, wild_first_half=True)
    series = compute_volatility_features(candles, windows=(5,), normalization_window=60)
    last = len(candles) - 1
    # The tail is far calmer than the wild first half: low regime, tight squeeze.
    assert series["volatility_regime_5"][last] == 0.0
    assert val(series["volatility_compression_5"][last]) > 0.6
    regimes = {v for v in series["volatility_regime_5"] if v is not None}
    assert regimes <= {0.0, 1.0, 2.0}


def test_expansion_probability_maps_compression() -> None:
    candles = make_candles(80, wild_first_half=True)
    series = compute_volatility_features(candles, windows=(5,), normalization_window=60)
    for i in range(len(candles)):
        compression = series["volatility_compression_5"][i]
        probability = series["volatility_expansion_prob_5"][i]
        if compression is None:
            assert probability is None
        else:
            assert probability == pytest.approx(0.1 + 0.8 * compression)
            assert 0.1 <= val(probability) <= 0.9


def test_vix_distance_realized_minus_implied() -> None:
    candles = make_candles(30)
    vix = [
        Candle(ts=c.ts, open=15.0, high=16.0, low=14.0, close=15.0, volume=0)
        for c in candles
    ]
    series = compute_volatility_features(candles, vix=vix, windows=(5,))
    i = 20
    assert series["volatility_vix_distance_5"][i] == pytest.approx(
        val(series["volatility_hist_5"][i]) - 15.0
    )


def test_vix_distance_empty_without_vix_data() -> None:
    candles = make_candles(30)
    series = compute_volatility_features(candles, vix=None, windows=(5,))
    assert all(v is None for v in series["volatility_vix_distance_5"])


def test_every_feature_has_z_companion() -> None:
    candles = make_candles(60)
    series = compute_volatility_features(candles, windows=(5,), normalization_window=30)
    raw_names = [name for name in series if not name.endswith("_z")]
    assert len(raw_names) == 10
    for name in raw_names:
        assert f"{name}_z" in series


def test_engine_registration_and_vix_reference() -> None:
    engine = VolatilityFeatureEngine(settings=Settings(feature_windows=[5, 10]))
    definitions = engine.registry.list_definitions(category="volatility")
    # 10 windowed features x 2 windows, doubled by _z companions.
    assert len(definitions) == 10 * 2 * 2
    assert all(d.version == "v1" for d in definitions)
    assert engine._reference_symbol("RELIANCE") == "INDIAVIX"
    assert engine._reference_symbol("INDIAVIX") is None
    order = engine.registry.dependency_order()
    assert order.index("volatility_compression_5") < order.index("volatility_expansion_prob_5")
    assert order.index("volatility_rolling_5") < order.index("volatility_of_volatility_5")
