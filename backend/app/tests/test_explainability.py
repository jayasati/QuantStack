from datetime import UTC, datetime

import pytest

from app.intelligence.base import Contribution, IntelligenceResult
from app.intelligence.explain import (
    MAX_MARGIN,
    ExplainabilityStore,
    compute_confidence_interval,
    explain,
)

AS_OF = datetime(2026, 7, 9, 10, 0, tzinfo=UTC)


def test_full_confidence_collapses_interval_to_the_score() -> None:
    low, high = compute_confidence_interval(70.0, 1.0)
    assert low == pytest.approx(70.0)
    assert high == pytest.approx(70.0)


def test_zero_confidence_widens_interval_by_max_margin() -> None:
    low, high = compute_confidence_interval(50.0, 0.0)
    assert low == pytest.approx(50.0 - MAX_MARGIN)
    assert high == pytest.approx(50.0 + MAX_MARGIN)


def test_interval_clamps_at_score_boundaries() -> None:
    low, high = compute_confidence_interval(95.0, 0.0)
    assert high == 100.0
    low2, _ = compute_confidence_interval(5.0, 0.0)
    assert low2 == 0.0


def test_partial_confidence_gives_a_partial_margin() -> None:
    low, high = compute_confidence_interval(50.0, 0.5)
    assert low == pytest.approx(50.0 - MAX_MARGIN / 2)
    assert high == pytest.approx(50.0 + MAX_MARGIN / 2)


def test_explain_builds_full_record_from_a_result() -> None:
    result = IntelligenceResult(
        component="trend",
        score=70.0,
        confidence=0.8,
        states={"bull": 0.7, "bear": 0.3},
        contributions=[
            Contribution(feature="price_momentum_20", value=3.5, weight=0.5, effect="bullish"),
        ],
        reasoning=["Momentum is positive.", "Dominant state: bull."],
        as_of=AS_OF,
    )
    record = explain("trend", "NIFTY", "D", result)

    assert record.component == "trend"
    assert record.symbol == "NIFTY"
    assert record.timeframe == "D"
    assert record.as_of == AS_OF
    assert record.score == 70.0
    assert record.confidence == 0.8
    assert record.contributions == [
        {"feature": "price_momentum_20", "value": 3.5, "weight": 0.5, "effect": "bullish"},
    ]
    assert record.reasoning == ["Momentum is positive.", "Dominant state: bull."]
    low, high = record.confidence_interval
    assert low == pytest.approx(70.0 - MAX_MARGIN * 0.2)
    assert high == pytest.approx(70.0 + MAX_MARGIN * 0.2)


def test_explain_handles_no_contributions_or_reasoning() -> None:
    result = IntelligenceResult(component="trend", score=50.0, confidence=0.25, states={})
    record = explain("trend", "NIFTY", "D", result)
    assert record.contributions == []
    assert record.reasoning == []


def test_to_dict_is_json_serializable() -> None:
    import json

    result = IntelligenceResult(
        component="trend", score=70.0, confidence=0.8, states={},
        contributions=[Contribution(feature="x", value=1.0, weight=0.5, effect="bullish")],
        reasoning=["ok"], as_of=AS_OF,
    )
    record = explain("trend", "NIFTY", "D", result)
    payload = record.to_dict()
    assert payload["as_of"] == AS_OF.isoformat()
    json.dumps(payload)  # must not raise


async def test_record_persists_and_returns_the_record_without_a_session() -> None:
    store = ExplainabilityStore()  # no session_factory -> graceful no-op persist
    result = IntelligenceResult(component="trend", score=70.0, confidence=0.8, states={})
    record = await store.record("trend", "NIFTY", "D", result)
    assert record.score == 70.0


async def test_history_returns_empty_without_a_session() -> None:
    store = ExplainabilityStore()
    assert await store.history("trend", "NIFTY", "D") == []


async def test_latest_returns_none_without_a_session() -> None:
    store = ExplainabilityStore()
    assert await store.latest("trend", "NIFTY", "D") is None
