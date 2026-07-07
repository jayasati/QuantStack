from datetime import UTC, datetime, timedelta
from statistics import fmean

import pytest

from app.core.config import Settings
from app.features.liquidity import (
    LiquidityFeatureEngine,
    QuoteSnapshot,
    compute_liquidity_candle_features,
    compute_liquidity_quote_features,
    parse_quote_event,
)
from app.features.schema import Candle

BASE_TS = datetime(2026, 7, 6, 9, 15, tzinfo=UTC)


def val(x: float | None) -> float:
    assert x is not None
    return x


def make_quotes(n: int = 30, spread: float = 0.10, bid_depth: float = 5000.0,
                ask_depth: float = 4000.0) -> list[QuoteSnapshot]:
    quotes = []
    for i in range(n):
        bid = 100.0 + i * 0.05
        quotes.append(
            QuoteSnapshot(
                ts=BASE_TS + timedelta(seconds=15 * i),
                bid=bid,
                ask=bid + spread,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
            )
        )
    return quotes


def test_spread_features_manual() -> None:
    quotes = make_quotes(10, spread=0.10)
    series = compute_liquidity_quote_features(quotes, windows=(5,))
    i = 6
    assert series["liquidity_current_spread"][i] == pytest.approx(0.10)
    mid = (val(quotes[i].bid) + val(quotes[i].ask)) / 2
    assert series["liquidity_spread_pct"][i] == pytest.approx(0.10 / mid * 100)
    spreads = [val(s) for s in series["liquidity_current_spread"][2:7]]
    assert series["liquidity_avg_spread_5"][6] == pytest.approx(fmean(spreads))
    assert series["liquidity_avg_spread_5"][3] is None  # cold start


def test_depth_and_imbalance() -> None:
    quotes = make_quotes(5, bid_depth=6000, ask_depth=4000)
    series = compute_liquidity_quote_features(quotes, windows=(5,))
    assert series["liquidity_bid_depth"][2] == 6000
    assert series["liquidity_ask_depth"][2] == 4000
    assert series["liquidity_order_book_imbalance"][2] == pytest.approx(2000 / 10000)
    observed = [v for v in series["liquidity_order_book_imbalance"] if v is not None]
    assert all(-1 <= v <= 1 for v in observed)


def test_market_impact_grows_with_order_size_vs_depth() -> None:
    quotes = make_quotes(5)
    thin = compute_liquidity_quote_features(quotes, windows=(5,), reference_order_qty=9000)
    thick = compute_liquidity_quote_features(quotes, windows=(5,), reference_order_qty=100)
    assert val(thin["liquidity_market_impact_pct"][2]) > val(
        thick["liquidity_market_impact_pct"][2]
    )
    # Impact is at least the half-spread cost.
    half_spread_pct = val(thick["liquidity_spread_pct"][2]) / 2
    assert val(thick["liquidity_market_impact_pct"][2]) > half_spread_pct


def test_liquidity_score_rewards_tight_spreads() -> None:
    tight = compute_liquidity_quote_features(
        make_quotes(40, spread=0.02), windows=(5,), normalization_window=20
    )
    wide = compute_liquidity_quote_features(
        make_quotes(40, spread=0.50), windows=(5,), normalization_window=20
    )
    i = 30
    assert val(tight["liquidity_score"][i]) > val(wide["liquidity_score"][i])
    scores = [v for v in tight["liquidity_score"] if v is not None]
    assert all(0 <= s <= 100 for s in scores)


def test_liquidity_trend_positive_when_spreads_tighten() -> None:
    quotes = []
    for i in range(60):
        # Spread narrows over time -> score rises -> positive trend.
        spread = max(0.02, 0.40 - i * 0.006)
        bid = 100.0
        quotes.append(
            QuoteSnapshot(ts=BASE_TS + timedelta(seconds=15 * i), bid=bid,
                          ask=bid + spread, bid_depth=5000, ask_depth=5000)
        )
    series = compute_liquidity_quote_features(quotes, windows=(10,), normalization_window=30)
    assert val(series["liquidity_trend_10"][55]) > 0


def test_quotes_without_book_produce_no_spread_features() -> None:
    # Index quotes: LTP only, no bid/ask/depth.
    quotes = [
        QuoteSnapshot(ts=BASE_TS + timedelta(seconds=15 * i)) for i in range(20)
    ]
    series = compute_liquidity_quote_features(quotes, windows=(5,))
    for name in ("liquidity_current_spread", "liquidity_order_book_imbalance",
                 "liquidity_score"):
        assert all(v is None for v in series[name])


def test_turnover_from_candles_and_zero_volume_guard() -> None:
    candles = [
        Candle(ts=BASE_TS + timedelta(days=i), open=100, high=101, low=99,
               close=100.0 + i, volume=1000 * (i + 1))
        for i in range(5)
    ]
    series = compute_liquidity_candle_features(candles)
    assert series["liquidity_turnover"][2] == pytest.approx(102.0 * 3000)
    index_candles = [
        Candle(ts=BASE_TS + timedelta(days=i), open=100, high=101, low=99,
               close=100.0 + i, volume=0)
        for i in range(5)
    ]
    index_series = compute_liquidity_candle_features(index_candles)
    assert all(v is None for v in index_series["liquidity_turnover"])
    # Delivery % stays empty until an NSE delivery source exists.
    assert all(v is None for v in series["liquidity_delivery_pct"])


def test_parse_quote_event_rest_and_websocket_shapes() -> None:
    rest = parse_quote_event({
        "timestamp": "2026-07-06T09:30:00+00:00",
        "metadata": {
            "bid": 99.9, "ask": 100.1, "bid_qty": 500, "ask_qty": 700,
            "depth": {
                "buy": [{"quantity": 100}, {"quantity": 200}],
                "sell": [{"quantity": 150}, {"quantity": 250}],
            },
        },
    })
    assert rest is not None
    assert rest.bid == 99.9 and rest.ask == 100.1
    assert rest.bid_depth == 300  # 5-level totals win over top-of-book qty
    assert rest.ask_depth == 400

    ws = parse_quote_event({
        "timestamp": "2026-07-06T09:30:15+00:00",
        "metadata": {"total_buy_qty": 12000, "total_sell_qty": 8000},
    })
    assert ws is not None
    assert ws.bid is None  # websocket ticks carry no top-of-book prices
    assert ws.bid_depth == 12000 and ws.ask_depth == 8000

    assert parse_quote_event({"metadata": {}}) is None  # no timestamp


def test_every_feature_has_z_companion_and_registration() -> None:
    quotes = make_quotes(40)
    series = compute_liquidity_quote_features(quotes, windows=(5,), normalization_window=20)
    raw = [name for name in series if not name.endswith("_z")]
    for name in raw:
        assert f"{name}_z" in series

    engine = LiquidityFeatureEngine(settings=Settings(feature_windows=[5, 10]))
    definitions = engine.registry.list_definitions(category="liquidity")
    # 9 base + 2 windowed x 2 windows = 13 raw, doubled by _z companions.
    assert len(definitions) == 13 * 2
    assert all(d.version == "v1" for d in definitions)
    order = engine.registry.dependency_order()
    assert order.index("liquidity_score") < order.index("liquidity_trend_5")
    assert order.index("liquidity_current_spread") < order.index("liquidity_spread_pct")
