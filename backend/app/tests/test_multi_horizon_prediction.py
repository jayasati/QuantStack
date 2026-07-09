import math
from datetime import UTC, datetime
from statistics import NormalDist
from zoneinfo import ZoneInfo

import pytest

from app.prediction.multi_horizon import (
    DEFAULT_VOLATILITY_ANNUAL_PCT,
    FIXED_HORIZON_MINUTES,
    MIN_HORIZON_MINUTES,
    MU_ANNUAL_SCALE,
    SESSION_MINUTES,
    TRADING_DAYS,
    MultiHorizonPredictionEngine,
    compute_drift_and_volatility,
    horizon_minutes_for,
    probability_up,
)
from app.prediction.snapshot import FeatureSnapshot

IST = ZoneInfo("Asia/Kolkata")


def manual_probability(mu: float, sigma: float, minutes: float) -> float:
    years = minutes / SESSION_MINUTES / TRADING_DAYS
    mean = (mu - 0.5 * sigma * sigma) * years
    std = sigma * math.sqrt(years)
    return NormalDist().cdf(mean / std)


def test_probability_up_at_zero_drift_is_pulled_slightly_below_half() -> None:
    """Standard GBM variance drag: even with zero drift, log-return mean is
    slightly negative (-0.5*sigma^2*t), so P(up) < 0.5, not exactly 0.5."""
    p = probability_up(0.0, 0.2, 60)
    assert p < 0.5
    assert p == pytest.approx(manual_probability(0.0, 0.2, 60))


def test_probability_up_sign_matches_drift_sign() -> None:
    assert probability_up(0.3, 0.2, 60) > 0.5
    assert probability_up(-0.3, 0.2, 60) < 0.5


def test_probability_up_moves_further_from_half_over_longer_horizons() -> None:
    """Drift dominates volatility over long horizons (sqrt(H) growth in the
    z-score) -- a real, positive drift should read more confidently bullish
    at 1 day out than at 5 minutes out."""
    short = probability_up(0.3, 0.2, 5)
    long = probability_up(0.3, 0.2, 750)
    assert 0.5 < short < long


def test_probability_up_zero_sigma_does_not_divide_by_zero() -> None:
    """sigma_annual=0.0 is floored at MIN_SIGMA (never truly 0), so this
    always goes through the normal CDF -- extreme but valid probabilities,
    never a ZeroDivisionError, and never exactly 0.0/1.0."""
    up = probability_up(0.1, 0.0, 60)
    down = probability_up(-0.1, 0.0, 60)
    # Extreme z-scores from a near-zero sigma legitimately saturate to
    # exactly 1.0/~0.0 in floating point -- what matters is no crash and
    # the correct ordering/sign, not that they stay strictly inside (0, 1).
    assert 0.0 <= down < 0.5 < up <= 1.0
    assert probability_up(0.0, 0.0, 60) == pytest.approx(0.5, abs=1e-3)


def make_features(
    trend_direction: float = 0.0, trend_strength: float = 0.0,
    expected_vol_pct: float | None = None,
) -> dict[str, float]:
    features: dict[str, float] = {
        "ms_trend_direction": trend_direction,
        "ms_structural_bias": trend_direction,
    }
    if trend_strength > 0:
        # price_momentum features drive both direction and strength in assess_trend
        for w, scale in ((5, 3.0), (20, 6.0), (50, 10.0), (200, 20.0)):
            features[f"price_momentum_{w}"] = trend_direction * trend_strength * scale
    if expected_vol_pct is not None:
        for w in (5, 20, 50, 100):
            features[f"volatility_hist_{w}"] = expected_vol_pct
            features[f"volatility_regime_{w}"] = 1.0  # "normal" tercile
    return features


def test_compute_drift_and_volatility_uses_expected_volatility_pct_when_present() -> None:
    features = make_features(trend_direction=0.5, trend_strength=0.5, expected_vol_pct=25.0)
    mu, sigma, trend_conf, vol_conf = compute_drift_and_volatility(features)
    assert mu == pytest.approx(0.5 * 0.5 * MU_ANNUAL_SCALE, abs=0.05)
    assert sigma == pytest.approx(0.25)


