import math
from datetime import UTC, datetime, timedelta
from statistics import pstdev

import pytest

from app.core.config import Settings
from app.features.relative import (
    REFERENCES,
    RelativeStrengthEngine,
    compute_relative_features,
)
from app.features.schema import Candle

BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def val(x: float | None) -> float:
    assert x is not None
    return x


def make_candles(n: int = 40, start: float = 100.0, daily_return: float = 0.01,
                 volume: int = 1000, wobble: float = 0.0) -> list[Candle]:
    """Geometric price path with optional alternating wobble on the return."""
    candles = []
    close = start
    for i in range(n):
        r = daily_return + (wobble if i % 2 == 0 else -wobble)
        close = close * (1 + r)
        candles.append(
            Candle(ts=BASE_TS + timedelta(days=i), open=close, high=close * 1.01,
                   low=close * 0.99, close=close, volume=volume)
        )
    return candles


def standard_inputs(n: int = 40):
    stock = make_candles(n, daily_return=0.02, volume=2000)
    references = {
        "nifty": make_candles(n, daily_return=0.01),
        "sensex": make_candles(n, daily_return=0.01),
        "sector": make_candles(n, daily_return=0.015),
        "industry": make_candles(n, daily_return=0.015),
    }
    peers = {
        "PEER1": make_candles(n, daily_return=0.005, volume=1000),
        "PEER2": make_candles(n, daily_return=0.0, volume=3000),
    }
    return stock, references, peers


def test_strength_is_cumulative_log_outperformance() -> None:
    stock, references, peers = standard_inputs()
    series = compute_relative_features(stock, references, peers, windows=(10,))
    i = 20
    expected = (math.log(1.02) - math.log(1.01)) * 10 * 100
    assert series["rs_nifty_strength_10"][i] == pytest.approx(expected)
    # Sector outpaces nifty, so sector-relative strength is smaller.
    assert val(series["rs_sector_strength_10"][i]) < val(series["rs_nifty_strength_10"][i])
    assert series["rs_nifty_strength_10"][5] is None  # cold start


def test_momentum_matches_constant_drift() -> None:
    stock, references, peers = standard_inputs()
    series = compute_relative_features(stock, references, peers, windows=(10,))
    # ln(stock/nifty) grows by a constant each bar -> slope = that constant.
    expected = (math.log(1.02) - math.log(1.01)) * 100
    assert series["rs_nifty_momentum_10"][20] == pytest.approx(expected)


def test_relative_volatility_ratio() -> None:
    n = 40
    stock = make_candles(n, daily_return=0.01, wobble=0.02)
    references = {"nifty": make_candles(n, daily_return=0.01, wobble=0.005)}
    series = compute_relative_features(stock, references, {}, windows=(10,))
    i = 30
    stock_rets = [
        math.log(stock[j].close / stock[j - 1].close) for j in range(i - 9, i + 1)
    ]
    ref = references["nifty"]
    ref_rets = [math.log(ref[j].close / ref[j - 1].close) for j in range(i - 9, i + 1)]
    assert series["rs_nifty_volatility_10"][i] == pytest.approx(
        pstdev(stock_rets) / pstdev(ref_rets)
    )
    assert val(series["rs_nifty_volatility_10"][i]) > 1


def test_relative_volume_and_percentile_vs_peers() -> None:
    stock, references, peers = standard_inputs()
    series = compute_relative_features(stock, references, peers, windows=(10,))
    i = 20
    # Stock volume 2000 vs peer mean (1000 + 3000)/2 = 2000 -> ratio 1.
    assert series["rs_relative_volume_10"][i] == pytest.approx(1.0)
    # Stock return beats both peers -> top percentile of the 3-member group.
    assert series["rs_percentile_rank_10"][i] == pytest.approx(100.0)


def test_outperformance_above_50_when_beating_everything() -> None:
    stock, references, peers = standard_inputs()
    series = compute_relative_features(stock, references, peers, windows=(10,))
    assert val(series["rs_outperformance_10"][20]) > 50
    laggard = make_candles(40, daily_return=0.0, volume=2000)
    lag_series = compute_relative_features(laggard, references, peers, windows=(10,))
    assert val(lag_series["rs_outperformance_10"][20]) < 50


def test_missing_reference_produces_empty_series_not_errors() -> None:
    stock, _, peers = standard_inputs()
    series = compute_relative_features(stock, {}, peers, windows=(10,))
    assert all(v is None for v in series["rs_sensex_strength_10"])
    # Peer-based features still work without index references.
    assert series["rs_percentile_rank_10"][20] is not None


def test_registration_and_z_companions() -> None:
    stock, references, peers = standard_inputs()
    series = compute_relative_features(
        stock, references, peers, windows=(10,), normalization_window=20
    )
    raw = [name for name in series if not name.endswith("_z")]
    for name in raw:
        assert f"{name}_z" in series

    engine = RelativeStrengthEngine(settings=Settings(feature_windows=[5, 10]))
    definitions = engine.registry.list_definitions(category="relative")
    # (5 refs x 3 families + 3 group features) x 2 windows = 36 raw, x2 with _z.
    assert len(definitions) == 36 * 2
    assert all(d.version == "v1" for d in definitions)
    order = engine.registry.dependency_order()
    assert order.index("rs_nifty_strength_5") < order.index("rs_outperformance_5")
    assert set(REFERENCES) == {"nifty", "sensex", "sector", "industry", "peers"}
