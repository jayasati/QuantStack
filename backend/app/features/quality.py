"""Feature Quality Engine (Volume 3, Prompt 3.14).

Evaluates every stored feature per (symbol, timeframe) group and produces a
0-100 quality score, a confidence multiplier for downstream consumers, and a
drift warning. Reports persist into feature_quality (components in the JSONB
payload) so quality is a queryable time series.

Metrics (each scored 0-100; the composite is the weighted mean of the
metrics that could be computed — nothing is fabricated for missing inputs):
- Freshness: age of the latest observation vs the timeframe's expected
  cadence (daily features tolerate weekends/holidays).
- Completeness / Missing %: the feature's recent row count vs the densest
  feature in its group — siblings share the same opportunity to emit.
- Distribution Stability: PSI between the prior and recent halves of the
  series (0.25 PSI scores zero).
- Variance: near-zero variance means an uninformative feature.
- Correlation Stability: change in lag-1 autocorrelation between halves —
  a feature whose temporal signature flips is unstable.
- Noise: lag-1 autocorrelation mapped to 0-100 (white noise scores ~50,
  strongly mean-reverting/noisy series score low).
- Predictive Power: |correlation| of the feature with the symbol's next-bar
  return (daily timeframe with candles only; absent otherwise).

Confidence multiplier maps the score onto 0.1..1.0. Drift warning fires on
PSI > 0.25 — the full drift engine (Prompt 3.15) provides the deeper tests.
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import fmean, pstdev
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.features.normalize import NORMALIZED_SUFFIX
from app.features.stats import lag1_autocorrelation, psi

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession] | async_sessionmaker[AsyncSession]

WEIGHTS = {
    "freshness": 0.20,
    "completeness": 0.15,
    "distribution_stability": 0.15,
    "variance": 0.10,
    "correlation_stability": 0.10,
    "noise": 0.10,
    "predictive_power": 0.20,
}

PSI_WARNING_THRESHOLD = 0.25
SERIES_WINDOW = 500
MIN_SERIES_LENGTH = 20

# Expected observation cadence (days) per timeframe; freshness scores zero at
# five times the allowance.
FRESHNESS_ALLOWANCE_DAYS = {
    "D": 4.0,  # weekend + holiday tolerance
    "quote": 2.0,
    "chain": 2.0,
    "breadth": 2.0,
    "sector": 2.0,
    "news": 2.0,
    "events": 2.0,
    "clock": 1.0,
}
DEFAULT_ALLOWANCE_DAYS = 3.0


@dataclass(frozen=True)
class QualityReport:
    feature_name: str
    symbol: str
    timeframe: str
    quality_score: float
    confidence_multiplier: float
    drift_warning: bool
    sample_count: int
    components: dict[str, float] = field(default_factory=dict)


def _score_freshness(age_days: float, allowance: float) -> float:
    if age_days <= allowance:
        return 100.0
    return max(0.0, 100.0 * (1 - (age_days - allowance) / (4 * allowance)))


def evaluate_series(
    feature_name: str,
    symbol: str,
    timeframe: str,
    observations: Sequence[tuple[datetime, float]],
    group_max_count: int,
    forward_returns: dict[datetime, float] | None = None,
    now: datetime | None = None,
) -> QualityReport | None:
    """Pure quality evaluation of one feature's observation series."""
    if len(observations) < MIN_SERIES_LENGTH:
        return None
    now = now or datetime.now(UTC)
    values = [v for _, v in observations]
    components: dict[str, float] = {}

    age_days = (now - observations[-1][0]).total_seconds() / 86400
    allowance = FRESHNESS_ALLOWANCE_DAYS.get(timeframe, DEFAULT_ALLOWANCE_DAYS)
    components["freshness"] = _score_freshness(age_days, allowance)

    completeness = min(1.0, len(observations) / max(group_max_count, 1)) * 100
    components["completeness"] = completeness
    missing_pct = 100.0 - completeness

    half = len(values) // 2
    reference, recent = values[:half], values[half:]
    psi_value = psi(reference, recent)
    if psi_value is not None:
        components["distribution_stability"] = max(
            0.0, 100.0 * (1 - psi_value / PSI_WARNING_THRESHOLD / 2)
        )

    std = pstdev(values)
    scale = max(abs(fmean(values)), 1e-9)
    components["variance"] = 100.0 if std / scale > 1e-4 else 0.0

    ac_reference = lag1_autocorrelation(reference)
    ac_recent = lag1_autocorrelation(recent)
    if ac_reference is not None and ac_recent is not None:
        components["correlation_stability"] = 100.0 * (
            1 - min(abs(ac_recent - ac_reference), 1.0)
        )
    ac_full = lag1_autocorrelation(values)
    if ac_full is not None:
        components["noise"] = 100.0 * max(0.0, min(1.0, (ac_full + 1) / 2))

    if forward_returns:
        pairs = [
            (v, forward_returns[ts]) for ts, v in observations if ts in forward_returns
        ]
        if len(pairs) >= MIN_SERIES_LENGTH:
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            mean_x, mean_y = fmean(xs), fmean(ys)
            var_x = fmean([(x - mean_x) ** 2 for x in xs])
            var_y = fmean([(y - mean_y) ** 2 for y in ys])
            if var_x > 0 and var_y > 0:
                cov = fmean(
                    [(x - mean_x) * (y - mean_y) for x, y in pairs]
                )
                correlation = cov / math.sqrt(var_x * var_y)
                components["predictive_power"] = min(1.0, abs(correlation) * 10) * 100

    weight_total = sum(WEIGHTS[name] for name in components)
    score = (
        sum(WEIGHTS[name] * value for name, value in components.items()) / weight_total
        if weight_total
        else 0.0
    )
    components["missing_pct"] = missing_pct  # informational, not weighted
    return QualityReport(
        feature_name=feature_name,
        symbol=symbol,
        timeframe=timeframe,
        quality_score=round(score, 2),
        confidence_multiplier=round(0.1 + 0.9 * score / 100, 4),
        drift_warning=bool(psi_value is not None and psi_value > PSI_WARNING_THRESHOLD),
        sample_count=len(observations),
        components={name: round(value, 2) for name, value in components.items()},
    )


