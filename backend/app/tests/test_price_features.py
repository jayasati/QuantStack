import math
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest

from app.core.cache import CacheService
from app.core.config import Settings
from app.features.price import PriceFeatureEngine, compute_price_features
from app.features.schema import Candle
from app.features.store import FeatureStore

BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def val(x: float | None) -> float:
    assert x is not None
    return x


def make_candles(n: int = 30, start: float = 100.0) -> list[Candle]:
    """Deterministic uptrend: close climbs 1.0 per bar, bars open above prior close."""
    candles = []
    prev_close = start
    for i in range(n):
        close = start + i
        open_ = prev_close + 0.5 if i > 0 else start
        high = max(open_, close) + 1.0
        low = min(open_, close) - 1.0
        candles.append(
            Candle(
                ts=BASE_TS + timedelta(days=i),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=1000 + i,
            )
        )
        prev_close = close
    return candles


def make_engine(**settings_overrides) -> PriceFeatureEngine:
    return PriceFeatureEngine(settings=Settings(**settings_overrides))


def test_log_and_simple_returns() -> None:
    candles = make_candles(10)
    series = compute_price_features(candles, windows=(5,))
    assert series["price_log_return"][0] is None  # cold start
    assert series["price_simple_return"][3] == pytest.approx(103 / 102 - 1)
    assert series["price_log_return"][3] == pytest.approx(math.log(103 / 102))


def test_gap_pct() -> None:
    candles = make_candles(5)
    series = compute_price_features(candles, windows=(5,))
    # Bar 2 opens at prev_close + 0.5 = 101.5 vs prev close 101.
    assert series["price_gap_pct"][2] == pytest.approx(0.5 / 101 * 100)


def test_true_range_and_atr() -> None:
    candles = make_candles(12)
    series = compute_price_features(candles, windows=(5,))
    bar, prev = candles[3], candles[2]
    expected_tr = max(bar.high - bar.low, abs(bar.high - prev.close), abs(bar.low - prev.close))
    assert series["price_true_range"][3] == pytest.approx(expected_tr)
    trs = [t for t in series["price_true_range"][6:11] if t is not None]
    assert len(trs) == 5
    assert series["price_atr_5"][10] == pytest.approx(sum(trs) / 5)
    assert series["price_atr_5"][4] is None  # needs a full window of TRs


def test_rolling_high_low_and_distances() -> None:
    candles = make_candles(15)
    series = compute_price_features(candles, windows=(5,))
    i = 10
    expected_high = max(c.high for c in candles[i - 4 : i + 1])
    expected_low = min(c.low for c in candles[i - 4 : i + 1])
    assert series["price_rolling_high_5"][i] == pytest.approx(expected_high)
    assert series["price_rolling_low_5"][i] == pytest.approx(expected_low)
    assert val(series["price_dist_from_high_5"][i]) <= 0
    assert val(series["price_dist_from_low_5"][i]) >= 0


def test_momentum_and_acceleration() -> None:
    candles = make_candles(20)
    series = compute_price_features(candles, windows=(5,))
    i = 12
    expected = (candles[i].close / candles[i - 5].close - 1) * 100
    assert series["price_momentum_5"][i] == pytest.approx(expected)
    assert series["price_acceleration_5"][i] == pytest.approx(
        val(series["price_momentum_5"][i]) - val(series["price_momentum_5"][i - 1])
    )


def test_vwap_distance() -> None:
    candles = make_candles(8)
    series = compute_price_features(candles, windows=(5,))
    i = 6
    window = candles[i - 4 : i + 1]
    volume_sum = sum(c.volume for c in window)
    vwap = sum((c.high + c.low + c.close) / 3 * c.volume for c in window) / volume_sum
    expected = (candles[i].close - vwap) / vwap * 100
    assert series["price_vwap_distance_5"][i] == pytest.approx(expected)


