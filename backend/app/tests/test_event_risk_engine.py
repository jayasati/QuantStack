from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.features.events import (
    EventRiskEngine,
    EventSnapshot,
    bucket_event_observations,
    compute_event_features,
)

BASE_TS = datetime(2026, 7, 7, 9, 0, tzinfo=UTC)


def val(x: float | None) -> float:
    assert x is not None
    return x


def event_meta(kind: str, hours: float, impact: str = "high",
               in_window: bool = False, multiplier: float = 2.0) -> dict:
    return {
        "record_type": "event",
        "kind": kind,
        "expected_impact": impact,
        "hours_until_event": hours,
        "in_pre_event_window": in_window,
        "expected_volatility_multiplier": multiplier,
    }


def summary_meta(kinds: tuple[str, ...] = (), count: int = 0,
                 max_impact: str = "low", reduction: float = 0.0,
                 freeze: bool = False) -> dict:
    return {
        "record_type": "summary",
        "active_event_kinds": list(kinds),
        "active_event_count": count,
        "max_active_impact": max_impact,
        "total_confidence_reduction": reduction,
        "trading_freeze_recommended": freeze,
    }


def snap(i: int, summary: dict, events: tuple[dict, ...]) -> EventSnapshot:
    return EventSnapshot(ts=BASE_TS + timedelta(minutes=i), summary=summary,
                         events=events)


def test_bucketing_groups_summary_with_events() -> None:
    ts = BASE_TS
    rows = [
        (ts, summary_meta(("FNO_EXPIRY",), 1, "high", 0.2)),
        (ts + timedelta(seconds=1), event_meta("FNO_EXPIRY", 2.0, in_window=True)),
        (ts + timedelta(seconds=2), event_meta("INDIA_CPI", 120.0)),
        (ts + timedelta(minutes=5), summary_meta()),  # next run
    ]
    snapshots = bucket_event_observations(rows)
    assert len(snapshots) == 2
    assert snapshots[0].summary["active_event_count"] == 1
    assert len(snapshots[0].events) == 2


def test_nearest_event_drives_hours_and_category() -> None:
    snapshots = [
        snap(0, summary_meta(), (event_meta("CPI", 120.0, "medium"),
                                 event_meta("EXPIRY", 1.5, "high"))),
    ] * 2
    series = compute_event_features(snapshots)
    assert series["event_hours_until_next"][0] == pytest.approx(1.5)
    assert series["event_category_impact"][0] == 1.0  # nearest is high impact


def test_risk_window_and_expected_volatility() -> None:
    active = snap(0, summary_meta(), (
        event_meta("EXPIRY", 1.0, in_window=True, multiplier=1.6),
        event_meta("CPI", 100.0, in_window=False, multiplier=2.5),
    ))
    quiet = snap(1, summary_meta(), (event_meta("CPI", 100.0, multiplier=2.5),))
    series = compute_event_features([active, quiet])
    assert series["event_risk_window"][0] == 1.0
    # Inside a window the governing event is the in-window one.
    assert series["event_expected_volatility"][0] == pytest.approx(1.6)
    assert series["event_risk_window"][1] == 0.0
    # Outside any window the nearest event governs.
    assert series["event_expected_volatility"][1] == pytest.approx(2.5)


def test_summary_driven_features() -> None:
    snapshots = [
        snap(0, summary_meta(("EXPIRY",), 1, "high", 0.2, freeze=False), ()),
        snap(1, summary_meta(("EXPIRY", "RBI"), 2, "high", 0.5, freeze=True), ()),
    ]
    series = compute_event_features(snapshots)
    assert series["event_confidence_reduction"][1] == pytest.approx(0.5)
    assert series["event_trading_freeze"][0] == 0.0
    assert series["event_trading_freeze"][1] == 1.0
    assert val(series["event_market_sensitivity"][1]) > val(
        series["event_market_sensitivity"][0]
    )
    assert 0.0 <= val(series["event_market_sensitivity"][1]) <= 1.0


def test_historical_similarity_recognizes_repeats() -> None:
    snapshots = [
        snap(0, summary_meta(("EXPIRY",), 1, "high", 0.2), ()),
        snap(1, summary_meta(("RBI",), 1, "high", 0.3), ()),
        snap(2, summary_meta(("EXPIRY",), 1, "high", 0.2), ()),  # repeat of run 0
        snap(3, summary_meta(("EXPIRY", "RBI"), 2, "high", 0.5), ()),
    ]
    series = compute_event_features(snapshots)
    assert series["event_historical_similarity"][0] == 0.0  # nothing seen yet
    assert series["event_historical_similarity"][2] == pytest.approx(1.0)
    assert series["event_historical_similarity"][3] == pytest.approx(0.5)


def test_registration_and_z_companions() -> None:
    snapshots = [
        snap(i, summary_meta(("EXPIRY",), 1, "high", 0.2),
             (event_meta("EXPIRY", 10.0 - i * 0.1),))
        for i in range(30)
    ]
    series = compute_event_features(snapshots, normalization_window=10)
    raw = [name for name in series if not name.endswith("_z")]
    assert len(raw) == 8
    for name in raw:
        assert f"{name}_z" in series

    engine = EventRiskEngine(settings=Settings())
    definitions = engine.registry.list_definitions(category="events")
    assert len(definitions) == 8 * 2
    assert all(d.version == "v1" for d in definitions)
