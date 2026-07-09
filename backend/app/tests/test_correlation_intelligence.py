from datetime import date, timedelta

from app.intelligence.correlation import (
    assess_correlations,
    average_daily_series,
    daily_series,
)


def _dates(n: int) -> list[str]:
    start = date(2026, 1, 1)
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


def _wave(n: int, period: int = 7, phase: int = 0, scale: float = 1.0) -> list[float]:
    return [((i + phase) % period - period // 2) * scale for i in range(n)]


def test_daily_series_keeps_most_recent_value_per_date() -> None:
    rows = [
        {"ts": "2026-01-02T15:00:00+05:30", "value": 2.0},  # newest for the 2nd
        {"ts": "2026-01-02T09:30:00+05:30", "value": 1.0},  # older, same date
        {"ts": "2026-01-01T10:00:00+05:30", "value": 5.0},
    ]
    series = daily_series(rows)
    assert series == {"2026-01-02": 2.0, "2026-01-01": 5.0}


def test_daily_series_skips_null_values_and_timestamps() -> None:
    rows: list[dict] = [
        {"ts": "2026-01-01T10:00:00+05:30", "value": None},
        {"ts": None, "value": 1.0},
        {"ts": "2026-01-02T10:00:00+05:30", "value": 3.0},
    ]
    assert daily_series(rows) == {"2026-01-02": 3.0}


def test_average_daily_series_averages_available_constituents() -> None:
    a = {"2026-01-01": 1.0, "2026-01-02": 2.0}
    b = {"2026-01-01": 3.0}  # missing 2026-01-02
    result = average_daily_series([a, b])
    assert result["2026-01-01"] == 2.0
    assert result["2026-01-02"] == 2.0  # only `a` has this date


def test_perfectly_correlated_pair_reads_highly_correlated() -> None:
    n = 90
    dates = _dates(n)
    nifty = _wave(n)
    banknifty = [v * 1.5 for v in nifty]  # scaled copy: correlation +1
    result = assess_correlations({
        "NIFTY": dict(zip(dates, nifty, strict=True)),
        "BANKNIFTY": dict(zip(dates, banknifty, strict=True)),
    })
    assert result.metrics["correlation_matrix"]["NIFTY"]["BANKNIFTY"] > 0.95
    assert result.metrics["mean_absolute_correlation"] > 0.95
    dominant = max(result.states, key=lambda s: result.states[s])
    assert dominant == "highly_correlated"


def test_perfectly_anticorrelated_pair_shows_negative_matrix_entry() -> None:
    n = 90
    dates = _dates(n)
    nifty = _wave(n)
    usdinr = [-v for v in nifty]  # correlation -1
    result = assess_correlations({
        "NIFTY": dict(zip(dates, nifty, strict=True)),
        "USDINR": dict(zip(dates, usdinr, strict=True)),
    })
    assert result.metrics["correlation_matrix"]["NIFTY"]["USDINR"] < -0.95
    # Anti-correlation is still "correlated" in magnitude, not decorrelated.
    assert result.metrics["mean_absolute_correlation"] > 0.95


def test_matrix_is_symmetric_with_unit_diagonal() -> None:
    n = 90
    dates = _dates(n)
    nifty = _wave(n)
    gold = _wave(n, period=5, phase=2)
    result = assess_correlations({
        "NIFTY": dict(zip(dates, nifty, strict=True)),
        "GOLD": dict(zip(dates, gold, strict=True)),
    })
    matrix = result.metrics["correlation_matrix"]
    assert matrix["NIFTY"]["NIFTY"] == 1.0
    assert matrix["GOLD"]["GOLD"] == 1.0
    assert matrix["NIFTY"]["GOLD"] == matrix["GOLD"]["NIFTY"]


def test_correlation_breakdown_detected_when_recent_window_flips_sign() -> None:
    n = 90
    dates = _dates(n)
    nifty = _wave(n)
    # Correlated for the first n-20 days, then flips sign for the most
    # recent 20 (the short window) — long and short window correlations
    # should diverge past the breakdown threshold.
    banknifty = [
        1.5 * v if i < n - 20 else -1.5 * v for i, v in enumerate(nifty)
    ]
    result = assess_correlations({
        "NIFTY": dict(zip(dates, nifty, strict=True)),
        "BANKNIFTY": dict(zip(dates, banknifty, strict=True)),
    })
    assert "NIFTY-BANKNIFTY" in result.metrics["correlation_breakdown"]
    assert result.metrics["correlation_stability"] < 0.5


def test_states_sum_to_one() -> None:
    n = 90
    dates = _dates(n)
    nifty = _wave(n)
    result = assess_correlations({"NIFTY": dict(zip(dates, nifty, strict=True))})
    assert abs(sum(result.states.values()) - 1.0) < 1e-9


def test_single_asset_has_no_pairs_and_low_confidence() -> None:
    n = 90
    dates = _dates(n)
    result = assess_correlations({
        "NIFTY": dict(zip(dates, _wave(n), strict=True)),
    })
    assert result.metrics["correlation_matrix"] == {"NIFTY": {"NIFTY": 1.0}}
    assert result.confidence < 0.4


def test_no_data_defaults_to_zero_risk_and_low_confidence() -> None:
    result = assess_correlations({})
    assert result.score == 0.0
    assert result.confidence < 0.3
    assert result.metrics["correlation_matrix"] == {}


def test_more_assets_and_history_increase_confidence() -> None:
    n = 90
    dates = _dates(n)
    sparse = assess_correlations({"NIFTY": dict(zip(dates, _wave(n), strict=True))})
    rich = assess_correlations({
        "NIFTY": dict(zip(dates, _wave(n), strict=True)),
        "BANKNIFTY": dict(zip(dates, [1.5 * v for v in _wave(n)], strict=True)),
        "GOLD": dict(zip(dates, _wave(n, period=5, phase=2), strict=True)),
    })
    assert rich.confidence > sparse.confidence
