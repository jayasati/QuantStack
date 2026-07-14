"""Scenario-based cross-layer integration tests (IRR-2026-07-11 finding #6,
full follow-up to test_cross_layer_integration.py's initial scaffold).

Uses market_scenarios.py's fixture builders to seed real, direction-
differentiated feature_store (and for the ensemble-training scenario,
ohlcv_candles) rows against a real Postgres, then runs the actual
intelligence -> prediction chain and asserts the OUTPUT is coherent with
the input -- not just "didn't crash," which is all test_cross_layer_
integration.py's original scaffold proved.

Honest scope note (carried over from the scaffold): this still isn't the
numeric-target load testing Volume 1 Sec16 describes, and it covers one
symbol's worth of scenarios, not a market-wide fuzz sweep. What it adds
over the scaffold: real bullish/bearish/mixed/degraded-data differentiation,
a multi-symbol correlation scenario, a real trained-ensemble scenario, and
one rejection-path scenario in TradeQualificationEngine.
"""

import pytest

from app.features.schema import FeatureValue
from app.features.store import FeatureStore
from app.intelligence.composite import CompositeMarketIntelligenceEngine
from app.intelligence.correlation import CorrelationIntelligenceEngine
from app.prediction.conviction import ConvictionEngine
from app.prediction.ensemble import EnsemblePredictionEngine
from app.prediction.qualification import TradeQualificationEngine

from .market_scenarios import (
    BASE_TS,
    snapshot_rows,
    write_ensemble_training_history,
)

pytestmark = pytest.mark.db

SYMBOL = "TESTSYM"


async def _seed(test_session_factory, direction, symbol=SYMBOL) -> None:
    store = FeatureStore(session_factory=test_session_factory)
    rows = snapshot_rows(symbol, direction)
    for start in range(0, len(rows), 500):
        await store.write(rows[start:start + 500])


async def test_bullish_snapshot_produces_a_clearly_bullish_composite_score(
    test_session_factory,
) -> None:
    await _seed(test_session_factory, "bullish")
    composite = CompositeMarketIntelligenceEngine(session_factory=test_session_factory)
    result = await composite.assess(symbol=SYMBOL)
    assert result.score > 65.0, f"expected a clear bullish tilt, got {result.score}"
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "bullish"


async def test_bearish_snapshot_produces_a_clearly_bearish_composite_score(
    test_session_factory,
) -> None:
    await _seed(test_session_factory, "bearish")
    composite = CompositeMarketIntelligenceEngine(session_factory=test_session_factory)
    result = await composite.assess(symbol=SYMBOL)
    assert result.score < 35.0, f"expected a clear bearish tilt, got {result.score}"
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "bearish"


async def test_bullish_snapshot_gives_conviction_a_bullish_tilt(test_session_factory) -> None:
    """Without real OHLCV history, ~55% of ConvictionEngine's weight
    (calibrated_probability + market_context) stays neutral -- this only
    asserts the feature-only evidence (institutional_flow, market_structure,
    sector strength via relative.py, historical analog) is enough to move
    conviction meaningfully above neutral, not that it reaches an extreme."""
    await _seed(test_session_factory, "bullish")
    conviction = ConvictionEngine(session_factory=test_session_factory)
    result = await conviction.evaluate(SYMBOL, timeframe="D", direction="long")
    assert result.conviction_score > 52.0, (
        f"expected a real (if muted) bullish tilt, got {result.conviction_score}"
    )


async def test_mixed_conflicting_signals_lower_conviction_vs_coherent_bullish(
    test_session_factory,
) -> None:
    """Same bullish base, but institutional flow and market structure --
    two of conviction's feature-only (ensemble-independent) evidence
    sources -- are flipped bearish. A coherent read should score higher
    than a conflicted one."""
    store = FeatureStore(session_factory=test_session_factory)
    bullish_rows = snapshot_rows(SYMBOL, "bullish")
    await store.write(bullish_rows)
    coherent_composite = await CompositeMarketIntelligenceEngine(
        session_factory=test_session_factory
    ).assess(symbol=SYMBOL)

    conflicting_overrides = [
        FeatureValue(feature_name=name, feature_version="v1", symbol="MARKET",
                      timeframe="flow", ts=BASE_TS, value=-0.4)
        for name in ("flow_fii_score", "flow_dii_score", "flow_etf_score",
                      "flow_deal_activity_score", "flow_promoter_score", "flow_insider_score")
    ] + [
        FeatureValue(feature_name="ms_structural_bias", feature_version="v1", symbol=SYMBOL,
                      timeframe="D", ts=BASE_TS, value=-0.8),
        FeatureValue(feature_name="ms_trend_direction", feature_version="v1", symbol=SYMBOL,
                      timeframe="D", ts=BASE_TS, value=-1.0),
        FeatureValue(feature_name="ms_break_of_structure", feature_version="v1", symbol=SYMBOL,
                      timeframe="D", ts=BASE_TS, value=-1.0),
    ]
    await store.write(conflicting_overrides)
    mixed_composite = await CompositeMarketIntelligenceEngine(
        session_factory=test_session_factory
    ).assess(symbol=SYMBOL)

    assert mixed_composite.score < coherent_composite.score
    # expected_opportunity = 100 * |overall_level| * stability -- conflicting
    # evidence pulls overall_level toward zero, so this should shrink too.
    assert mixed_composite.metrics["expected_opportunity"] < coherent_composite.metrics["expected_opportunity"]


