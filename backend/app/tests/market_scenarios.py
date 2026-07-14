"""Synthetic market-scenario fixture builders for cross-layer integration
tests (IRR-2026-07-11 finding #6, follow-up to the initial scaffold in
test_cross_layer_integration.py).

Not a test file itself (no test_ prefix) -- these are builders that write
real feature_store / ohlcv_candles rows so intelligence and prediction
engines produce REAL, direction-differentiated output against a real
Postgres, not just neutral/empty defaults. Feature names, scales, and
saturation constants below were read directly out of each engine's own
assess() function (app/intelligence/*.py) -- see each section's comment
for the source file.

Two tiers:
- `snapshot_rows(...)`: a single point-in-time row per feature, covering
  every intelligence domain that reads latest_values() directly. Enough
  to differentiate CompositeMarketIntelligenceEngine and the
  feature-only evidence sources in ConvictionEngine (institutional_flow,
  market_structure, liquidity, sector_strength).
- `write_ensemble_training_history(...)`: real OHLCV candle history +
  matching historical feature_store rows spanning the same dates, large
  enough to actually clear EnsemblePredictionEngine.MIN_TRAINING_SAMPLES
  and produce a real (non-"untrained") prediction -- which is what
  unlocks non-neutral output from calibration/market_context/model
  agreement, the ensemble-gated half of ConvictionEngine's evidence.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import insert

from app.database.tables import OhlcvCandle
from app.features.schema import FeatureValue
from app.prediction.ensemble import ENSEMBLE_FEATURE_SPECS

Direction = Literal["bullish", "bearish"]

BASE_TS = datetime(2026, 6, 1, tzinfo=UTC)

SECTOR_UNIVERSE = (
    "Banking", "IT", "Auto", "Energy", "Pharma", "FMCG", "PSU", "PSU Bank",
    "Private Bank", "Realty", "Metal", "Infrastructure",
)
FACTOR_UNIVERSE = (
    "USDINR", "DXY", "US10Y", "INDIA10Y", "CRUDE", "GOLD", "SILVER", "NATGAS",
    "SPX", "NDX", "NIKKEI", "HANGSENG", "DAX", "CRYPTO_MCAP",
)


def _sign(direction: Direction) -> float:
    return 1.0 if direction == "bullish" else -1.0


def _fv(feature_name: str, symbol: str, timeframe: str, value: float, ts: datetime) -> FeatureValue:
    return FeatureValue(
        feature_name=feature_name, feature_version="v1",
        symbol=symbol, timeframe=timeframe, ts=ts, value=value,
    )


def snapshot_rows(
    symbol: str, direction: Direction, as_of: datetime = BASE_TS
) -> list[FeatureValue]:
    """One point-in-time row per feature across every intelligence domain
    that reads latest_values() directly, all coherently pointing the same
    direction. Domains with no meaningful bull/bear axis (volatility,
    events) are set to a calm/neutral-magnitude reading so they don't
    fight the directional read."""
    s = _sign(direction)
    rows: list[FeatureValue] = []

    # trend.py: price_momentum_{5,20,50,200} (tanh scale per window),
    # ms_trend_direction, ms_structural_bias.
    for window, scale in ((5, 3.0), (20, 6.0), (50, 10.0), (200, 20.0)):
        rows.append(_fv(f"price_momentum_{window}", symbol, "D", s * scale * 0.8, as_of))
    rows.append(_fv("ms_trend_direction", symbol, "D", s, as_of))
    rows.append(_fv("ms_structural_bias", symbol, "D", s * 0.8, as_of))
    # price_acceleration_20 is written once below, by the momentum.py loop
    # (which covers windows 5/20/50/200) -- writing it here too would be a
    # duplicate (feature_name, symbol, timeframe, ts) key within the same
    # upsert batch, which Postgres rejects outright ("ON CONFLICT DO UPDATE
    # command cannot affect row a second time").
    rows.append(_fv("price_dist_from_high_50", symbol, "D", -1.0 if direction == "bullish" else -8.0, as_of))
    rows.append(_fv("price_dist_from_low_50", symbol, "D", -8.0 if direction == "bullish" else -1.0, as_of))
    rows.append(_fv("volume_rvol_20", symbol, "D", 1.8, as_of))
    rows.append(_fv("volume_obv_z", symbol, "D", s * 1.2, as_of))

    # momentum.py: reuses price_momentum_*, plus acceleration/z-scores.
    for window, scale in ((5, 1.5), (20, 3.0), (50, 5.0), (200, 10.0)):
        rows.append(_fv(f"price_acceleration_{window}", symbol, "D", s * scale * 0.6, as_of))
        rows.append(_fv(f"price_momentum_{window}_z", symbol, "D", s * 1.0, as_of))

    # volatility.py: calm/normal regime (direction-agnostic magnitude only).
    for window in (5, 20, 50, 100):
        rows.append(_fv(f"volatility_regime_{window}", symbol, "D", 1.0, as_of))
        rows.append(_fv(f"volatility_vix_distance_{window}", symbol, "D", 0.0, as_of))
        rows.append(_fv(f"volatility_compression_{window}", symbol, "D", 0.2, as_of))
        rows.append(_fv(f"volatility_expansion_prob_{window}", symbol, "D", 0.2, as_of))
        rows.append(_fv(f"volatility_hist_{window}", symbol, "D", 0.15, as_of))
        rows.append(_fv(f"volatility_expected_move_{window}", symbol, "D", 1.0, as_of))
        rows.append(_fv(f"volatility_of_volatility_{window}", symbol, "D", 0.1, as_of))

    # liquidity.py: liquid, tight-spread quote features + daily context.
    rows.append(_fv("liquidity_score", symbol, "quote", 75.0, as_of))
    rows.append(_fv("liquidity_spread_pct", symbol, "quote", 0.05, as_of))
    rows.append(_fv("liquidity_market_impact_pct", symbol, "quote", 0.1, as_of))
    rows.append(_fv("liquidity_order_book_imbalance", symbol, "quote", s * 0.3, as_of))
    for window in (5, 20, 50, 100):
        rows.append(_fv(f"liquidity_trend_{window}", symbol, "quote", 0.5, as_of))
    rows.append(_fv("liquidity_turnover_z", symbol, "quote", 0.2, as_of))
    rows.append(_fv("liquidity_delivery_pct_z", symbol, "quote", 0.2, as_of))
    rows.append(_fv("liquidity_turnover", symbol, "D", 1_000_000.0, as_of))
    rows.append(_fv("liquidity_delivery_pct", symbol, "D", 55.0, as_of))

    # breadth.py: market-wide, fixed symbol "MARKET" / timeframe "breadth".
    rows.append(_fv("breadth_strength", "MARKET", "breadth", s * 0.7, as_of))
    rows.append(_fv("breadth_participation_pct", "MARKET", "breadth", 65.0 if direction == "bullish" else 35.0, as_of))
    rows.append(_fv("breadth_trend_pct", "MARKET", "breadth", 65.0 if direction == "bullish" else 35.0, as_of))
    for window in (5, 20, 50, 100):
        rows.append(_fv(f"breadth_momentum_{window}", "MARKET", "breadth", s * 0.05, as_of))
        rows.append(_fv(f"breadth_new_high_momentum_{window}", "MARKET", "breadth", s * 3.0, as_of))
    rows.append(_fv("breadth_divergence", "MARKET", "breadth", 0.3 if direction == "bullish" else -0.3, as_of))
    rows.append(_fv("breadth_health_score", "MARKET", "breadth", 65.0 if direction == "bullish" else 35.0, as_of))

    # institutional_flow.py: market-wide, fixed symbol "MARKET" / "flow".
    for name in ("flow_fii_score", "flow_dii_score", "flow_etf_score",
                 "flow_deal_activity_score", "flow_promoter_score", "flow_insider_score"):
        rows.append(_fv(name, "MARKET", "flow", s * 0.4, as_of))
    rows.append(_fv("flow_participation_index", "MARKET", "flow", 65.0 if direction == "bullish" else 35.0, as_of))
    for window in (5, 20, 50, 100):
        rows.append(_fv(f"flow_fii_score_momentum_{window}", "MARKET", "flow", s * 0.2, as_of))

    # structure.py: markup (bullish) / markdown (bearish) structural read.
    rows.append(_fv("ms_breakout_probability", symbol, "D", 0.3, as_of))
    rows.append(_fv("ms_sweep_probability", symbol, "D", 0.1, as_of))
    rows.append(_fv("ms_change_of_character", symbol, "D", 0.0, as_of))
    rows.append(_fv("ms_break_of_structure", symbol, "D", s, as_of))

    # macro.py: 14 macro factors, all leaning the same way (India-equity-signed).
    for factor in FACTOR_UNIVERSE:
        rows.append(_fv("macro_score", factor, "macro", s * 0.5, as_of))

    # sector.py: all 12 sectors + the market-wide "SECTORS" pseudo-symbol.
    for sector in SECTOR_UNIVERSE:
        rows.append(_fv("sector_heat_score", sector, "sector", 65.0 if direction == "bullish" else 35.0, as_of))
        rows.append(_fv("sector_relative_strength", sector, "sector", s * 3.0, as_of))
        rows.append(_fv("sector_momentum", sector, "sector", s * 3.0, as_of))
        rows.append(_fv("sector_capital_rotation", sector, "sector", s * 0.3, as_of))
        # Two historical points so sector.py's leadership-change read (needs
        # >=2 rows via feature_history) has something to compare.
        rows.append(_fv("sector_leadership", sector, "sector", s * 0.5, as_of - timedelta(days=1)))
        rows.append(_fv("sector_leadership", sector, "sector", s * 1.0, as_of))
    rows.append(_fv("sector_rotation_index", "SECTORS", "sector", s * 10.0, as_of))
    rows.append(_fv("sector_participation_pct", "SECTORS", "sector", 65.0 if direction == "bullish" else 35.0, as_of))

    # events.py: calm, no imminent high-impact event (kept neutral so it
    # doesn't fight the directional read -- events are risk, not direction).
    rows.append(_fv("event_market_sensitivity", "MARKET", "events", 0.1, as_of))
    rows.append(_fv("event_hours_until_next", "MARKET", "events", 48.0, as_of))
    rows.append(_fv("event_expected_volatility", "MARKET", "events", 1.5, as_of))
    rows.append(_fv("event_category_impact", "MARKET", "events", 0.1, as_of))
    rows.append(_fv("event_confidence_reduction", "MARKET", "events", 0.0, as_of))
    rows.append(_fv("event_trading_freeze", "MARKET", "events", 0.0, as_of))
    rows.append(_fv("event_historical_similarity", "MARKET", "events", 0.8, as_of))

    # options.py: dealer positioning + PCR + max pain, same direction.
    rows.append(_fv("options_dealer_positioning", symbol, "chain", s * 0.6, as_of))
    rows.append(_fv("options_pcr", symbol, "chain", 0.7 if direction == "bullish" else 1.3, as_of))
    rows.append(_fv("options_max_pain_distance_pct", symbol, "chain", s * 3.0, as_of))
    rows.append(_fv("options_atm_iv", symbol, "chain", 18.0, as_of))
    rows.append(_fv("options_iv_rank", symbol, "chain", 50.0, as_of))
    rows.append(_fv("options_gamma_exposure", symbol, "chain", s * 0.3, as_of))

    # relative.py: outperforming (bullish) / lagging (bearish) vs all 5 refs.
    for ref in ("nifty", "sensex", "sector", "industry", "peers"):
        for window in (5, 20, 50, 100):
            rows.append(_fv(f"rs_{ref}_strength_{window}", symbol, "D", s * 3.0, as_of))
            rows.append(_fv(f"rs_{ref}_momentum_{window}", symbol, "D", s * 0.6, as_of))
    for window in (5, 20, 50, 100):
        rows.append(_fv(f"rs_outperformance_{window}", symbol, "D", 65.0 if direction == "bullish" else 35.0, as_of))
        rows.append(_fv(f"rs_percentile_rank_{window}", symbol, "D", 65.0 if direction == "bullish" else 35.0, as_of))

    return rows


async def write_ensemble_training_history(
    session_factory, symbol: str, direction: Direction,
    n_days: int = 150, daily_drift_pct: float = 0.004, noise_std_pct: float = 0.01,
) -> None:
    """Real OHLCV candles (drift + noise) + matching daily feature_store
    rows for all 30 ENSEMBLE_FEATURE_SPECS features, spanning the same
    date range -- enough for TripleBarrierLabelingEngine to produce a
    majority-win (bullish) or majority-loss (bearish) label set and for
    EnsemblePredictionEngine.train() to actually clear MIN_TRAINING_SAMPLES.

    Barriers in labeling.py are volatility-based (profit=2x, stop=1x a
    20-bar trailing stdev of log returns, floored at 0.2%/bar). A
    deterministic-but-noisy log-return path (drift dominates on average,
    so most entries resolve via the profit target, but noise is large
    enough relative to drift that some entries hit the stop first) is
    required, not just a smooth compounding drift: train_models() refuses
    to fit on a single-class label set (see
    test_ensemble_prediction.py::test_train_models_returns_nothing_with_a_single_class),
    and a near-zero-noise drift produces exactly that -- every entry
    resolves the same way via the next bar's opening gap.
    """
    s = _sign(direction)
    dates = [BASE_TS + timedelta(days=i) for i in range(n_days)]
    rng = random.Random(20260601 if direction == "bullish" else 20260602)

    candle_rows = []
    close = 1000.0
    for ts in dates:
        open_ = close
        log_return = s * daily_drift_pct + rng.gauss(0.0, noise_std_pct)
        close = open_ * math.exp(log_return)
        candle_rows.append({
            "symbol": symbol, "timeframe": "D", "ts": ts,
            "open": open_, "high": max(open_, close) * 1.002,
            "low": min(open_, close) * 0.998, "close": close,
            "volume": 1_000_000,
        })

    async with session_factory() as session:
        await session.execute(insert(OhlcvCandle), candle_rows)
        await session.commit()

    # One row per day per ensemble feature, all pointing `direction` --
    # assemble_dataset's as-of join only needs a value at-or-before each
    # label's entry_ts, so a constant per-feature level replicated daily
    # satisfies MIN_FEATURE_COVERAGE without needing realistic day-to-day
    # variation.
    feature_values: dict[str, float] = {
        "price_momentum_20": s * 5.0, "price_acceleration_20": s * 2.0,
        "price_dist_from_high_50": -1.0 if direction == "bullish" else -8.0,
        "price_dist_from_low_50": -8.0 if direction == "bullish" else -1.0,
        "volume_rvol_20": 1.8, "volume_obv_z": s * 1.2,
        "ms_trend_direction": s, "ms_structural_bias": s * 0.8,
        "ms_breakout_probability": 0.3, "ms_sweep_probability": 0.1,
        "ms_change_of_character": 0.0, "ms_break_of_structure": s,
        "volatility_regime_20": 1.0, "volatility_expected_move_20": 1.0,
        "liquidity_score": 75.0, "liquidity_spread_pct": 0.05,
        "liquidity_market_impact_pct": 0.1, "liquidity_order_book_imbalance": s * 0.3,
        "rs_nifty_strength_20": s * 3.0, "rs_outperformance_20": 65.0 if direction == "bullish" else 35.0,
        "breadth_health_score": 65.0 if direction == "bullish" else 35.0,
        "breadth_participation_pct": 65.0 if direction == "bullish" else 35.0,
        "flow_participation_index": 65.0 if direction == "bullish" else 35.0,
        "event_trading_freeze": 0.0, "event_expected_volatility": 1.5,
        "options_pcr": 0.7 if direction == "bullish" else 1.3,
        "options_max_pain_distance_pct": s * 3.0, "options_iv_rank": 50.0,
        "options_gamma_exposure": s * 0.3, "options_dealer_positioning": s * 0.6,
    }
    assert set(feature_values) == {spec[0] for spec in ENSEMBLE_FEATURE_SPECS}

    market_features = {"breadth_health_score", "breadth_participation_pct",
                        "flow_participation_index", "event_trading_freeze",
                        "event_expected_volatility"}
    rows: list[FeatureValue] = []
    for spec_name, _mode, timeframe in ENSEMBLE_FEATURE_SPECS:
        row_symbol = "MARKET" if spec_name in market_features else symbol
        value = feature_values[spec_name]
        for ts in dates:
            rows.append(_fv(spec_name, row_symbol, timeframe, value, ts))

    from app.features.store import FeatureStore
    store = FeatureStore(session_factory=session_factory)
    for start in range(0, len(rows), 500):
        await store.write(rows[start:start + 500])
