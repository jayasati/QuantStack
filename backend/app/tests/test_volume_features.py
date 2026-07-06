from datetime import UTC, datetime, timedelta
from statistics import fmean, pstdev

import pytest

from app.core.config import Settings
from app.features.normalize import rolling_zscore
from app.features.schema import Candle
from app.features.volume import VolumeFeatureEngine, compute_volume_features

BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def val(x: float | None) -> float:
    assert x is not None
    return x


def make_candles(n: int = 40, start: float = 100.0) -> list[Candle]:
    """Uptrend with rising, varying volume: close +1/bar, volume alternates around a ramp."""
    candles = []
    prev_close = start
    for i in range(n):
        close = start + i
        open_ = prev_close + 0.5 if i > 0 else start
        high = max(open_, close) + 1.0
        low = min(open_, close) - 1.0
        volume = 1000 + 50 * i + (200 if i % 3 == 0 else 0)
        candles.append(
            Candle(ts=BASE_TS + timedelta(days=i), open=open_, high=high,
                   low=low, close=close, volume=volume)
        )
        prev_close = close
    return candles


def volumes_of(candles: list[Candle]) -> list[float]:
    return [float(c.volume) for c in candles]


def test_rolling_avg_and_rvol() -> None:
    candles = make_candles(15)
    volumes = volumes_of(candles)
    series = compute_volume_features(candles, windows=(5,))
    i = 10
    assert series["volume_rolling_avg_5"][i] == pytest.approx(fmean(volumes[i - 4 : i + 1]))
    # RVOL uses the *prior* window, excluding the current bar.
    assert series["volume_rvol_5"][i] == pytest.approx(volumes[i] / fmean(volumes[i - 5 : i]))
    assert series["volume_rvol_5"][4] is None  # cold start


def test_volume_spike_is_windowed_zscore() -> None:
    candles = make_candles(15)
    volumes = volumes_of(candles)
    series = compute_volume_features(candles, windows=(5,))
    i = 9  # i % 3 == 0 bar carries the +200 spike
    window = volumes[i - 4 : i + 1]
    expected = (volumes[i] - fmean(window)) / pstdev(window)
    assert series["volume_spike_5"][i] == pytest.approx(expected)


def test_buying_selling_pressure_and_delta() -> None:
    candles = make_candles(10)
    series = compute_volume_features(candles, windows=(5,))
    i = 5
    buy, sell = val(series["volume_buying_pressure"][i]), val(series["volume_selling_pressure"][i])
    assert buy + sell == pytest.approx(candles[i].volume)
    assert series["volume_delta"][i] == pytest.approx(buy - sell)
    assert buy >= 0 and sell >= 0


def test_imbalance_bounded() -> None:
    candles = make_candles(30)
    series = compute_volume_features(candles, windows=(5,))
    observed = [v for v in series["volume_imbalance_5"] if v is not None]
    assert observed
    assert all(-1.0 <= v <= 1.0 for v in observed)


def test_obv_cumulative() -> None:
    candles = make_candles(10)
    volumes = volumes_of(candles)
    series = compute_volume_features(candles, windows=(5,))
    # Closes rise every bar, so OBV is the cumulative sum of volume from bar 1.
    assert series["volume_obv"][0] is None
    assert series["volume_obv"][4] == pytest.approx(sum(volumes[1:5]))
    assert series["volume_obv"][9] == pytest.approx(sum(volumes[1:10]))


def test_accumulation_distribution_manual() -> None:
    candles = make_candles(6)
    series = compute_volume_features(candles, windows=(5,))
    running = 0.0
    for i, c in enumerate(candles):
        rng = c.high - c.low
        mfm = ((c.close - c.low) - (c.high - c.close)) / rng if rng > 0 else 0.0
        running += mfm * c.volume
        assert series["volume_accum_dist"][i] == pytest.approx(running)