async def test_missing_data_degrades_confidence_but_does_not_crash(test_session_factory) -> None:
    """No feature rows anywhere -- not just for this symbol, for the whole
    DB. Six of the twelve components key off the queried symbol directly
    (trend/volatility/liquidity/market_structure/options/momentum) and
    degrade to a neutral read with no data for it. The other six
    (breadth/macro/sector/institutional_flow/events/correlation) are
    market-wide/fixed-pseudo-symbol reads that don't key off the queried
    symbol AT ALL -- they report their own engine-level empty-data default
    (not None), which is why this is a materially different scenario from
    test_composite_market_intelligence.py's pure-function
    test_no_components_defaults_to_neutral_with_zero_confidence (which
    simulates every component() call itself raising/returning nothing,
    not "the DB has no rows"). The real, correct claim here is narrower:
    the composite still resolves to a defined neutral score and doesn't
    raise, with all 12 components technically "present" (none actually
    failed) even though none of them found anything meaningful."""
    composite = CompositeMarketIntelligenceEngine(session_factory=test_session_factory)
    result = await composite.assess(symbol="SYMBOL_WITH_NO_DATA_AT_ALL")
    assert result.score == 50.0
    assert result.metrics["components_present"] == 12


async def test_correlation_engine_detects_concentration_among_co_moving_assets(
    test_session_factory,
) -> None:
    """60+ days of NIFTY/BANKNIFTY daily returns that move in lockstep --
    Risk Concentration (mean|correlation|) should read high, not the
    near-zero/undefined read an empty or randomly-scattered feature store
    would produce."""
    from datetime import timedelta

    store = FeatureStore(session_factory=test_session_factory)
    rows = []
    for i in range(65):
        ts = BASE_TS + timedelta(days=i)
        daily_return = 0.01 if i % 2 == 0 else -0.008  # same sign both symbols, every day
        rows.append(FeatureValue(
            feature_name="price_simple_return", feature_version="v1",
            symbol="NIFTY", timeframe="D", ts=ts, value=daily_return,
        ))
        rows.append(FeatureValue(
            feature_name="price_simple_return", feature_version="v1",
            symbol="BANKNIFTY", timeframe="D", ts=ts, value=daily_return * 1.1,
        ))
    await store.write(rows)

    correlation = CorrelationIntelligenceEngine(session_factory=test_session_factory)
    result = await correlation.assess()
    assert result.score > 50.0, (
        f"expected high concentration from lockstep NIFTY/BANKNIFTY returns, got {result.score}"
    )


async def test_full_pipeline_with_real_ohlcv_history_trains_the_ensemble(
    test_session_factory,
) -> None:
    """The scaffold test in test_cross_layer_integration.py only proves the
    chain doesn't crash with zero training data. This seeds real OHLCV
    history + matching daily feature rows so EnsemblePredictionEngine
    actually clears MIN_TRAINING_SAMPLES and produces a genuinely trained
    (not 'untrained-n0') model, which is what unlocks non-neutral output
    from calibration/market_context/model agreement downstream."""
    await write_ensemble_training_history(test_session_factory, SYMBOL, "bullish")

    ensemble = EnsemblePredictionEngine(session_factory=test_session_factory)
    training = await ensemble.train(SYMBOL, timeframe="D", direction="long")
    assert training.is_trained, f"expected a trained ensemble, got n_samples={training.n_samples}"
    assert training.n_samples >= 40

    prediction = await ensemble.predict(SYMBOL, timeframe="D", direction="long")
    assert prediction.probability > 0.5, (
        f"expected the trained ensemble to lean bullish, got probability={prediction.probability}"
    )


async def test_low_liquidity_snapshot_causes_qualification_to_reject(test_session_factory) -> None:
    await _seed(test_session_factory, "bullish")
    store = FeatureStore(session_factory=test_session_factory)
    await store.write([
        FeatureValue(feature_name="liquidity_score", feature_version="v1", symbol=SYMBOL,
                      timeframe="quote", ts=BASE_TS, value=5.0),
        FeatureValue(feature_name="liquidity_spread_pct", feature_version="v1", symbol=SYMBOL,
                      timeframe="quote", ts=BASE_TS, value=5.0),
    ])

    qualification = TradeQualificationEngine(session_factory=test_session_factory)
    result = await qualification.evaluate(SYMBOL, timeframe="D", direction="long")
    assert result.qualified is False
    assert any("liquidity" in reason.lower() for reason in result.rejection_reasons)
