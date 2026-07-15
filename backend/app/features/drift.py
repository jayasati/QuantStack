"""Feature Drift Engine (Volume 3, Prompt 3.15).

Continuously monitors whether feature distributions are shifting away from
what models were trained on. For each feature the recent window is compared
against the preceding reference window:

- Distribution drift: Kolmogorov-Smirnov statistic (shape change).
- Covariate drift: PSI and Jensen-Shannon distance over quantile bins, plus
  population shift (mean change in pooled-sigma units).
- Concept drift: change in the feature's correlation with the symbol's
  next-bar return between the two windows (daily features with candles).
- Missing-pattern drift: change in observation cadence — a feature that
  quietly stops emitting drifts even when its values look stable.

Every detection appends rows to feature_drift (metric, value, threshold,
breached, window sizes in the JSONB payload) — drift history is versioned by
accumulation, never overwritten. A scheduled feature-health job sweeps all
stored groups; the API exposes on-demand detection and the stored history.
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import fmean
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.features.stats import jensen_shannon, ks_statistic, population_shift, psi

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession] | async_sessionmaker[AsyncSession]

RECENT_WINDOW = 100
REFERENCE_WINDOW = 400
MIN_WINDOW = 30

THRESHOLDS = {
    "ks_statistic": 0.20,
    "psi": 0.25,
    "jensen_shannon": 0.30,
    "population_shift": 1.00,
    "concept_shift": 0.30,
    "cadence_ratio": 0.50,  # breached when recent cadence < 50% of reference
}


@dataclass(frozen=True)
class DriftResult:
    feature_name: str
    symbol: str
    timeframe: str
    metric: str
    value: float
    threshold: float
    breached: bool


def detect_series_drift(
    feature_name: str,
    symbol: str,
    timeframe: str,
    observations: Sequence[tuple[datetime, float]],
    forward_returns: dict[datetime, float] | None = None,
    recent_window: int = RECENT_WINDOW,
    reference_window: int = REFERENCE_WINDOW,
) -> list[DriftResult]:
    """Pure drift detection on one feature's observation series."""
    if len(observations) < 2 * MIN_WINDOW:
        return []
    recent_obs = observations[-recent_window:]
    reference_obs = observations[-(recent_window + reference_window) : -recent_window]
    if len(reference_obs) < MIN_WINDOW or len(recent_obs) < MIN_WINDOW:
        return []
    reference = [v for _, v in reference_obs]
    recent = [v for _, v in recent_obs]

    def result(metric: str, value: float | None) -> DriftResult | None:
        if value is None:
            return None
        threshold = THRESHOLDS[metric]
        if metric == "cadence_ratio":
            breached = value < threshold
        else:
            breached = value > threshold
        return DriftResult(
            feature_name=feature_name, symbol=symbol, timeframe=timeframe,
            metric=metric, value=round(value, 6), threshold=threshold,
            breached=breached,
        )

    candidates = [
        result("ks_statistic", ks_statistic(reference, recent)),
        result("psi", psi(reference, recent)),
        result("jensen_shannon", jensen_shannon(reference, recent)),
        result("population_shift", population_shift(reference, recent)),
        result("cadence_ratio", _cadence_ratio(reference_obs, recent_obs)),
    ]
    if forward_returns:
        candidates.append(
            result(
                "concept_shift",
                _concept_shift(reference_obs, recent_obs, forward_returns),
            )
        )
    return [c for c in candidates if c is not None]


def _cadence_ratio(
    reference_obs: Sequence[tuple[datetime, float]],
    recent_obs: Sequence[tuple[datetime, float]],
) -> float | None:
    """Observations per day, recent vs reference (1 = unchanged cadence)."""
    def per_day(observations: Sequence[tuple[datetime, float]]) -> float | None:
        span = (observations[-1][0] - observations[0][0]).total_seconds() / 86400
        if span <= 0:
            return None
        return len(observations) / span

    reference_rate = per_day(reference_obs)
    recent_rate = per_day(recent_obs)
    if reference_rate is None or recent_rate is None or reference_rate <= 0:
        return None
    return recent_rate / reference_rate


