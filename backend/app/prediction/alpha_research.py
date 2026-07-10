"""Alpha Research Engine (Volume 5.5 — recommended extension).

"Instead of relying only on predefined features, build a subsystem that
continuously searches for new predictive signals." Reuses the exact
point-in-time join, chronological train/holdout split, and model-training
machinery Prompt 5.6's Ensemble Prediction Engine already built
(`assemble_dataset`, `feature_stats`, `train_models` all already accept an
arbitrary `feature_names` sequence, not hardcoded to the production set) --
nothing here re-implements dataset assembly or model fitting a second way.

The six asks, each mapped to a real, computable mechanism:

- Evaluate candidate features automatically: point-biserial correlation
  (Pearson correlation against a binary win/loss label -- Python's own
  `statistics.correlation`) between each candidate feature's point-in-time
  value and Triple Barrier outcomes (Prompt 5.5). A pure statistical
  screen, not a full model fit, since scanning many candidate features
  needs to stay cheap.
- Rank them by predictive power: sort by |correlation| descending.
- Detect feature decay over time: the SAME lookback window split in half
  chronologically -- correlation in the older half vs. the recent half.
  A feature whose recent-half power has dropped meaningfully below its
  older-half power is decaying. This is a self-contained, single-pass
  decay read (always computable on the first run); comparing against
  PERSISTED prior evaluation runs once enough of them have accumulated is
  a natural v2 extension, deferred rather than fabricated from data this
  first run doesn't have yet.
- Recommend new features for inclusion: predictive_power at or above a
  documented threshold, not already in the production ensemble's own
  feature set (ensemble.ENSEMBLE_FEATURE_SPECS), and not meaningfully
  decaying.
- Compare new models against production models: trains TWO ensembles
  over the exact same labels -- a "champion" using only the production
  feature set, and a "challenger" using production + candidate features
  -- and compares mean holdout accuracy across their models. A named
  feature-set variant plus its measured performance is this module's
  notion of a "strategy".
- Maintain a research leaderboard of features and strategies: every
  feature evaluation and every model comparison is persisted
  (event-sourced, matching this codebase's own MarketEvent convention),
  queryable as a leaderboard ranked across every symbol/timeframe ever
  evaluated, not just one.

The illustrative candidate feature pool (CANDIDATE_FEATURE_SPECS) is a
documented, reasonably-sized sample of real, already-collected features
(Volume 3) that are NOT part of the production ensemble's own feature
set -- not every feature this codebase has ever defined (that would need
the live, in-memory FeatureRegistry populated by full application
startup, which this module deliberately doesn't depend on, to stay
self-contained and testable without a running app).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import correlation
from typing import Any

from app.core.cache import CacheService
from app.core.config import Settings, get_settings
from app.features.store import FeatureStore
from app.prediction.ensemble import (
    DEFAULT_MAX_HOLDING_BARS,
    ENSEMBLE_FEATURE_SPECS,
    FEATURE_HISTORY_LIMIT,
    INSTRUMENT,
    MARKET,
    MIN_LABEL_QUALITY,
    MIN_TRAINING_SAMPLES,
    EnsemblePredictionEngine,
    assemble_dataset,
    feature_stats,
    train_models,
)
from app.prediction.labeling import Label, TripleBarrierLabelingEngine

FEATURE_EVENT_TYPE = "alpha_research.feature_evaluation"
COMPARISON_EVENT_TYPE = "alpha_research.model_comparison"

MIN_FEATURE_SAMPLES = 20  # below this, correlation is too noisy to report honestly
RECOMMENDATION_THRESHOLD = 0.10  # |correlation| at/above this is a meaningful univariate signal
# A 0.05 drop in correlation (older half -> recent half) reads as decaying.
DECAY_WARNING_THRESHOLD = 0.05
# A holdout-accuracy gap below this reads as a "tie", not a real winner.
MODEL_COMPARISON_EPSILON = 0.01

PRODUCTION_FEATURE_NAMES: frozenset[str] = frozenset(spec[0] for spec in ENSEMBLE_FEATURE_SPECS)

# A documented, illustrative sample of real Volume 3 features NOT already
# in the production ensemble's own feature set.
CANDIDATE_FEATURE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("price_momentum_50", INSTRUMENT, "D"),
    ("price_momentum_5", INSTRUMENT, "D"),
    ("volume_rvol_50", INSTRUMENT, "D"),
    ("volume_cmf_20", INSTRUMENT, "D"),
    ("volatility_compression_20", INSTRUMENT, "D"),
    ("risk_sharpe_20", INSTRUMENT, "D"),
    ("risk_var_95_20", INSTRUMENT, "D"),
    ("rs_percentile_rank_20", INSTRUMENT, "D"),
    ("ms_liquidity_zone_distance_pct", INSTRUMENT, "D"),
    ("news_sentiment", MARKET, "news"),
)


def _correlation_magnitude(values: Sequence[float], labels: Sequence[int]) -> float | None:
    """|Pearson correlation| between a continuous feature and a binary
    label -- point-biserial correlation. None (never a fabricated 0.0)
    when there's too little data or the label doesn't vary."""
    if len(values) < 2 or len(set(labels)) < 2:
        return None
    return round(abs(correlation(values, labels)), 4)


def build_feature_label_pairs(
    labels: Sequence[Label], series: Sequence[tuple[datetime, float]]
) -> tuple[list[float], list[int]]:
    """Point-in-time join (last observation at or before each label's
    entry_ts, the same as-of convention ensemble.py's own dataset
    assembly uses) between one candidate feature's history and Triple
    Barrier outcomes."""
    import bisect

    timestamps = [ts for ts, _ in series]
    values: list[float] = []
    targets: list[int] = []
    for label in labels:
        if label.label_quality < MIN_LABEL_QUALITY:
            continue
        idx = bisect.bisect_right(timestamps, label.entry_ts) - 1
        if idx < 0:
            continue
        values.append(series[idx][1])
        targets.append(1 if label.label in ("win", "partial_success") else 0)
    return values, targets


@dataclass
class FeatureEvaluation:
    feature_name: str
    n_samples: int
    predictive_power: float | None
    older_half_power: float | None
    recent_half_power: float | None
    decay: float | None
    is_production_feature: bool

    @property
    def is_recommended(self) -> bool:
        return (
            not self.is_production_feature
            and self.predictive_power is not None
            and self.predictive_power >= RECOMMENDATION_THRESHOLD
            and (self.decay is None or self.decay < DECAY_WARNING_THRESHOLD)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_name": self.feature_name,
            "n_samples": self.n_samples,
            "predictive_power": self.predictive_power,
            "older_half_power": self.older_half_power,
            "recent_half_power": self.recent_half_power,
            "decay": self.decay,
            "is_production_feature": self.is_production_feature,
            "is_recommended": self.is_recommended,
        }


def evaluate_feature(
    feature_name: str, values: Sequence[float], targets: Sequence[int]
) -> FeatureEvaluation:
    """Pure evaluation from already-joined (value, label) pairs -- no DB
    access. Splits chronologically in half for the decay read."""
    n = len(values)
    if n < MIN_FEATURE_SAMPLES:
        return FeatureEvaluation(
            feature_name=feature_name, n_samples=n, predictive_power=None,
            older_half_power=None, recent_half_power=None, decay=None,
            is_production_feature=feature_name in PRODUCTION_FEATURE_NAMES,
        )

    split = n // 2
    overall_power = _correlation_magnitude(values, targets)
    older_power = _correlation_magnitude(values[:split], targets[:split])
    recent_power = _correlation_magnitude(values[split:], targets[split:])
    decay = (
        round(older_power - recent_power, 4)
        if older_power is not None and recent_power is not None else None
    )

    return FeatureEvaluation(
        feature_name=feature_name, n_samples=n, predictive_power=overall_power,
        older_half_power=older_power, recent_half_power=recent_power, decay=decay,
        is_production_feature=feature_name in PRODUCTION_FEATURE_NAMES,
    )


@dataclass
class ModelComparisonResult:
    symbol: str
    direction: str
    as_of: datetime
    champion_holdout_accuracy: float | None
    challenger_holdout_accuracy: float | None
    improvement: float | None
    champion_feature_count: int
    challenger_feature_count: int
    added_features: list[str] = field(default_factory=list)
    winner: str = "insufficient_data"  # "champion" | "challenger" | "tie" | "insufficient_data"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "as_of": self.as_of.isoformat(),
            "champion_holdout_accuracy": self.champion_holdout_accuracy,
            "challenger_holdout_accuracy": self.challenger_holdout_accuracy,
            "improvement": self.improvement,
            "champion_feature_count": self.champion_feature_count,
            "challenger_feature_count": self.challenger_feature_count,
            "added_features": self.added_features,
            "winner": self.winner,
        }


def _mean_holdout_accuracy(rows: Sequence, feature_names: Sequence[str]) -> float | None:
    if len(rows) < MIN_TRAINING_SAMPLES:
        return None
    means, _ = feature_stats(rows, feature_names)
    models, _ = train_models(rows, feature_names=feature_names, means=means)
    if not models:
        return None
    return round(sum(m.holdout_accuracy for m in models) / len(models), 4)


class AlphaResearchEngine:
    name = "alpha_research_engine"

    def __init__(
        self,
        session_factory: Any = None,
        cache: CacheService | None = None,
        settings: Settings | None = None,
        labeling_engine: TripleBarrierLabelingEngine | None = None,
        ensemble_engine: EnsemblePredictionEngine | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings or get_settings()
        self.store = FeatureStore(session_factory=session_factory, cache=cache)
        self._labeling = labeling_engine or TripleBarrierLabelingEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._ensemble = ensemble_engine or EnsemblePredictionEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )

    async def evaluate_candidate_features(
        self,
        symbol: str,
        timeframe: str = "D",
        direction: str = "long",
        lookback_bars: int = 500,
        max_holding_bars: int = DEFAULT_MAX_HOLDING_BARS,
        candidate_specs: Sequence[tuple[str, str, str]] = CANDIDATE_FEATURE_SPECS,
    ) -> list[FeatureEvaluation]:
        """Evaluates every candidate feature spec against real Triple
        Barrier outcomes for this symbol, ranked by predictive power."""
        labels = await self._labeling.label_history(
            symbol, timeframe=timeframe, direction=direction,
            lookback_bars=lookback_bars, max_holding_bars=max_holding_bars,
        )
        evaluations = []
        for feature_name, symbol_mode, feature_timeframe in candidate_specs:
            key_symbol = symbol if symbol_mode == INSTRUMENT else symbol_mode
            series = await self._fetch_series(feature_name, key_symbol, feature_timeframe)
            values, targets = build_feature_label_pairs(labels, series)
            evaluations.append(evaluate_feature(feature_name, values, targets))

        evaluations.sort(
            key=lambda e: (e.predictive_power is not None, e.predictive_power or 0.0),
            reverse=True,
        )
        await self._persist_evaluations(symbol, evaluations)
        return evaluations

    async def recommend_features(
        self, symbol: str, timeframe: str = "D", direction: str = "long", **kwargs: Any
    ) -> list[FeatureEvaluation]:
        evaluations = await self.evaluate_candidate_features(
            symbol, timeframe=timeframe, direction=direction, **kwargs
        )
        return [e for e in evaluations if e.is_recommended]

    async def compare_against_production(
        self,
        symbol: str,
        timeframe: str = "D",
        direction: str = "long",
        lookback_bars: int = 500,
        max_holding_bars: int = DEFAULT_MAX_HOLDING_BARS,
        candidate_feature_names: Sequence[str] | None = None,
    ) -> ModelComparisonResult:
        """Champion (production feature set) vs. challenger (production +
        candidate features), trained over the exact same labels."""
        labels = await self._labeling.label_history(
            symbol, timeframe=timeframe, direction=direction,
            lookback_bars=lookback_bars, max_holding_bars=max_holding_bars,
        )
        quality_labels = [label for label in labels if label.label_quality >= MIN_LABEL_QUALITY]
        candidate_specs = [
            spec for spec in CANDIDATE_FEATURE_SPECS
            if candidate_feature_names is None or spec[0] in candidate_feature_names
        ]
        all_specs = list(ENSEMBLE_FEATURE_SPECS) + candidate_specs
        feature_series = await self._fetch_all_series(all_specs, symbol)

        champion_names = tuple(spec[0] for spec in ENSEMBLE_FEATURE_SPECS)
        challenger_names = tuple(spec[0] for spec in all_specs)

        champion_rows = assemble_dataset(
            quality_labels, feature_series, feature_names=champion_names
        )
        challenger_rows = assemble_dataset(
            quality_labels, feature_series, feature_names=challenger_names
        )

        champion_accuracy = _mean_holdout_accuracy(champion_rows, champion_names)
        challenger_accuracy = _mean_holdout_accuracy(challenger_rows, challenger_names)

        improvement = None
        winner = "insufficient_data"
        if champion_accuracy is not None and challenger_accuracy is not None:
            improvement = round(challenger_accuracy - champion_accuracy, 4)
            if improvement > MODEL_COMPARISON_EPSILON:
                winner = "challenger"
            elif improvement < -MODEL_COMPARISON_EPSILON:
                winner = "champion"
            else:
                winner = "tie"
        elif challenger_accuracy is not None:
            winner = "challenger"
        elif champion_accuracy is not None:
            winner = "champion"

        result = ModelComparisonResult(
            symbol=symbol, direction=direction, as_of=datetime.now(UTC),
            champion_holdout_accuracy=champion_accuracy,
            challenger_holdout_accuracy=challenger_accuracy, improvement=improvement,
            champion_feature_count=len(champion_names),
            challenger_feature_count=len(challenger_names),
            added_features=[spec[0] for spec in candidate_specs], winner=winner,
        )
        await self._persist_comparison(result)
        return result

    async def _fetch_series(
        self, feature_name: str, symbol: str, timeframe: str
    ) -> list[tuple[datetime, float]]:
        rows = await self.store.history(
            feature_name, symbol=symbol, timeframe=timeframe, limit=FEATURE_HISTORY_LIMIT,
        )
        return sorted(
            (datetime.fromisoformat(row["ts"]), row["value"])
            for row in rows if row["value"] is not None
        )

    async def _fetch_all_series(
        self, specs: Sequence[tuple[str, str, str]], symbol: str
    ) -> dict[str, list[tuple[datetime, float]]]:
        series: dict[str, list[tuple[datetime, float]]] = {}
        for feature_name, symbol_mode, timeframe in specs:
            key_symbol = symbol if symbol_mode == INSTRUMENT else symbol_mode
            series[feature_name] = await self._fetch_series(feature_name, key_symbol, timeframe)
        return series

    async def _persist_evaluations(
        self, symbol: str, evaluations: Sequence[FeatureEvaluation]
    ) -> None:
        if self._sessions is None:
            return
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            for evaluation in evaluations:
                session.add(MarketEvent(
                    event_type=FEATURE_EVENT_TYPE,
                    source=self.name,
                    data={
                        "symbol": symbol, "as_of": datetime.now(UTC).isoformat(),
                        **evaluation.to_dict(),
                    },
                ))
            await session.commit()

    async def _persist_comparison(self, result: ModelComparisonResult) -> None:
        if self._sessions is None:
            return
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            session.add(MarketEvent(
                event_type=COMPARISON_EVENT_TYPE,
                source=self.name,
                data=result.to_dict(),
            ))
            await session.commit()

    async def feature_leaderboard(self, top_n: int = 20) -> list[dict[str, Any]]:
        """Top N persisted feature evaluations by predictive power,
        across every symbol/timeframe ever evaluated."""
        rows = await self._recent_raw(FEATURE_EVENT_TYPE, limit=500)
        ranked = [row for row in rows if row.get("predictive_power") is not None]
        ranked.sort(key=lambda row: row["predictive_power"], reverse=True)
        return ranked[:top_n]

    async def comparison_leaderboard(self, top_n: int = 20) -> list[dict[str, Any]]:
        """Top N persisted model ("strategy") comparisons by improvement
        over their champion, across every symbol ever compared."""
        rows = await self._recent_raw(COMPARISON_EVENT_TYPE, limit=500)
        ranked = [row for row in rows if row.get("improvement") is not None]
        ranked.sort(key=lambda row: row["improvement"], reverse=True)
        return ranked[:top_n]

    async def recent(
        self, symbol: str | None = None, event_type: str = FEATURE_EVENT_TYPE, limit: int = 50
    ) -> list[dict[str, Any]]:
        if self._sessions is None:
            return []
        from sqlalchemy import desc, select

        from app.database.tables import MarketEvent

        query = select(MarketEvent.data).where(MarketEvent.event_type == event_type)
        if symbol is not None:
            query = query.where(MarketEvent.data["symbol"].astext == symbol)
        query = query.order_by(desc(MarketEvent.id)).limit(limit)
        async with self._sessions() as session:
            result = await session.execute(query)
            return list(result.scalars().all())

    async def _recent_raw(self, event_type: str, limit: int) -> list[dict[str, Any]]:
        if self._sessions is None:
            return []
        from sqlalchemy import desc, select

        from app.database.tables import MarketEvent

        query = (
            select(MarketEvent.data)
            .where(MarketEvent.event_type == event_type)
            .order_by(desc(MarketEvent.id))
            .limit(limit)
        )
        async with self._sessions() as session:
            result = await session.execute(query)
            return list(result.scalars().all())
