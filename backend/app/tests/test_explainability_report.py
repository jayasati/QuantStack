"""Tests for the Explainability Report (Volume 5, Prompt 5.16)."""

from datetime import UTC, datetime, timedelta

from app.prediction.agreement import AgreementResult
from app.prediction.conviction import ConvictionResult, EvidenceContribution
from app.prediction.ensemble import (
    EnsembleTraining,
    TrainingRow,
    feature_stats,
    train_models,
)
from app.prediction.explainability import (
    ExplainabilityReportEngine,
    build_natural_language_summary,
    build_reason_codes,
    compute_top_features,
)
from app.prediction.historical_similarity import HistoricalSimilarityResult

BASE_TS = datetime(2026, 7, 1, tzinfo=UTC)


def make_conviction(
    score: float = 70.0, grade: str = "B", trend: str = "stable"
) -> ConvictionResult:
    return ConvictionResult(
        symbol="NIFTY", direction="long", snapshot_id="snap-1", as_of=BASE_TS,
        conviction_score=score, conviction_confidence=0.6, conviction_stability=0.8,
        conviction_trend=trend, conviction_grade=grade, trend_slope=0.0, data_completeness=1.0,
        evidence=[EvidenceContribution(name="calibrated_probability", score=score, confidence=0.6)],
    )


def make_agreement(agreement_pct: float = 0.9, consensus: float = 0.8) -> AgreementResult:
    return AgreementResult(
        symbol="NIFTY", snapshot_id="snap-1", as_of=BASE_TS,
        prediction_variance=0.01, agreement_pct=agreement_pct, confidence_spread=0.1,
        consensus_probability=consensus, model_reliability=0.75,
        agreement_level="high" if agreement_pct >= 0.8 else "low",
        proceed=agreement_pct >= 0.8, per_model_reliability=[],
    )


def make_similarity(
    win_rate: float | None = 0.65, average_return: float | None = 0.02, n_analogs: int = 20
) -> HistoricalSimilarityResult:
    return HistoricalSimilarityResult(
        symbol="NIFTY", direction="long", as_of=BASE_TS, n_analogs=n_analogs,
        historical_win_rate=win_rate, average_return=average_return, worst_drawdown=-0.03,
        best_runup=0.05, probability_distribution=None, mean_similarity=0.8, method_agreement=0.6,
    )


# --- build_reason_codes -------------------------------------------------


def test_reason_codes_flag_high_conviction_and_agreement() -> None:
    codes = build_reason_codes(make_conviction(grade="A"), make_agreement(0.9), make_similarity())
    assert "HIGH_CONVICTION" in codes
    assert "MODEL_AGREEMENT_HIGH" in codes
    assert "STRONG_HISTORICAL_PRECEDENT" in codes


def test_reason_codes_flag_low_conviction_and_disagreement() -> None:
    codes = build_reason_codes(
        make_conviction(grade="F"), make_agreement(0.2),
        make_similarity(win_rate=0.3),
    )
    assert "LOW_CONVICTION" in codes
    assert "MODEL_AGREEMENT_LOW" in codes
    assert "WEAK_HISTORICAL_PRECEDENT" in codes


def test_reason_codes_track_conviction_trend() -> None:
    improving = build_reason_codes(
        make_conviction(trend="improving"), make_agreement(), make_similarity()
    )
    declining = build_reason_codes(
        make_conviction(trend="declining"), make_agreement(), make_similarity()
    )
    assert "CONVICTION_IMPROVING" in improving
    assert "CONVICTION_DECLINING" in declining


def test_reason_codes_handle_no_historical_analogs_without_a_code() -> None:
    codes = build_reason_codes(
        make_conviction(), make_agreement(), make_similarity(win_rate=None, average_return=None)
    )
    assert "STRONG_HISTORICAL_PRECEDENT" not in codes
    assert "WEAK_HISTORICAL_PRECEDENT" not in codes


# --- build_natural_language_summary ---------------------------------------


def test_natural_language_summary_mentions_symbol_direction_and_grade() -> None:
    summary = build_natural_language_summary(
        "NIFTY", "long", make_conviction(grade="A", score=88.0), make_agreement(),
        make_similarity(), {"trend": "strong_bull_trend"}, [],
    )
    assert "NIFTY" in summary
    assert "long" in summary
    assert "A" in summary
    assert "strong bull trend" in summary


def test_natural_language_summary_handles_no_analogs_honestly() -> None:
    summary = build_natural_language_summary(
        "NIFTY", "long", make_conviction(), make_agreement(),
        make_similarity(win_rate=None, average_return=None), {}, [],
    )
    assert "No reliable historical analogs" in summary


# --- compute_top_features: real trained ensemble --------------------------


def _separable_rows(n: int = 60) -> list[TrainingRow]:
    rows = []
    for i in range(n):
        signal = 1.0 if i % 2 == 0 else -1.0
        rows.append(TrainingRow(
            ts=BASE_TS + timedelta(minutes=i),
            features={"signal": signal, "noise": float(i % 5)},
            label=1 if signal > 0 else 0,
        ))
    return rows


def test_compute_top_features_on_an_untrained_ensemble_is_empty() -> None:
    training = EnsembleTraining(
        symbol="X", timeframe="D", direction="long", trained_at=BASE_TS,
        n_samples=0, n_holdout=0, feature_names=("a",), feature_means={}, feature_stds={},
        model_version="ensemble_v1-untrained-n0", models=[],
    )
    features, _ = compute_top_features(training, {})
    assert features == []


def test_compute_top_features_uses_real_shap_and_finds_the_real_signal() -> None:
    rows = _separable_rows()
    means, stds = feature_stats(rows, feature_names=("signal", "noise"))
    models, split = train_models(rows, feature_names=("signal", "noise"), means=means)
    training = EnsembleTraining(
        symbol="X", timeframe="D", direction="long", trained_at=BASE_TS,
        n_samples=len(rows), n_holdout=len(rows) - split,
        feature_names=("signal", "noise"), feature_means=means, feature_stds=stds,
        model_version="test-v1", models=models,
    )
    features, shap_available = compute_top_features(training, {"signal": 1.0, "noise": 2.0})

    assert shap_available is True
    assert len(features) > 0
    # The perfectly-separable "signal" feature must dominate "noise".
    assert features[0].feature == "signal"


# --- engine, no DB: honest degradation ---------------------------------------


async def test_generate_without_a_db_runs_cleanly() -> None:
    engine = ExplainabilityReportEngine(session_factory=None)
    report = await engine.generate("NIFTY")
    assert report.symbol == "NIFTY"
    assert report.top_features == []
    assert report.historical_analogs["historical_win_rate"] is None
    assert report.model_agreement["agreement_level"] == "low"
    assert isinstance(report.natural_language_summary, str) and report.natural_language_summary
    assert report.reason_codes  # low conviction/agreement still produce real codes


async def test_generate_confidence_breakdown_has_all_four_stages() -> None:
    engine = ExplainabilityReportEngine(session_factory=None)
    report = await engine.generate("NIFTY")
    assert set(report.confidence_breakdown) == {
        "ensemble_confidence", "calibration_confidence",
        "market_context_confidence", "conviction_confidence",
    }


async def test_recent_returns_empty_list_without_a_session_factory() -> None:
    engine = ExplainabilityReportEngine(session_factory=None)
    assert await engine.recent("NIFTY") == []