def test_cmf_manual_and_bounded() -> None:
    candles = make_candles(12)
    volumes = volumes_of(candles)
    series = compute_volume_features(candles, windows=(5,))
    i = 8
    mfm = []
    for c in candles[i - 4 : i + 1]:
        rng = c.high - c.low
        mfm.append(((c.close - c.low) - (c.high - c.close)) / rng if rng > 0 else 0.0)
    expected = sum(m * v for m, v in zip(mfm, volumes[i - 4 : i + 1], strict=True)) / sum(
        volumes[i - 4 : i + 1]
    )
    assert series["volume_cmf_5"][i] == pytest.approx(expected)
    assert -1.0 <= val(series["volume_cmf_5"][i]) <= 1.0


def test_mfi_hits_100_in_pure_uptrend() -> None:
    candles = make_candles(20)
    series = compute_volume_features(candles, windows=(5,))
    # Typical price rises every bar, so all money flow is positive.
    assert series["volume_mfi_5"][10] == pytest.approx(100.0)


def test_volume_oscillator_manual() -> None:
    candles = make_candles(25)
    volumes = volumes_of(candles)
    series = compute_volume_features(candles, windows=(5,))
    i = 12
    fast = fmean(volumes[i - 4 : i + 1])
    slow = fmean(volumes[i - 9 : i + 1])
    assert series["volume_oscillator_5"][i] == pytest.approx((fast - slow) / slow * 100)
    assert series["volume_oscillator_5"][8] is None  # needs 2w bars


def test_volume_trend_positive_on_rising_volume() -> None:
    candles = make_candles(30)
    series = compute_volume_features(candles, windows=(10,))
    assert val(series["volume_trend_10"][20]) > 0


def test_zero_volume_history_produces_nothing() -> None:
    candles = [
        Candle(ts=BASE_TS + timedelta(days=i), open=100 + i, high=102 + i,
               low=99 + i, close=101 + i, volume=0)
        for i in range(30)
    ]
    assert compute_volume_features(candles, windows=(5,)) == {}


def test_every_feature_has_normalized_companion() -> None:
    candles = make_candles(40)
    series = compute_volume_features(candles, windows=(5,), normalization_window=20)
    raw_names = [name for name in series if not name.endswith("_z")]
    assert raw_names
    for name in raw_names:
        assert f"{name}_z" in series
    # Spot-check the z math against the helper on its own input.
    assert series["volume_rvol_5_z"] == rolling_zscore(series["volume_rvol_5"], 20)


def test_rolling_zscore_is_lookahead_safe() -> None:
    flat: list[float | None] = [10.0] * 20
    spiked = flat + [100.0]
    z_before = rolling_zscore(flat, 20, min_obs=5)
    z_with_spike = rolling_zscore(spiked, 20, min_obs=5)
    # Adding a future bar must not change any earlier bar's z-score.
    assert z_with_spike[:20] == z_before
    assert val(z_with_spike[20]) > 3


def test_engine_registers_raw_and_normalized_features() -> None:
    engine = VolumeFeatureEngine(settings=Settings(feature_windows=[5, 10]))
    definitions = engine.registry.list_definitions(category="volume")
    # 5 bar-level + 8 windowed x 2 windows = 21 raw, doubled by _z companions.
    assert len(definitions) == 21 * 2
    normalized = [d for d in definitions if d.feature_name.endswith("_z")]
    assert len(normalized) == 21
    for d in normalized:
        assert d.unit == "zscore"
        assert d.dependencies == (d.feature_name.removesuffix("_z"),)
    assert all(d.version == "v1" for d in definitions)


def test_engine_build_values_stores_features_independently() -> None:
    engine = VolumeFeatureEngine(settings=Settings(feature_windows=[5]))
    candles = make_candles(30)
    series = engine._compute(candles)
    values = engine.build_values("RELIANCE", "D", candles, series)
    assert values
    assert all(v.feature_version == "v1" for v in values)
    keys = {(v.feature_name, v.ts) for v in values}
    assert len(keys) == len(values)
