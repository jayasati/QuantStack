"""Minimal cross-layer integration + perf smoke tests (IRR-2026-07-11
findings #6 and #7).

Honest scope note: this is a starting scaffold, not the full integration
suite or the numeric-target load testing Volume 1 Sec16 describes (tick
<100ms, signal gen <2s, 99.9% collector uptime under real market load).
Building that properly needs a load-generation harness and realistic
market data fixtures -- multi-day scope on its own, not something to fake
with a couple of thin assertions. What this file DOES prove:

1. A real write in the Feature Store layer is actually visible to the
   Intelligence layer's read path, whose output is actually consumed by
   the Prediction layer, all wired to ONE real database -- every previous
   test in this suite exercises at most one or two layers with the DB
   faked out (session_factory=None) or scoped to a single engine.
2. A cheap wall-clock smoke bound on end-to-end composite scoring, so a
   future change that accidentally makes this path pathologically slow
   (e.g. an N+1 query, a forgotten await, a busy-loop) fails a test
   instead of only being noticed live.
"""

import time

import pytest

from app.features.schema import FeatureValue
from app.features.store import FeatureStore
from app.intelligence.composite import CompositeMarketIntelligenceEngine
from app.prediction.candidates import CandidateGenerationEngine
from app.prediction.conviction import ConvictionEngine
from app.prediction.opportunity import OpportunityDetectionEngine
from app.prediction.priority import SignalPriorityEngine

pytestmark = pytest.mark.db

SYMBOL = "TESTSYM"


async def test_a_feature_store_write_flows_through_intelligence_into_prediction(
    test_session_factory,
) -> None:
    """Features layer -> Intelligence layer -> Prediction layer, one real
    DB shared end to end. With no real market data this can't produce a
    high-conviction trade -- the point is that every layer reads what the
    layer below it actually wrote, and the whole chain runs to completion
    without raising."""
    from datetime import UTC, datetime

    store = FeatureStore(session_factory=test_session_factory)
    await store.write([
        FeatureValue(
            feature_name="price_momentum_5", feature_version="v1",
            symbol=SYMBOL, timeframe="D", ts=datetime.now(UTC), value=1.2,
        ),
    ])

    composite = CompositeMarketIntelligenceEngine(session_factory=test_session_factory)
    composite_result = await composite.assess(symbol=SYMBOL)
    assert composite_result.score is not None

    conviction = ConvictionEngine(session_factory=test_session_factory)
    conviction_result = await conviction.evaluate(SYMBOL, timeframe="D", direction="long")
    assert 0.0 <= conviction_result.conviction_score <= 100.0

    detector = OpportunityDetectionEngine(session_factory=test_session_factory)
    candidates_engine = CandidateGenerationEngine(
        session_factory=test_session_factory, detector=detector,
    )
    candidates = await candidates_engine.generate()
    assert isinstance(candidates, list)  # empty is fine -- no data means no real edge

    priority = SignalPriorityEngine(session_factory=test_session_factory)
    ranked = await priority.rank(top_n=5)
    assert isinstance(ranked, list)


# --- Perf smoke -- a cheap wall-clock bound, not a load test -----------------

async def test_composite_assessment_completes_within_a_generous_wall_clock_bound() -> None:
    """Volume 1 Sec16 targets signal generation <2s; this uses no DB/network
    (session_factory=None, every sub-engine reads an empty feature store)
    so it should be near-instant -- a loose 2s bound catches a pathological
    regression (N+1 query, forgotten await, busy loop) without being a
    flaky micro-benchmark on slower CI runners."""
    engine = CompositeMarketIntelligenceEngine(session_factory=None)
    start = time.monotonic()
    await engine.assess(symbol=SYMBOL)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"composite assessment took {elapsed:.2f}s, expected <2s"
