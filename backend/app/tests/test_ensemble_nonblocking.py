"""Regression test for IRR Critical #1's asyncio.to_thread fix in
EnsemblePredictionEngine.train() (IRR-2026-07-11 finding #5).

Before this file, the only async-level test touching train() was
test_ensemble_prediction.py::test_train_without_a_db_returns_an_untrained_result,
which short-circuits on the MIN_TRAINING_SAMPLES gate with an empty rows
list and never reaches the `asyncio.to_thread(_fit_and_calibrate, ...)`
line at all. If someone accidentally removed the to_thread wrapper --
reintroducing the exact "training freezes the event loop" bug `1f73829`
fixed -- no existing test would fail. This test forces training to
actually run (monkeypatching assemble_dataset to hand it enough real,
separable rows to clear MIN_TRAINING_SAMPLES) and proves the event loop
stays responsive throughout, the same forced-yield concurrency pattern
already used for IRR Critical #2's fix (see test_lifecycle.py).
"""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

from app.prediction import ensemble as ensemble_module
from app.prediction.ensemble import MIN_TRAINING_SAMPLES, EnsemblePredictionEngine, TrainingRow

BASE_TS = datetime(2026, 7, 1, tzinfo=UTC)


def _separable_rows(n: int) -> list[TrainingRow]:
    """A real, separable dataset -- large enough that fitting six models on
    it takes measurable wall-clock time, which is the whole point: too
    small and the event loop never gets a chance to starve even without
    the to_thread wrapper, making the test unable to catch a regression."""
    rows = []
    for i in range(n):
        signal = 1.0 if i % 2 == 0 else -1.0
        rows.append(TrainingRow(
            ts=BASE_TS + timedelta(minutes=i),
            features={"signal": signal, "noise": float(i % 7)},
            label=1 if signal > 0 else 0,
        ))
    return rows


async def test_train_reaches_to_thread_and_does_not_block_the_event_loop(monkeypatch) -> None:
    rows = _separable_rows(600)
    assert len(rows) >= MIN_TRAINING_SAMPLES  # sanity: this test must actually exercise to_thread
    monkeypatch.setattr(ensemble_module, "assemble_dataset", lambda *a, **k: rows)

    engine = EnsemblePredictionEngine(session_factory=None)

    heartbeats = 0

    async def heartbeat_counter() -> None:
        nonlocal heartbeats
        while True:
            heartbeats += 1
            await asyncio.sleep(0.005)

    heartbeat_task = asyncio.create_task(heartbeat_counter())
    try:
        training = await engine.train("TESTSYM")
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task

    # Training actually ran (not the MIN_TRAINING_SAMPLES short-circuit).
    assert training.is_trained
    assert training.n_samples == 600

    # If asyncio.to_thread were removed, _fit_and_calibrate's six
    # synchronous model.fit() calls would run directly on the event loop
    # and the heartbeat coroutine -- which never gets control back until
    # training fully returns -- would tick at most once (whatever ran
    # before `await engine.train(...)` yielded control the first time).
    # A healthy to_thread offload lets the loop keep servicing the 5ms
    # heartbeat throughout, so this should be many.
    assert heartbeats > 5, (
        f"only {heartbeats} heartbeats ticked during training -- the event "
        "loop was starved, suggesting asyncio.to_thread was bypassed"
    )
