from datetime import UTC, datetime

from app.intelligence.base import IntelligenceResult
from app.intelligence.report import MarketStateReportEngine, build_market_state_report

AS_OF = datetime(2026, 7, 9, 10, 0, tzinfo=UTC)


def fake(score: float, confidence: float = 0.7, states=None, metrics=None, reasoning=None):
    return IntelligenceResult(
        component="fake", score=score, confidence=confidence,
        states=states or {"bullish": 0.7, "bearish": 0.3},
        metrics=metrics or {}, reasoning=reasoning or ["fake reasoning"],
    )


def full_component_results() -> dict[str, IntelligenceResult]:
    return {
        "trend": fake(70.0, metrics={"trend_direction": 0.4}),
        "volatility": fake(30.0),
        "breadth": fake(60.0),
        "liquidity": fake(80.0),
        "macro": fake(55.0),
        "sector": fake(65.0, metrics={
            "leading_sectors": ["Banking", "IT"], "lagging_sectors": ["Metal", "Realty"],
        }),
        "institutional_flow": fake(72.0, metrics={"accumulation_score": 60.0}),
        "correlation": fake(40.0),
        "market_structure": fake(68.0),
        "event_risk": fake(10.0),
    }


def test_report_includes_current_regime_per_component() -> None:
    report = build_market_state_report(
        "NIFTY", AS_OF, full_component_results(), confidence_result=None, analog_result=None,
    )
    assert report.current_regimes["trend"] == "bullish"
    assert report.probabilities["trend"] == {"bullish": 0.7, "bearish": 0.3}
    assert len(report.current_regimes) == len(full_component_results())


def test_sector_leaders_extracted_from_sector_metrics() -> None:
    report = build_market_state_report(
        "NIFTY", AS_OF, full_component_results(), confidence_result=None, analog_result=None,
    )
    assert report.sector_leaders == {
        "leading_sectors": ["Banking", "IT"], "lagging_sectors": ["Metal", "Realty"],
    }


def test_trend_summary_carries_score_confidence_and_reasoning() -> None:
    report = build_market_state_report(
        "NIFTY", AS_OF, full_component_results(), confidence_result=None, analog_result=None,
    )
    assert report.trend_summary["available"] is True
    assert report.trend_summary["score"] == 70.0
    assert report.trend_summary["dominant_state"] == "bullish"
    assert report.trend_summary["metrics"] == {"trend_direction": 0.4}
    assert report.trend_summary["reasoning"] == ["fake reasoning"]


def test_missing_component_reads_unavailable() -> None:
    partial = full_component_results()
    del partial["trend"]
    report = build_market_state_report(
        "NIFTY", AS_OF, partial, confidence_result=None, analog_result=None,
    )
    assert report.trend_summary == {"available": False}
    assert "trend" not in report.current_regimes


def test_market_confidence_summary_extracted() -> None:
    confidence_result = fake(
        75.0, states={"high_confidence": 0.8, "moderate_confidence": 0.2},
        metrics={"confidence_grade": "B", "confidence_trend": "improving"},
    )
    report = build_market_state_report(
        "NIFTY", AS_OF, full_component_results(), confidence_result=confidence_result,
        analog_result=None,
    )
    assert report.market_confidence == {"score": 75.0, "grade": "B", "trend": "improving"}
    assert report.current_regimes["market_confidence"] == "high_confidence"


def test_historical_analogs_passed_through() -> None:
    analog_result = fake(60.0, metrics={"analogs": [{"date": "2026-01-01", "similarity": 0.9}]})
    report = build_market_state_report(
        "NIFTY", AS_OF, full_component_results(), confidence_result=None,
        analog_result=analog_result,
    )
    assert report.historical_analogs == [{"date": "2026-01-01", "similarity": 0.9}]


def test_composite_fields_computed_from_same_component_results() -> None:
    report = build_market_state_report(
        "NIFTY", AS_OF, full_component_results(), confidence_result=None, analog_result=None,
    )
    assert 0.0 <= report.composite_intelligence_score <= 100.0
    assert 0.0 <= report.expected_opportunity <= 100.0
    assert 0.0 <= report.expected_risk <= 100.0


def test_to_dict_is_json_serializable() -> None:
    import json

    report = build_market_state_report(
        "NIFTY", AS_OF, full_component_results(), confidence_result=None, analog_result=None,
    )
    payload = report.to_dict()
    assert payload["as_of"] == AS_OF.isoformat()
    json.dumps(payload)  # must not raise


def test_empty_report_gracefully_degrades() -> None:
    report = build_market_state_report(
        "NIFTY", AS_OF, {}, confidence_result=None, analog_result=None,
    )
    assert report.current_regimes == {}
    assert report.sector_leaders == {"leading_sectors": None, "lagging_sectors": None}
    assert report.composite_intelligence_score == 50.0
    assert report.historical_analogs == []


async def test_report_as_of_returns_none_without_a_session() -> None:
    engine = MarketStateReportEngine()
    result = await engine.report_as_of("NIFTY", AS_OF)
    assert result is None


async def test_list_reports_returns_empty_without_a_session() -> None:
    engine = MarketStateReportEngine()
    assert await engine.list_reports("NIFTY") == []


async def test_persist_is_a_noop_without_a_session() -> None:
    engine = MarketStateReportEngine()
    report = build_market_state_report(
        "NIFTY", AS_OF, full_component_results(), confidence_result=None, analog_result=None,
    )
    await engine._persist(report)  # must not raise
