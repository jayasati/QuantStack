"""Multi-Horizon Prediction Engine (Volume 5, Prompt 5.4).

"Do not predict price. Predict probabilities across multiple horizons"
(5min/15min/30min/1hour/end_of_day/next_day), probability-only, no buy/sell
recommendation, every probability stored independently.

No ML model exists yet -- Prompt 5.6 (Ensemble Prediction) is what
eventually trains one against Triple Barrier labels (Prompt 5.5, also not
yet built). Until then, this is a principled, well-understood, documented
v1 baseline, not a placeholder or a fabricated number: a drift-diffusion
(geometric Brownian motion) probability-of-up-move estimate, using only
already-verified Volume 4 fields as its drift (Trend Intelligence's
trend_direction/trend_strength) and diffusion (Volatility Intelligence's
expected_volatility_pct) terms. This is exactly what a "naive baseline"
means in Volume 5.5's own vocabulary (a Model Tournament needs something to
beat) -- chosen because it's a standard, well-understood estimator, not
because it's presumed accurate.

Properly implements "Snapshot -> Prediction, never Live Market ->
Prediction": predict_from_snapshot() computes drift/volatility by calling
assess_trend()/assess_volatility() (Volume 4's own pure functions) directly
on a FeatureSnapshot's frozen feature_values, not a fresh live fetch --
exact historical reconstruction of any past prediction is just re-running
this pure function against the same persisted snapshot.

Using the SAME (drift, volatility) pair for every horizon, scaled by each
horizon's time fraction, is itself a v1 simplification: an intraday-
specific realized-vol read (IntradayRiskFeatureEngine, already built) would
likely sharpen the short horizons (5min/15min/30min/1hour) — deferred
rather than built silently here, since it adds a second data fetch this
prompt doesn't strictly need.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from statistics import NormalDist
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.events.bus import Event, EventBus
from app.features.intraday_risk import SESSION_MINUTES
from app.intelligence.trend import assess_trend
from app.intelligence.volatility import assess_volatility
from app.prediction.snapshot import FeatureSnapshot, FeatureSnapshotEngine

logger = get_logger(__name__)

EVENT_TYPE = "multi_horizon_prediction.probability"

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN_MINUTES = 9 * 60 + 15  # 09:15 IST
SESSION_CLOSE_MINUTES = SESSION_OPEN_MINUTES + SESSION_MINUTES  # 15:30 IST

TRADING_DAYS = 252
MIN_HORIZON_MINUTES = 1.0  # floor so a horizon can never be 0 (div-by-zero guard)
MIN_SIGMA = 1e-4  # floor so a genuinely-zero volatility read can't divide by zero

# Documented v1 heuristic scales (same category as MOMENTUM_SATURATION/
# STRENGTH_SATURATION elsewhere in Volume 4): annualized drift at full trend
# conviction (direction=+/-1, strength=1), and the fallback annualized
# volatility assumption when expected_volatility_pct has no data yet.
MU_ANNUAL_SCALE = 0.30
DEFAULT_VOLATILITY_ANNUAL_PCT = 20.0

FIXED_HORIZON_MINUTES: dict[str, float] = {
    "5min": 5.0,
    "15min": 15.0,
    "30min": 30.0,
    "1hour": 60.0,
}


def _minutes_remaining_in_session(now: datetime) -> float:
    """Minutes left until today's 15:30 IST close; before open returns the
    full session; after close floors at MIN_HORIZON_MINUTES (a meaningless
    but non-crashing "end of day" horizon outside trading hours)."""
    ist_now = now.astimezone(IST)
    minutes_of_day = ist_now.hour * 60 + ist_now.minute + ist_now.second / 60
    if minutes_of_day < SESSION_OPEN_MINUTES:
        return float(SESSION_MINUTES)
    return max(SESSION_CLOSE_MINUTES - minutes_of_day, MIN_HORIZON_MINUTES)


def horizon_minutes_for(now: datetime) -> dict[str, float]:
    """The 6 doc-specified horizons, in order, with end_of_day/next_day
    computed from the current session clock rather than fixed constants —
    same-day trading needs "end of day" to actually mean the rest of today."""
    horizons = dict(FIXED_HORIZON_MINUTES)
    end_of_day = _minutes_remaining_in_session(now)
    horizons["end_of_day"] = end_of_day
    horizons["next_day"] = end_of_day + SESSION_MINUTES
    return horizons


def compute_drift_and_volatility(
    features: Mapping[str, float],
) -> tuple[float, float, float, float]:
    """(mu_annual, sigma_annual, trend_confidence, volatility_confidence)
    from a feature snapshot, by calling Volume 4's own pure assessment
    functions directly on the frozen values."""
    trend_result = assess_trend(features)  # no direction_history -> age/stability
    volatility_result = assess_volatility(features)  # degrade; direction/strength stay real

    mu_annual = (
        trend_result.metrics["trend_direction"]
        * trend_result.metrics["trend_strength"]
        * MU_ANNUAL_SCALE
    )

    expected_vol_pct = volatility_result.metrics.get("expected_volatility_pct")
    if expected_vol_pct is not None:
        sigma_annual = expected_vol_pct / 100.0
        volatility_confidence = volatility_result.confidence
    else:
        sigma_annual = DEFAULT_VOLATILITY_ANNUAL_PCT / 100.0
        volatility_confidence = volatility_result.confidence * 0.5  # docked: fallback used

    return mu_annual, sigma_annual, trend_result.confidence, volatility_confidence


def probability_up(mu_annual: float, sigma_annual: float, horizon_minutes: float) -> float:
    """P(price higher at the end of the horizon than now) under a
    geometric-Brownian-motion assumption with drift mu_annual and
    volatility sigma_annual (both annualized)."""
    sigma = max(sigma_annual, MIN_SIGMA)
    years = max(horizon_minutes, MIN_HORIZON_MINUTES) / SESSION_MINUTES / TRADING_DAYS
    mean_log_return = (mu_annual - 0.5 * sigma * sigma) * years
    std_log_return = sigma * math.sqrt(years)
    if std_log_return <= 0:
        return 1.0 if mean_log_return > 0 else (0.0 if mean_log_return < 0 else 0.5)
    z = mean_log_return / std_log_return
    return NormalDist().cdf(z)


@dataclass(frozen=True)
class HorizonProbability:
    horizon: str
    horizon_minutes: float
    probability_up: float
    confidence: float
    drift_annual: float
    volatility_annual: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "horizon": self.horizon,
            "horizon_minutes": self.horizon_minutes,
            "probability_up": self.probability_up,
            "confidence": self.confidence,
            "drift_annual": self.drift_annual,
            "volatility_annual": self.volatility_annual,
        }


@dataclass
class MultiHorizonPrediction:
    symbol: str
    snapshot_id: str
    as_of: datetime
    horizons: list[HorizonProbability] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of.isoformat(),
            "horizons": [h.to_dict() for h in self.horizons],
        }


class MultiHorizonPredictionEngine:
    name = "multi_horizon_prediction_engine"

    def __init__(
        self,
        session_factory: Any = None,
        settings: Settings | None = None,
        bus: EventBus | None = None,
        snapshot_engine: FeatureSnapshotEngine | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings or get_settings()
        self._bus = bus
        self._snapshots = snapshot_engine or FeatureSnapshotEngine(
            session_factory=session_factory, settings=self._settings
        )

    def predict_from_snapshot(self, snapshot: FeatureSnapshot) -> MultiHorizonPrediction:
        """Pure computation from an already-frozen snapshot — no DB access,
        no live read. This IS the "Snapshot -> Prediction" flow: re-running
        this against the same persisted snapshot always reproduces the same
        prediction exactly."""
        mu, sigma, trend_confidence, volatility_confidence = compute_drift_and_volatility(
            snapshot.feature_values
        )
        confidence = round(0.5 * trend_confidence + 0.5 * volatility_confidence, 4)

        horizons = [
            HorizonProbability(
                horizon=name,
                horizon_minutes=minutes,
                probability_up=round(probability_up(mu, sigma, minutes), 4),
                confidence=confidence,
                drift_annual=round(mu, 4),
                volatility_annual=round(sigma, 4),
            )
            for name, minutes in horizon_minutes_for(snapshot.as_of).items()
        ]
        return MultiHorizonPrediction(
            symbol=snapshot.symbol,
            snapshot_id=snapshot.snapshot_id,
            as_of=snapshot.as_of,
            horizons=horizons,
        )

    async def predict(self, symbol: str) -> MultiHorizonPrediction:
        """Convenience: capture a fresh snapshot, then predict from it."""
        snapshot = await self._snapshots.capture(symbol)
        prediction = self.predict_from_snapshot(snapshot)
        await self._persist(prediction)
        return prediction

    async def _persist(self, prediction: MultiHorizonPrediction) -> None:
        """Every probability stored independently: one row per horizon,
        not one bundled blob, so "all 5min probabilities for NIFTY over
        time" is a direct query."""
        payloads = [
            {
                "symbol": prediction.symbol,
                "snapshot_id": prediction.snapshot_id,
                "as_of": prediction.as_of.isoformat(),
                **horizon_probability.to_dict(),
            }
            for horizon_probability in prediction.horizons
        ]
        if self._bus is not None:
            for payload in payloads:
                await self._bus.publish(
                    Event(type=EVENT_TYPE, payload=payload, source=self.name)
                )
        if self._sessions is None:
            return
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            for payload in payloads:
                session.add(MarketEvent(
                    event_type=EVENT_TYPE,
                    source=self.name,
                    data=payload,
                ))
            await session.commit()

    async def recent(
        self, symbol: str | None = None, horizon: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        if self._sessions is None:
            return []
        from sqlalchemy import desc, select

        from app.database.tables import MarketEvent

        query = select(MarketEvent.data).where(MarketEvent.event_type == EVENT_TYPE)
        if symbol is not None:
            query = query.where(MarketEvent.data["symbol"].astext == symbol)
        if horizon is not None:
            query = query.where(MarketEvent.data["horizon"].astext == horizon)
        query = query.order_by(desc(MarketEvent.id)).limit(limit)
        async with self._sessions() as session:
            result = await session.execute(query)
            return list(result.scalars().all())
