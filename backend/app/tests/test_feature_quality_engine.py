import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from app.features.quality import PSI_WARNING_THRESHOLD, evaluate_series
from app.features.stats import (
    jensen_shannon,
    ks_statistic,
    lag1_autocorrelation,
    population_shift,
    psi,
)

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def series(values: list[float], days_ago_last: float = 0.5):
    start = NOW - timedelta(days=days_ago_last + len(values) - 1)
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


def wave(n: int = 100, amplitude: float = 1.0) -> list[float]:
    """Stationary periodic series: period 20 so equal halves match distributionally."""
    return [
        amplitude * math.sin(i * math.pi / 10) + 0.05 * ((i % 5) - 2)
        for i in range(n)
    ]


# --- statistics ------------------------------------------------------------------


def test_ks_zero_for_identical_and_high_for_disjoint() -> None:
    sample = [float(i) for i in range(50)]
    assert ks_statistic(sample, list(sample)) == pytest.approx(0.0)
    shifted = [v + 1000 for v in sample]
    assert val(ks_statistic(sample, shifted)) == pytest.approx(1.0)


def val(x: float | None) -> float:
    assert x is not None
    return x


def test_psi_detects_mean_shift() -> None:
    rng = random.Random(7)
    reference = [rng.gauss(0, 1) for _ in range(500)]
    same = [rng.gauss(0, 1) for _ in range(500)]
    shifted = [rng.gauss(2, 1) for _ in range(500)]
    assert val(psi(reference, same)) < 0.1
    assert val(psi(reference, shifted)) > 0.25


def test_jensen_shannon_bounded_and_ordered() -> None:
    rng = random.Random(11)
    reference = [rng.gauss(0, 1) for _ in range(400)]
    near = [rng.gauss(0.2, 1) for _ in range(400)]
    far = [rng.gauss(3, 1) for _ in range(400)]
    js_near = val(jensen_shannon(reference, near))
    js_far = val(jensen_shannon(reference, far))
    assert 0.0 <= js_near < js_far <= 1.0


def test_population_shift_in_sigma_units() -> None:
    reference = [float(i % 10) for i in range(100)]
    recent = [v + 5.0 for v in reference]  # same shape, mean shifted 5
    shift = val(population_shift(reference, recent))
    std = (sum((v - 4.5) ** 2 for v in reference) / 100) ** 0.5
    assert shift == pytest.approx(5.0 / std)


def test_lag1_autocorrelation_signatures() -> None:
    trending = [float(i) for i in range(50)]
    assert val(lag1_autocorrelation(trending)) > 0.9
    alternating = [1.0 if i % 2 else -1.0 for i in range(50)]
    assert val(lag1_autocorrelation(alternating)) < -0.9


# --- quality evaluation ------------------------------------------------------------


def test_healthy_feature_scores_high() -> None:
    report = evaluate_series(
        "feat", "NIFTY", "D", series(wave(120)), group_max_count=120, now=NOW
    )
    assert report is not None
    assert report.quality_score > 70
    assert not report.drift_warning
    assert 0.1 <= report.confidence_multiplier <= 1.0
    assert report.components["freshness"] == 100.0
    assert report.components["variance"] == 100.0


def test_stale_feature_loses_freshness() -> None:
    fresh = evaluate_series("f", "NIFTY", "D", series(wave(100), days_ago_last=1),
                            group_max_count=100, now=NOW)
    stale = evaluate_series("f", "NIFTY", "D", series(wave(100), days_ago_last=15),
                            group_max_count=100, now=NOW)
    assert val_report(stale).components["freshness"] < val_report(fresh).components["freshness"]
    assert val_report(stale).quality_score < val_report(fresh).quality_score


def val_report(report):
    assert report is not None
    return report


def test_drift_warning_on_regime_change() -> None:
    values = [0.0 + 0.01 * (i % 5) for i in range(60)] + [
        5.0 + 0.01 * (i % 5) for i in range(60)
    ]
    report = evaluate_series("f", "NIFTY", "D", series(values),
                             group_max_count=120, now=NOW)
    assert val_report(report).drift_warning
    assert val_report(report).components["distribution_stability"] < 50


def test_constant_feature_flagged_uninformative() -> None:
    report = evaluate_series("f", "NIFTY", "D", series([42.0] * 100),
                             group_max_count=100, now=NOW)
    assert val_report(report).components["variance"] == 0.0


def test_incomplete_feature_penalized() -> None:
    full = evaluate_series("f", "NIFTY", "D", series(wave(100)),
                           group_max_count=100, now=NOW)
    sparse = evaluate_series("f", "NIFTY", "D", series(wave(40)),
                             group_max_count=100, now=NOW)
    assert val_report(sparse).components["completeness"] == pytest.approx(40.0)
    assert val_report(sparse).components["missing_pct"] == pytest.approx(60.0)
    assert val_report(full).components["completeness"] == pytest.approx(100.0)


def test_predictive_power_rewards_correlated_feature() -> None:
    observations = series(wave(100))
    # Feature value at t exactly anticipates the next-bar return.
    forward = {ts: v / 10 for ts, v in observations}
    predictive = evaluate_series("f", "NIFTY", "D", observations, 100,
                                 forward_returns=forward, now=NOW)
    noise = {ts: ((i * 104729) % 17 - 8) / 10 for i, (ts, _) in enumerate(observations)}
    unpredictive = evaluate_series("f", "NIFTY", "D", observations, 100,
                                   forward_returns=noise, now=NOW)
    assert val_report(predictive).components["predictive_power"] == pytest.approx(100.0)
    assert val_report(predictive).quality_score > val_report(unpredictive).quality_score


def test_short_series_returns_none() -> None:
    assert evaluate_series("f", "NIFTY", "D", series(wave(5)), 100, now=NOW) is None


def test_psi_threshold_constant_documented() -> None:
    assert PSI_WARNING_THRESHOLD == 0.25