class FeatureQualityEngine:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._sessions = session_factory

    async def evaluate_group(
        self, symbol: str, timeframe: str, include_normalized: bool = False
    ) -> list[QualityReport]:
        """Evaluate every feature stored for one (symbol, timeframe) group."""
        series_map = await self._load_group(symbol, timeframe, include_normalized)
        if not series_map:
            return []
        group_max = max(len(observations) for observations in series_map.values())
        forward_returns = (
            await self._forward_returns(symbol) if timeframe == "D" else None
        )
        reports = []
        for feature_name, observations in series_map.items():
            report = evaluate_series(
                feature_name, symbol, timeframe, observations,
                group_max, forward_returns,
            )
            if report is not None:
                reports.append(report)
        await self._persist(reports)
        return reports

    async def groups(self) -> list[tuple[str, str]]:
        """Every distinct (symbol, timeframe) present in the feature store."""
        from app.database.tables import FeatureStoreRow

        async with self._sessions() as session:
            result = await session.execute(
                select(FeatureStoreRow.symbol, FeatureStoreRow.timeframe).distinct()
            )
            return [(symbol, timeframe) for symbol, timeframe in result.all()]

    async def evaluate_all(self) -> dict[str, int]:
        evaluated = 0
        warnings = 0
        for symbol, timeframe in await self.groups():
            try:
                reports = await self.evaluate_group(symbol, timeframe)
            except Exception as exc:
                logger.error(
                    "quality evaluation failed",
                    extra={"symbol": symbol, "timeframe": timeframe, "error": str(exc)},
                )
                continue
            evaluated += len(reports)
            warnings += sum(1 for r in reports if r.drift_warning)
        logger.info(
            "feature quality sweep complete",
            extra={"features_evaluated": evaluated, "drift_warnings": warnings},
        )
        return {"features_evaluated": evaluated, "drift_warnings": warnings}

    async def _load_group(
        self, symbol: str, timeframe: str, include_normalized: bool
    ) -> dict[str, list[tuple[datetime, float]]]:
        from app.database.tables import FeatureStoreRow

        async with self._sessions() as session:
            result = await session.execute(
                select(
                    FeatureStoreRow.feature_name,
                    FeatureStoreRow.ts,
                    FeatureStoreRow.value,
                )
                .where(
                    FeatureStoreRow.symbol == symbol,
                    FeatureStoreRow.timeframe == timeframe,
                )
                .order_by(FeatureStoreRow.feature_name, FeatureStoreRow.ts)
            )
            rows = result.all()
        series: dict[str, list[tuple[datetime, float]]] = {}
        for feature_name, ts, value in rows:
            if not include_normalized and feature_name.endswith(NORMALIZED_SUFFIX):
                continue
            series.setdefault(feature_name, []).append((ts, value))
        return {
            name: observations[-SERIES_WINDOW:]
            for name, observations in series.items()
        }

    async def _forward_returns(self, symbol: str) -> dict[datetime, float]:
        """Next-bar log return per daily bar ts — predictive-power target."""
        from app.database.tables import OhlcvCandle

        async with self._sessions() as session:
            result = await session.execute(
                select(OhlcvCandle.ts, OhlcvCandle.close)
                .where(OhlcvCandle.symbol == symbol, OhlcvCandle.timeframe == "D")
                .order_by(OhlcvCandle.ts)
            )
            rows = result.all()
        returns: dict[datetime, float] = {}
        for (ts, close), (_, next_close) in zip(rows, rows[1:], strict=False):
            if close and close > 0 and next_close and next_close > 0:
                returns[ts] = math.log(next_close / close)
        return returns

    async def _persist(self, reports: Sequence[QualityReport]) -> None:
        if not reports:
            return
        from app.database.tables import FeatureQualityRow

        rows: list[Any] = [
            FeatureQualityRow(
                feature_name=r.feature_name,
                symbol=r.symbol,
                timeframe=r.timeframe,
                quality_score=r.quality_score,
                sample_count=r.sample_count,
                data={
                    "components": r.components,
                    "confidence_multiplier": r.confidence_multiplier,
                    "drift_warning": r.drift_warning,
                },
            )
            for r in reports
        ]
        async with self._sessions() as session:
            session.add_all(rows)
            await session.commit()
