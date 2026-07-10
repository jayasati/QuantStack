"""Historical Similarity Prediction (Volume 5, Prompt 5.9).

"For every candidate: retrieve the Top 20 historical analogs." The actual
retrieval already exists -- Volume 4's Historical Analog Engine (Prompt
4.10, app/intelligence/analogs.py) already finds the top 20 historical
sessions by cosine similarity over a z-scored feature vector, each analog
carrying its own real forward-looking subsequent return/volatility/
drawdown/run-up. Nothing here re-implements that search; this module calls
HistoricalAnalogEngine.assess(symbol) directly and adds the four things a
market-level assessment didn't need but a per-candidate, direction-aware
prediction does:

- Direction-adjusted Win Rate / Average Return: Prompt 4.10's own
  win_rate/mean_subsequent_return are direction-agnostic (a market-level
  "is history bullish or bearish here" read). A trade candidate has a
  concrete direction (long/short, Prompt 5.2) -- for a short candidate a
  falling price is the WIN, so subsequent_return is sign-flipped before
  computing win rate/average return, the same sign convention
  labeling.py's own triple-barrier walk-forward already uses.
- Worst Drawdown / Best Run-up: the single most extreme analog in the
  pool (min of max_drawdown, max of max_runup) -- genuinely different
  from Prompt 4.10's own mean_max_drawdown/mean_max_runup, which average
  across all 20 rather than surfacing the tail case a risk-aware reader
  actually wants to see. For a short candidate, these are approximated by
  swapping the long path's own run-up/drawdown magnitudes (a rally against
  a short is its worst excursion, a decline is its best) -- a directional
  sign convention, not a literal short-equity-curve recomputation, since
  Prompt 4.10's Analog record only stores summary stats per analog, not
  each analog's full daily-return path. Documented v1 approximation, the
  same spirit as analogs.py's own Euclidean-distance stand-in for
  Mahalanobis distance.
- Probability Distribution: a percentile breakdown (p10/p25/median/p75/
  p90) of the direction-adjusted subsequent returns across the 20 analogs
  -- Prompt 4.10 only ever reported the mean.

"Pass historical statistics into the Conviction Engine": the Conviction
Engine (Prompt 5.11) doesn't exist yet -- these results are exposed here
for it to consume once it does, the same "field exists, downstream
consumer deferred" pattern FeatureSnapshot.model_version used until
Prompt 5.6 existed.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import fmean
from typing import Any

import numpy as np

from app.core.cache import CacheService
from app.core.config import Settings, get_settings
from app.intelligence.analogs import HistoricalAnalogEngine
from app.intelligence.base import IntelligenceResult
from app.prediction.candidates import CandidateGenerationEngine, TradeCandidate

EVENT_TYPE = "historical_similarity.result"

PERCENTILES: tuple[int, ...] = (10, 25, 50, 75, 90)


def _direction_sign(direction: str) -> float:
    """"neutral" candidates have no directional bias to adjust for --
    evaluated on the raw (long-convention) historical path, same as
    "long"."""
    return -1.0 if direction == "short" else 1.0


@dataclass(frozen=True)
class ProbabilityDistribution:
    p10: float
    p25: float
    median: float
    p75: float
    p90: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "p10": self.p10, "p25": self.p25, "median": self.median,
            "p75": self.p75, "p90": self.p90,
        }


def probability_distribution(returns: Sequence[float]) -> ProbabilityDistribution | None:
    if not returns:
        return None
    p10, p25, p50, p75, p90 = np.percentile(np.array(returns), PERCENTILES)
    return ProbabilityDistribution(
        p10=round(float(p10), 4), p25=round(float(p25), 4), median=round(float(p50), 4),
        p75=round(float(p75), 4), p90=round(float(p90), 4),
    )


@dataclass
class HistoricalSimilarityResult:
    symbol: str
    direction: str
    as_of: datetime
    n_analogs: int
    historical_win_rate: float | None
    average_return: float | None
    worst_drawdown: float | None
    best_runup: float | None
    probability_distribution: ProbabilityDistribution | None
    mean_similarity: float | None
    method_agreement: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "as_of": self.as_of.isoformat(),
            "n_analogs": self.n_analogs,
            "historical_win_rate": self.historical_win_rate,
            "average_return": self.average_return,
            "worst_drawdown": self.worst_drawdown,
            "best_runup": self.best_runup,
            "probability_distribution": (
                self.probability_distribution.to_dict()
                if self.probability_distribution is not None else None
            ),
            "mean_similarity": self.mean_similarity,
            "method_agreement": self.method_agreement,
        }


def evaluate_similarity(
    symbol: str, direction: str, as_of: datetime, analog_result: IntelligenceResult
) -> HistoricalSimilarityResult:
    """Pure transformation from an already-computed Historical Analog
    IntelligenceResult (Prompt 4.10) into a direction-aware, per-candidate
    prediction -- no DB access, no re-running the analog search."""
    analogs: list[dict[str, Any]] = analog_result.metrics.get("analogs") or []
    if not analogs:
        return HistoricalSimilarityResult(
            symbol=symbol, direction=direction, as_of=as_of, n_analogs=0,
            historical_win_rate=None, average_return=None, worst_drawdown=None,
            best_runup=None, probability_distribution=None,
            mean_similarity=None, method_agreement=analog_result.metrics.get("method_agreement"),
        )

    sign = _direction_sign(direction)
    adjusted_returns = [sign * a["subsequent_return"] for a in analogs]
    win_rate = sum(1 for r in adjusted_returns if r > 0) / len(adjusted_returns)
    average_return = fmean(adjusted_returns)

    if sign > 0:
        drawdowns = [a["max_drawdown"] for a in analogs]
        runups = [a["max_runup"] for a in analogs]
    else:
        # Short-candidate approximation: a rally against the short (the
        # long path's own best run-up) is the short's worst excursion, and
        # vice versa -- see module docstring.
        drawdowns = [-a["max_runup"] for a in analogs]
        runups = [-a["max_drawdown"] for a in analogs]

    return HistoricalSimilarityResult(
        symbol=symbol, direction=direction, as_of=as_of, n_analogs=len(analogs),
        historical_win_rate=round(win_rate, 4), average_return=round(average_return, 4),
        worst_drawdown=round(min(drawdowns), 4), best_runup=round(max(runups), 4),
        probability_distribution=probability_distribution(adjusted_returns),
        mean_similarity=analog_result.metrics.get("mean_similarity"),
        method_agreement=analog_result.metrics.get("method_agreement"),
    )


class HistoricalSimilarityEngine:
    name = "historical_similarity_engine"

    def __init__(
        self,
        session_factory: Any = None,
        cache: CacheService | None = None,
        settings: Settings | None = None,
        analog_engine: HistoricalAnalogEngine | None = None,
        candidate_engine: CandidateGenerationEngine | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings or get_settings()
        self._analogs = analog_engine or HistoricalAnalogEngine(
            session_factory=session_factory, cache=cache, settings=self._settings,
        )
        self._candidates = candidate_engine or CandidateGenerationEngine(
            session_factory=session_factory, settings=self._settings,
        )

    async def evaluate(self, symbol: str, direction: str = "long") -> HistoricalSimilarityResult:
        """Top 20 historical analogs for one (symbol, direction), and the
        historical statistics computed from them."""
        analog_result = await self._analogs.assess(symbol)
        result = evaluate_similarity(symbol, direction, analog_result.as_of, analog_result)
        await self._persist(result)
        return result

    async def evaluate_candidates(
        self, candidates: Sequence[TradeCandidate]
    ) -> list[HistoricalSimilarityResult]:
        """"For every candidate": one Historical Similarity result per
        already-generated TradeCandidate (Prompt 5.2)."""
        return [await self.evaluate(c.instrument, c.direction) for c in candidates]

    async def evaluate_top_candidates(self) -> list[HistoricalSimilarityResult]:
        """Convenience: a fresh Top-20 candidate scan, then a similarity
        result for every one of them."""
        candidates = await self._candidates.generate()
        return await self.evaluate_candidates(candidates)

    async def _persist(self, result: HistoricalSimilarityResult) -> None:
        if self._sessions is None:
            return
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            session.add(MarketEvent(
                event_type=EVENT_TYPE,
                source=self.name,
                data=result.to_dict(),
            ))
            await session.commit()

    async def recent(self, symbol: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if self._sessions is None:
            return []
        from sqlalchemy import desc, select

        from app.database.tables import MarketEvent

        query = select(MarketEvent.data).where(MarketEvent.event_type == EVENT_TYPE)
        if symbol is not None:
            query = query.where(MarketEvent.data["symbol"].astext == symbol)
        query = query.order_by(desc(MarketEvent.id)).limit(limit)
        async with self._sessions() as session:
            result = await session.execute(query)
            return list(result.scalars().all())