def _correlation(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < MIN_WINDOW:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mean_x, mean_y = fmean(xs), fmean(ys)
    var_x = fmean([(x - mean_x) ** 2 for x in xs])
    var_y = fmean([(y - mean_y) ** 2 for y in ys])
    if var_x <= 0 or var_y <= 0:
        return None
    cov = fmean([(x - mean_x) * (y - mean_y) for x, y in pairs])
    return cov / math.sqrt(var_x * var_y)


def _concept_shift(
    reference_obs: Sequence[tuple[datetime, float]],
    recent_obs: Sequence[tuple[datetime, float]],
    forward_returns: dict[datetime, float],
) -> float | None:
    """|corr(feature, forward return)| change between windows."""
    def window_corr(observations: Sequence[tuple[datetime, float]]) -> float | None:
        pairs = [
            (v, forward_returns[ts]) for ts, v in observations if ts in forward_returns
        ]
        return _correlation(pairs)

    reference_corr = window_corr(reference_obs)
    recent_corr = window_corr(recent_obs)
    if reference_corr is None or recent_corr is None:
        return None
    return abs(recent_corr - reference_corr)


class FeatureDriftEngine:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._sessions = session_factory

    async def detect_group(self, symbol: str, timeframe: str) -> list[DriftResult]:
        """Detect drift on every raw feature of one group and persist history."""
        from app.features.quality import FeatureQualityEngine

        quality = FeatureQualityEngine(self._sessions)
        series_map = await quality._load_group(symbol, timeframe, include_normalized=False)
        forward_returns = (
            await quality._forward_returns(symbol) if timeframe == "D" else None
        )
        results: list[DriftResult] = []
        for feature_name, observations in series_map.items():
            results.extend(
                detect_series_drift(
                    feature_name, symbol, timeframe, observations, forward_returns
                )
            )
        await self._persist(results)
        return results

    async def detect_all(self) -> dict[str, int]:
        from app.features.quality import FeatureQualityEngine

        quality = FeatureQualityEngine(self._sessions)
        detections = 0
        breaches = 0
        for symbol, timeframe in await quality.groups():
            try:
                results = await self.detect_group(symbol, timeframe)
            except Exception as exc:
                logger.error(
                    "drift detection failed",
                    extra={"symbol": symbol, "timeframe": timeframe, "error": str(exc)},
                )
                continue
            detections += len(results)
            breaches += sum(1 for r in results if r.breached)
        logger.info(
            "feature drift sweep complete",
            extra={"detections": detections, "breaches": breaches},
        )
        return {"detections": detections, "breaches": breaches}

    async def history(
        self, feature_name: str, symbol: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        from app.database.tables import FeatureDriftRow

        query = (
            select(
                FeatureDriftRow.created_at, FeatureDriftRow.metric,
                FeatureDriftRow.value, FeatureDriftRow.threshold,
                FeatureDriftRow.breached, FeatureDriftRow.data,
            )
            .where(FeatureDriftRow.feature_name == feature_name)
            .order_by(FeatureDriftRow.id.desc())
            .limit(limit)
        )
        async with self._sessions() as session:
            rows = (await session.execute(query)).all()
        history = []
        for row in rows:
            payload = row.data or {}
            if symbol is not None and payload.get("symbol") != symbol:
                continue
            history.append(
                {
                    "at": row.created_at.isoformat() if row.created_at else None,
                    "metric": row.metric,
                    "value": row.value,
                    "threshold": row.threshold,
                    "breached": row.breached,
                    **payload,
                }
            )
        return history

    async def _persist(self, results: Sequence[DriftResult]) -> None:
        if not results:
            return
        from app.database.tables import FeatureDriftRow

        rows = [
            FeatureDriftRow(
                feature_name=r.feature_name,
                metric=r.metric,
                value=r.value,
                threshold=r.threshold,
                breached=r.breached,
                data={
                    "symbol": r.symbol,
                    "timeframe": r.timeframe,
                    "recent_window": RECENT_WINDOW,
                    "reference_window": REFERENCE_WINDOW,
                },
            )
            for r in results
        ]
        async with self._sessions() as session:
            session.add_all(rows)
            await session.commit()
