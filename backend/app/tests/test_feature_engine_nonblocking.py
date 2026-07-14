"""Regression test for BaseFeatureEngine.run()'s asyncio.to_thread offload.

Found live (2026-07-14) via py-spy attached to the production process:
VolatilityFeatureEngine's scheduled run_all() job was caught with the
MainThread stuck inside statistics.pstdev() (called from normalize.py's
rolling_zscore, itself O(n*window) -- it recomputes the mean/stdev from
scratch for every index rather than an incremental rolling calculation)
for seconds at a time. Since _compute() used to be called directly (not
via to_thread), that blocked the single asyncio event loop entirely --
stalling every other concurrent request, including completely unrelated
ones like /prediction/candidates, for the duration. This is the exact
same class of bug already fixed once this session for
EnsemblePredictionEngine.train() (IRR Critical #1) -- see
test_ensemble_nonblocking.py for that one's own version of this test.

BaseFeatureEngine is the shared base class for all 16 feature engines, so
this test targets it directly with a synthetic slow _compute() rather
than relying on volatility.py's real (also now-fixed) cost profile.
"""

import asyncio
import contextlib
import time
from datetime import UTC, datetime, timedelta

from app.features.base import BaseFeatureEngine
from app.features.schema import Candle, FeatureDefinition, Series

SLOW_COMPUTE_SECONDS = 0.3


class _SlowFeatureEngine(BaseFeatureEngine):
    name = "slow_test_engine"
    category = "test"

    def _definitions(self) -> list[FeatureDefinition]:
        return []

    def _compute(self, candles, benchmark=None) -> dict[str, Series]:
        time.sleep(SLOW_COMPUTE_SECONDS)  # stands in for a genuinely slow rolling calc
        return {"test_feature": [1.0 for _ in candles]}


def _synthetic_candles(n: int = 10) -> list[Candle]:
    base = datetime(2026, 7, 1, tzinfo=UTC)
    return [
        Candle(ts=base + timedelta(days=i), open=100.0, high=101.0, low=99.0, close=100.5)
        for i in range(n)
    ]


async def test_run_offloads_compute_and_does_not_block_the_event_loop(monkeypatch) -> None:
    engine = _SlowFeatureEngine(session_factory=None)

    async def fake_load_candles(symbol: str, timeframe: str) -> list[Candle]:
        return _synthetic_candles()

    monkeypatch.setattr(engine, "_load_candles", fake_load_candles)

    heartbeat_times: list[float] = []

    async def heartbeat_recorder() -> None:
        while True:
            heartbeat_times.append(time.monotonic())
            await asyncio.sleep(0.02)

    start = time.monotonic()
    heartbeat_task = asyncio.create_task(heartbeat_recorder())
    try:
        result = await engine.run("TESTSYM", "D")
        elapsed = time.monotonic() - start
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task

    assert "skipped" not in result  # actually ran _compute, not the <2-candle shortcut
    assert elapsed >= SLOW_COMPUTE_SECONDS

    # The gap to check is BEFORE the first recorded heartbeat, not just
    # between later ones: a fully blocked event loop gives the heartbeat
    # task zero chances to run until _compute()'s time.sleep() finally
    # releases it, so every gap *after* that point looks perfectly healthy
    # (confirmed empirically -- an earlier, broken version of this test
    # only checked inter-heartbeat gaps and passed even with to_thread
    # removed, because the damage is entirely in the start-to-first-tick
    # gap). Prepending `start` makes that gap visible.
    assert len(heartbeat_times) > 5
    gaps = [b - a for a, b in zip([start, *heartbeat_times], heartbeat_times)]
    max_gap = max(gaps)
    assert max_gap < SLOW_COMPUTE_SECONDS / 2, (
        f"largest gap before/between heartbeats was {max_gap:.3f}s (compute "
        f"took {SLOW_COMPUTE_SECONDS}s) -- the event loop was starved for a "
        "chunk of that time, suggesting asyncio.to_thread was bypassed"
    )