def test_beta_alpha_correlation_against_self() -> None:
    candles = make_candles(30)
    series = compute_price_features(candles, benchmark=candles, windows=(10,))
    # Regressing a series on itself: beta 1, alpha 0, correlation 1.
    assert series["price_beta_10"][25] == pytest.approx(1.0)
    assert series["price_alpha_10"][25] == pytest.approx(0.0, abs=1e-9)
    assert series["price_correlation_10"][25] == pytest.approx(1.0)


def test_benchmark_features_absent_without_benchmark() -> None:
    series = compute_price_features(make_candles(30), benchmark=None, windows=(10,))
    assert "price_beta_10" not in series
    assert "price_correlation_10" not in series


def test_cold_start_produces_none_not_values() -> None:
    series = compute_price_features(make_candles(30), windows=(20,))
    assert all(v is None for v in series["price_rolling_high_20"][:19])
    assert all(v is None for v in series["price_momentum_20"][:20])
    assert series["price_momentum_20"][20] is not None


def test_engine_registers_every_feature_with_metadata() -> None:
    engine = make_engine(feature_windows=[5, 10])
    definitions = engine.registry.list_definitions(category="price")
    # 6 window-independent + 11 windowed features x 2 windows.
    assert len(definitions) == 6 + 11 * 2
    for definition in definitions:
        assert definition.version == "v1"
        assert definition.owner == "price_feature_engine"
        assert definition.description


def test_build_values_are_versioned_and_independent() -> None:
    engine = make_engine(feature_windows=[5])
    candles = make_candles(10)
    series = compute_price_features(candles, windows=(5,))
    values = engine.build_values("NIFTY", "D", candles, series)
    assert values, "expected feature values"
    assert all(v.feature_version == "v1" for v in values)
    assert all(v.symbol == "NIFTY" and v.timeframe == "D" for v in values)
    # Every feature stored under its own name — one row per (feature, bar).
    keys = {(v.feature_name, v.ts) for v in values}
    assert len(keys) == len(values)
    windowed = [v for v in values if v.feature_name == "price_momentum_5"]
    assert all(v.window == 5 for v in windowed)


def test_build_values_since_filter_is_incremental() -> None:
    engine = make_engine(feature_windows=[5])
    candles = make_candles(10)
    series = compute_price_features(candles, windows=(5,))
    since = candles[7].ts
    values = engine.build_values("NIFTY", "D", candles, series, since=since)
    assert values
    assert all(v.ts > since for v in values)


def test_registry_dependency_order() -> None:
    engine = make_engine(feature_windows=[5])
    order = engine.registry.dependency_order()
    assert order.index("price_true_range") < order.index("price_atr_5")
    assert order.index("price_momentum_5") < order.index("price_acceleration_5")
    assert order.index("price_simple_return") < order.index("price_beta_5")
    assert "price_acceleration_5" in engine.registry.dependents_of("price_momentum_5")


def test_quality_check_flags_out_of_range_values() -> None:
    engine = make_engine(feature_windows=[5])
    candles = make_candles(10)
    series = compute_price_features(candles, windows=(5,))
    values = engine.build_values("NIFTY", "D", candles, series)
    quality = engine._quality_check(values)
    assert quality["price_simple_return"][0] == pytest.approx(100.0)
    correlations = [v for v in values if v.feature_name == "price_log_return"]
    assert quality["price_log_return"][1] == len(correlations)


async def test_online_store_roundtrip() -> None:
    cache = CacheService(client=fakeredis.aioredis.FakeRedis(decode_responses=True))
    store = FeatureStore(cache=cache)
    engine = make_engine(feature_windows=[5])
    candles = make_candles(10)
    series = compute_price_features(candles, windows=(5,))
    values = engine.build_values("NIFTY", "D", candles, series)

    result = await store.write(values)
    assert result["offline_rows"] == 0  # no DB in unit tests
    assert result["online_entries"] > 0

    latest = await store.latest("NIFTY", "D")
    assert latest["price_simple_return"]["version"] == "v1"
    assert latest["price_simple_return"]["value"] == pytest.approx(109 / 108 - 1)
    # Online store holds the newest bar's value for every feature.
    assert latest["price_momentum_5"]["ts"] == candles[-1].ts.isoformat()