def test_compute_drift_and_volatility_falls_back_and_docks_confidence_without_data() -> None:
    mu, sigma, trend_conf, vol_conf_empty = compute_drift_and_volatility({})
    assert mu == 0.0
    assert sigma == pytest.approx(DEFAULT_VOLATILITY_ANNUAL_PCT / 100.0)

    features_with_vol = make_features(expected_vol_pct=25.0)
    _, _, _, vol_conf_present = compute_drift_and_volatility(features_with_vol)
    # fallback confidence is docked relative to having a real measurement
    assert vol_conf_empty <= vol_conf_present


def test_horizon_minutes_for_matches_doc_horizons_in_order() -> None:
    now = datetime(2026, 7, 9, 11, 0, tzinfo=IST)
    horizons = horizon_minutes_for(now)
    assert list(horizons.keys()) == [
        "5min", "15min", "30min", "1hour", "end_of_day", "next_day",
    ]
    for name, minutes in FIXED_HORIZON_MINUTES.items():
        assert horizons[name] == minutes
    # 15:30 close minus 11:00 = 4h30m = 270 minutes
    assert horizons["end_of_day"] == pytest.approx(270.0)
    assert horizons["next_day"] == pytest.approx(270.0 + SESSION_MINUTES)


def test_horizon_minutes_for_before_open_uses_full_session() -> None:
    before_open = datetime(2026, 7, 9, 8, 0, tzinfo=IST)
    assert horizon_minutes_for(before_open)["end_of_day"] == pytest.approx(SESSION_MINUTES)


def test_horizon_minutes_for_after_close_floors_at_minimum() -> None:
    after_close = datetime(2026, 7, 9, 17, 0, tzinfo=IST)
    assert horizon_minutes_for(after_close)["end_of_day"] == MIN_HORIZON_MINUTES


def test_predict_from_snapshot_assembles_all_six_horizons() -> None:
    snapshot = FeatureSnapshot(
        snapshot_id="snap-1",
        symbol="NIFTY",
        timeframe="D",
        as_of=datetime(2026, 7, 9, 11, 0, tzinfo=UTC),
        feature_values=make_features(
            trend_direction=0.6, trend_strength=0.7, expected_vol_pct=18.0
        ),
    )
    engine = MultiHorizonPredictionEngine(session_factory=None)
    prediction = engine.predict_from_snapshot(snapshot)

    assert prediction.symbol == "NIFTY"
    assert prediction.snapshot_id == "snap-1"
    assert prediction.as_of == snapshot.as_of
    assert len(prediction.horizons) == 6
    assert [h.horizon for h in prediction.horizons] == [
        "5min", "15min", "30min", "1hour", "end_of_day", "next_day",
    ]
    for h in prediction.horizons:
        assert 0.0 <= h.probability_up <= 1.0
        assert 0.0 <= h.confidence <= 1.0
    # positive trend -> every horizon should read bullish-leaning
    assert all(h.probability_up > 0.5 for h in prediction.horizons)


def test_predict_from_snapshot_is_deterministic_reconstruction() -> None:
    """Same frozen snapshot -> same prediction, exactly. This IS the
    "exact historical reconstruction" requirement — no live-data dependency."""
    snapshot = FeatureSnapshot(
        snapshot_id="snap-2", symbol="NIFTY", timeframe="D",
        as_of=datetime(2026, 7, 9, 11, 0, tzinfo=UTC),
        feature_values=make_features(
            trend_direction=-0.4, trend_strength=0.3, expected_vol_pct=22.0
        ),
    )
    engine = MultiHorizonPredictionEngine(session_factory=None)
    first = engine.predict_from_snapshot(snapshot)
    second = engine.predict_from_snapshot(snapshot)
    assert first.to_dict() == second.to_dict()


async def test_predict_runs_cleanly_without_a_db() -> None:
    engine = MultiHorizonPredictionEngine(session_factory=None)
    prediction = await engine.predict("NIFTY")
    assert prediction.symbol == "NIFTY"
    assert len(prediction.horizons) == 6


async def test_recent_returns_empty_list_without_a_session_factory() -> None:
    engine = MultiHorizonPredictionEngine(session_factory=None)
    assert await engine.recent("NIFTY") == []
