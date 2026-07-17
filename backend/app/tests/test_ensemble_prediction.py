"""Tests for the Ensemble Prediction Engine (Volume 5, Prompt 5.6)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.prediction.ensemble import (
    CORE_FEATURE_NAMES,
    ENSEMBLE_FEATURE_SPECS,
    FEATURE_NAMES,
    MIN_LABEL_QUALITY,
    EnsemblePredictionEngine,
    EnsembleTraining,
    ModelPrediction,
    TrainedModel,
    TrainingRow,
    assemble_dataset,
    blend_predictions,
    blended_probabilities,
    dataset_hash,
    disagreement_score,
    feature_stats,
    predict_from_training,
    train_models,
    uncertainty,
)
from app.prediction.labeling import BarrierConfig, Label
from app.prediction.snapshot import FeatureSnapshot

BASE_TS = datetime(2026, 7, 1, tzinfo=UTC)


def make_label(day: int, label: str, quality: float = 1.0) -> Label:
    return Label(
        symbol="X", timeframe="D", direction="long",
        entry_ts=BASE_TS + timedelta(days=day), entry_price=100.0,
        exit_ts=BASE_TS + timedelta(days=day + 1), exit_price=101.0,
        exit_reason="profit_target" if label == "win" else "stop",
        exit_return_pct=1.0, label=label, label_quality=quality, bars_held=1,
        barrier_config=BarrierConfig(profit_target_pct=0.02, stop_pct=0.01),
    )


# --- pure math ---------------------------------------------------------


def test_disagreement_score_is_zero_when_all_models_agree() -> None:
    assert disagreement_score([0.7, 0.7, 0.7]) == 0.0


def test_disagreement_score_is_one_at_maximum_split() -> None:
    assert disagreement_score([0.0, 0.0, 1.0, 1.0]) == pytest.approx(1.0)


def test_disagreement_score_needs_at_least_two_models() -> None:
    assert disagreement_score([0.8]) == 0.0
    assert disagreement_score([]) == 0.0


def test_uncertainty_is_maximal_at_a_coin_flip() -> None:
    assert uncertainty(0.5) == 1.0


def test_uncertainty_is_zero_at_the_extremes() -> None:
    assert uncertainty(0.0) == 0.0
    assert uncertainty(1.0) == 0.0


def test_blend_predictions_weights_more_skillful_models_more() -> None:
    strong = ModelPrediction(name="a", probability=0.9, weight=0.4, holdout_accuracy=0.9)
    weak = ModelPrediction(name="b", probability=0.1, weight=0.01, holdout_accuracy=0.51)
    probability, confidence = blend_predictions([strong, weak])
    assert probability > 0.5  # dominated by the higher-weight model
    assert confidence == pytest.approx(
        (0.9 * 0.4 + 0.51 * 0.01) / (0.4 + 0.01)
    )


def test_blend_predictions_falls_back_to_a_plain_average_with_zero_total_weight() -> None:
    a = ModelPrediction(name="a", probability=0.8, weight=0.0, holdout_accuracy=0.5)
    b = ModelPrediction(name="b", probability=0.4, weight=0.0, holdout_accuracy=0.5)
    probability, confidence = blend_predictions([a, b])
    assert probability == pytest.approx(0.6)
    assert confidence == pytest.approx(0.5)


def test_blend_predictions_on_empty_input_is_a_neutral_default() -> None:
    assert blend_predictions([]) == (0.5, 0.0)


# --- point-in-time dataset assembly --------------------------------------


def test_assemble_dataset_uses_the_last_value_at_or_before_entry_ts() -> None:
    series = {
        "price_momentum_20": [
            (BASE_TS - timedelta(days=1), 1.0),
            (BASE_TS + timedelta(days=2), 99.0),  # strictly after entry -- must not leak in
        ]
    }
    labels = [make_label(1, "win")]  # entry_ts == BASE_TS + 1 day
    rows = assemble_dataset(
        labels, series, feature_names=("price_momentum_20",), min_coverage=1.0,
        core_feature_names=("price_momentum_20",),
    )
    assert len(rows) == 1
    assert rows[0].features["price_momentum_20"] == 1.0


def test_assemble_dataset_drops_rows_below_coverage_threshold() -> None:
    """Coverage is gated on core_feature_names specifically -- a row can
    qualify with `b` entirely missing as long as the core set is covered."""
    series = {"a": [(BASE_TS, 1.0)], "b": []}
    labels = [make_label(1, "win")]
    rows = assemble_dataset(
        labels, series, feature_names=("a", "b"), min_coverage=1.0,
        core_feature_names=("a", "b"),
    )
    assert rows == []
    rows = assemble_dataset(
        labels, series, feature_names=("a", "b"), min_coverage=0.5,
        core_feature_names=("a", "b"),
    )
    assert len(rows) == 1


def test_assemble_dataset_coverage_ignores_non_core_features() -> None:
    """A feature outside core_feature_names being entirely absent must not
    block a row that has full core coverage -- this is the actual fix for
    DEBT-13: quote/chain/breadth/flow/events features only ~1-2 days old
    must not gate out ~2 years of otherwise-usable D-timeframe history."""
    series = {"core_a": [(BASE_TS, 1.0)], "new_feature": []}
    labels = [make_label(1, "win")]
    rows = assemble_dataset(
        labels, series, feature_names=("core_a", "new_feature"), min_coverage=1.0,
        core_feature_names=("core_a",),
    )
    assert len(rows) == 1
    assert rows[0].features == {"core_a": 1.0}


def test_assemble_dataset_maps_win_and_partial_success_to_one() -> None:
    series = {"a": [(BASE_TS, 1.0)]}
    labels = [make_label(1, "win"), make_label(2, "partial_success"), make_label(3, "loss")]
    rows = assemble_dataset(labels, series, feature_names=("a",), min_coverage=0.0)
    assert [r.label for r in rows] == [1, 1, 0]


def test_min_label_quality_matches_documented_floor() -> None:
    assert MIN_LABEL_QUALITY == 0.2


def test_intraday_5m_features_are_auxiliary_not_core() -> None:
    """DEBT-13 2026-07-17 follow-up: IntradayRiskFeatureEngine's 5m
    features are as recent as the quote/chain/breadth/flow/events set --
    they must stay out of CORE_FEATURE_NAMES (derived from timeframe=="D")
    or 5m-timeframe training would hit the exact same all-rows-fail-
    coverage bug this chunk just fixed for the D path."""
    intraday_names = {
        name for name, _, timeframe in ENSEMBLE_FEATURE_SPECS if timeframe == "5m"
    }
    assert intraday_names, "expected at least one 5m feature spec"
    assert intraday_names.isdisjoint(CORE_FEATURE_NAMES)
    assert intraday_names.issubset(FEATURE_NAMES)


# --- dataset hash (model registry data_hash provenance) --------------------


def test_dataset_hash_is_deterministic_for_identical_input() -> None:
    rows = [
        TrainingRow(ts=BASE_TS, features={"a": 1.0, "b": 2.0}, label=1),
        TrainingRow(ts=BASE_TS + timedelta(minutes=5), features={"a": 3.0}, label=0),
    ]
    assert dataset_hash(rows, feature_names=("a", "b")) == dataset_hash(
        rows, feature_names=("a", "b")
    )


def test_dataset_hash_is_insensitive_to_feature_dict_insertion_order() -> None:
    row_ab = TrainingRow(ts=BASE_TS, features={"a": 1.0, "b": 2.0}, label=1)
    row_ba = TrainingRow(ts=BASE_TS, features={"b": 2.0, "a": 1.0}, label=1)
    assert dataset_hash([row_ab], feature_names=("a", "b")) == dataset_hash(
        [row_ba], feature_names=("a", "b")
    )


def test_dataset_hash_changes_when_a_value_changes() -> None:
    rows_a = [TrainingRow(ts=BASE_TS, features={"a": 1.0}, label=1)]
    rows_b = [TrainingRow(ts=BASE_TS, features={"a": 1.5}, label=1)]
    assert dataset_hash(rows_a, feature_names=("a",)) != dataset_hash(
        rows_b, feature_names=("a",)
    )


def test_dataset_hash_changes_when_a_label_changes() -> None:
    rows_a = [TrainingRow(ts=BASE_TS, features={"a": 1.0}, label=1)]
    rows_b = [TrainingRow(ts=BASE_TS, features={"a": 1.0}, label=0)]
    assert dataset_hash(rows_a, feature_names=("a",)) != dataset_hash(
        rows_b, feature_names=("a",)
    )


def test_dataset_hash_changes_when_row_count_changes() -> None:
    row = TrainingRow(ts=BASE_TS, features={"a": 1.0}, label=1)
    assert dataset_hash([row], feature_names=("a",)) != dataset_hash(
        [row, row], feature_names=("a",)
    )


def test_dataset_hash_of_empty_rows_is_stable() -> None:
    assert dataset_hash([], feature_names=("a",)) == dataset_hash([], feature_names=("a",))


# --- feature stats ---------------------------------------------------------


def test_feature_stats_computes_mean_and_std_ignoring_missing() -> None:
    rows = [
        TrainingRow(ts=BASE_TS, features={"a": 1.0}, label=1),
        TrainingRow(ts=BASE_TS, features={"a": 3.0}, label=0),
        TrainingRow(ts=BASE_TS, features={}, label=0),  # missing "a" entirely
    ]
    means, stds = feature_stats(rows, feature_names=("a",))
    assert means["a"] == pytest.approx(2.0)
    assert stds["a"] == pytest.approx(1.0)


def test_feature_stats_defaults_to_zero_when_a_feature_never_appears() -> None:
    rows = [TrainingRow(ts=BASE_TS, features={}, label=1)]
    means, stds = feature_stats(rows, feature_names=("missing",))
    assert means["missing"] == 0.0
    assert stds["missing"] == 0.0


# --- training + prediction on a synthetic, cleanly-separable dataset ------


def _separable_rows(n: int = 60) -> list[TrainingRow]:
    """feature "signal" perfectly predicts the label -- real sklearn models
    fit on this should score well above chance on the chronological
    holdout, giving every model a real positive weight."""
    rows = []
    for i in range(n):
        signal = 1.0 if i % 2 == 0 else -1.0
        rows.append(TrainingRow(
            ts=BASE_TS + timedelta(minutes=i),
            features={"signal": signal, "noise": float(i % 5)},
            label=1 if signal > 0 else 0,
        ))
    return rows


def test_train_models_learns_a_clean_signal_above_chance() -> None:
    rows = _separable_rows()
    means, _ = feature_stats(rows, feature_names=("signal", "noise"))
    models, split = train_models(rows, feature_names=("signal", "noise"), means=means)

    assert len(models) >= 3  # random_forest, extra_trees, logistic_regression are always available
    assert 0 < split < len(rows)
    for model in models:
        assert model.holdout_accuracy > 0.8  # a perfectly separable signal
        assert model.weight > 0


def test_train_models_returns_nothing_with_a_single_class() -> None:
    rows = [TrainingRow(ts=BASE_TS, features={"a": 1.0}, label=1) for _ in range(10)]
    models, _ = train_models(rows, feature_names=("a",), means={"a": 1.0})
    assert models == []


def test_blended_probabilities_matches_manual_weighted_average() -> None:
    rows = _separable_rows()
    means, _ = feature_stats(rows, feature_names=("signal", "noise"))
    models, split = train_models(rows, feature_names=("signal", "noise"), means=means)
    import numpy as np

    X_holdout = np.array([[r.features[f] for f in ("signal", "noise")] for r in rows[split:]])
    probabilities = blended_probabilities(models, X_holdout)

    total_weight = sum(m.weight for m in models)
    expected_first = sum(
        m.weight * m.model.predict_proba(X_holdout[:1])[0][1] for m in models
    ) / total_weight
    assert probabilities[0] == pytest.approx(expected_first)
    assert len(probabilities) == len(X_holdout)


def test_blended_probabilities_defaults_to_a_coin_flip_with_no_models() -> None:
    import numpy as np

    assert blended_probabilities([], np.array([[1.0], [2.0]])) == [0.5, 0.5]


def test_predict_from_training_blends_a_real_trained_ensemble() -> None:
    rows = _separable_rows()
    means, stds = feature_stats(rows, feature_names=("signal", "noise"))
    models, split = train_models(rows, feature_names=("signal", "noise"), means=means)
    training = EnsembleTraining(
        symbol="X", timeframe="D", direction="long", trained_at=BASE_TS,
        n_samples=len(rows), n_holdout=len(rows) - split,
        feature_names=("signal", "noise"), feature_means=means, feature_stds=stds,
        model_version="test-v1", models=models,
    )
    bullish_snapshot = FeatureSnapshot(
        snapshot_id="s1", symbol="X", timeframe="D", as_of=BASE_TS,
        feature_values={"signal": 1.0, "noise": 2.0},
    )
    prediction = predict_from_training(training, bullish_snapshot)

    assert prediction.probability > 0.5
    assert 0.0 <= prediction.confidence <= 1.0
    assert 0.0 <= prediction.disagreement_score <= 1.0
    assert prediction.uncertainty == pytest.approx(1.0 - 2.0 * abs(prediction.probability - 0.5))
    assert len(prediction.model_predictions) == len(models)
    for model_prediction in prediction.model_predictions:
        assert model_prediction.explanation  # every model produces an explanation


def test_predict_from_training_is_honestly_neutral_when_untrained() -> None:
    training = EnsembleTraining(
        symbol="X", timeframe="D", direction="long", trained_at=BASE_TS,
        n_samples=5, n_holdout=0, feature_names=FEATURE_NAMES,
        feature_means={}, feature_stds={}, model_version="ensemble_v1-untrained-n5", models=[],
    )
    snapshot = FeatureSnapshot(snapshot_id="s2", symbol="X", timeframe="D", as_of=BASE_TS)
    prediction = predict_from_training(training, snapshot)
    assert prediction.probability == 0.5
    assert prediction.confidence == 0.0
    assert prediction.uncertainty == 1.0
    assert prediction.disagreement_score == 0.0
    assert prediction.model_predictions == []


def test_ensemble_training_to_dict_excludes_raw_fitted_estimators() -> None:
    trained = TrainedModel(name="logistic_regression", model=object(), kind="linear",
                            weight=0.3, holdout_accuracy=0.8)
    training = EnsembleTraining(
        symbol="X", timeframe="D", direction="long", trained_at=BASE_TS,
        n_samples=50, n_holdout=10, feature_names=("a",),
        feature_means={"a": 0.0}, feature_stds={"a": 1.0},
        model_version="v1", models=[trained],
    )
    payload = training.to_dict()
    assert payload["models"] == [
        {"name": "logistic_regression", "kind": "linear", "weight": 0.3, "holdout_accuracy": 0.8}
    ]
    assert "model" not in payload["models"][0]
    assert payload["n_calibration_pairs"] == 0  # default: none attached in this test


def test_ensemble_training_defaults_to_no_calibration_pairs() -> None:
    training = EnsembleTraining(
        symbol="X", timeframe="D", direction="long", trained_at=BASE_TS,
        n_samples=0, n_holdout=0, feature_names=("a",),
        feature_means={}, feature_stds={}, model_version="ensemble_v1-untrained-n0",
    )
    assert training.calibration_pairs == []


# --- engine, no DB: honest degradation -------------------------------------


async def test_train_without_a_db_returns_an_untrained_result() -> None:
    engine = EnsemblePredictionEngine(session_factory=None)
    training = await engine.train("NIFTY")
    assert training.n_samples == 0
    assert training.is_trained is False


async def test_predict_without_a_db_returns_a_neutral_prediction() -> None:
    engine = EnsemblePredictionEngine(session_factory=None)
    prediction = await engine.predict("NIFTY")
    assert prediction.probability == 0.5
    assert prediction.confidence == 0.0
    assert prediction.model_predictions == []


async def test_recent_returns_empty_list_without_a_session_factory() -> None:
    engine = EnsemblePredictionEngine(session_factory=None)
    assert await engine.recent("NIFTY") == []


async def test_model_registry_returns_empty_list_without_a_session_factory() -> None:
    engine = EnsemblePredictionEngine(session_factory=None)
    assert await engine.model_registry("NIFTY") == []


async def test_retraining_history_returns_empty_list_without_a_session_factory() -> None:
    engine = EnsemblePredictionEngine(session_factory=None)
    assert await engine.retraining_history("NIFTY") == []


async def test_dataset_registry_returns_empty_list_without_a_session_factory() -> None:
    engine = EnsemblePredictionEngine(session_factory=None)
    assert await engine.dataset_registry() == []


async def test_train_without_a_db_does_not_raise_on_registry_persist() -> None:
    """_persist_registry's session_factory=None guard must fire even on the
    < MIN_TRAINING_SAMPLES path (data_hash is computed either way)."""
    engine = EnsemblePredictionEngine(session_factory=None)
    training = await engine.train("NIFTY", trigger="scheduled")
    assert training.n_samples == 0
