import random
from datetime import UTC, datetime, timedelta

from app.features.drift import THRESHOLDS, detect_series_drift

NOW = datetime(2026, 7, 7, tzinfo=UTC)


def series(values: list[float], gap_after: int | None = None,
           gap_days: float = 0.0) -> list[tuple[datetime, float]]:
    """Daily observations; optionally stretch spacing after an index (cadence drop)."""
    observations = []
    ts = NOW - timedelta(days=len(values) * 2)
    for i, v in enumerate(values):
        step = 1.0
        if gap_after is not None and i >= gap_after:
            step = 1.0 + gap_days
        ts = ts + timedelta(days=step)
        observations.append((ts, v))
    return observations


def by_metric(results):
    return {r.metric: r for r in results}


def stable_values(n: int = 300) -> list[float]:
    rng = random.Random(3)
    return [rng.gauss(0, 1) for _ in range(n)]


def test_stable_series_breaches_nothing() -> None:
    results = detect_series_drift("f", "NIFTY", "D", series(stable_values()))
    assert results
    metrics = by_metric(results)
    for name in ("ks_statistic", "psi", "jensen_shannon", "population_shift"):
        assert not metrics[name].breached, name
    assert not metrics["cadence_ratio"].breached


def test_regime_change_breaches_distribution_metrics() -> None:
    rng = random.Random(5)
    values = [rng.gauss(0, 1) for _ in range(250)] + [rng.gauss(4, 1) for _ in range(100)]
    metrics = by_metric(detect_series_drift("f", "NIFTY", "D", series(values)))
    assert metrics["ks_statistic"].breached
    assert metrics["psi"].breached
    assert metrics["jensen_shannon"].breached
    assert metrics["population_shift"].breached


def test_missing_pattern_drift_on_cadence_drop() -> None:
    values = stable_values(300)
    # After index 200 (inside the recent window), observations arrive 4x slower.
    observations = series(values, gap_after=200, gap_days=3.0)
    metrics = by_metric(detect_series_drift("f", "NIFTY", "D", observations))
    assert metrics["cadence_ratio"].breached
    assert metrics["cadence_ratio"].value < THRESHOLDS["cadence_ratio"]


def test_concept_drift_when_correlation_flips() -> None:
    values = stable_values(300)
    observations = series(values)
    forward = {}
    for i, (ts, v) in enumerate(observations):
        # Feature predicts returns early on, then the relationship inverts.
        forward[ts] = v * 0.01 if i < 200 else -v * 0.01
    metrics = by_metric(
        detect_series_drift("f", "NIFTY", "D", observations, forward_returns=forward)
    )
    assert metrics["concept_shift"].breached
    assert metrics["concept_shift"].value > 1.5  # correlation flipped +1 -> -1


def test_short_series_yields_no_detections() -> None:
    assert detect_series_drift("f", "NIFTY", "D", series(stable_values(40))) == []
