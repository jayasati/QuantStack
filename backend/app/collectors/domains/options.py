"""Options intelligence collector (Volume 2, Chapter 9, Prompt 2.4).

Derives normalized option-chain features — PCR, max pain, ATM IV, IV skew,
OI concentration, buildup classification, writing intensity, chain-wide
exposure proxies, and ATM Greeks risk (Theta burn %, Gamma, Vega) — instead
of publishing raw chain rows. The chain itself comes from an injectable
``OptionsChainSource``; the default is the public NSE option-chain feed
(``NseOptionChainSource``).

ATM Greeks risk (same-day F&O gap fill, 2026-07-09) is generic/instrument-
level, not position-level: this codebase has no open-position tracking yet,
so "Theta burn" here means "at the current ATM strike right now," not "on
your specific trade."
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from statistics import fmean
from typing import Any

from app.collectors.base import BaseCollector, CollectionError
from app.collectors.schema import CollectorCategory, CollectorOutput, Direction
from app.core.config import get_settings


class OptionsChainSource(ABC):
    """Async provider of one option-chain snapshot per underlying.

    Expected chain shape::

        {
            "spot": float,
            "prev_spot": float,            # optional; enables buildup classification
            "strikes": [
                {
                    "strike": float,
                    "call": {"oi", "oi_change", "iv", "volume", "ltp"
                             "[, gamma, delta, theta, vega]"},
                    "put":  {...same keys...},
                },
                ...
            ],
        }
    """

    @abstractmethod
    async def fetch_chain(self, instrument: str) -> dict[str, Any]:
        """Return the current option chain for ``instrument``."""


class UnconfiguredOptionsSource(OptionsChainSource):
    """Default source: always fails. A real chain provider must be wired in."""

    async def fetch_chain(self, instrument: str) -> dict[str, Any]:
        raise CollectionError("options chain source not configured")


class IvHistoryProvider(ABC):
    """Historical ATM-IV observations for IV percentile computation."""

    @abstractmethod
    async def history(self, instrument: str) -> list[float]:
        """Return past ATM IV observations, most recent last."""


class DbIvHistoryProvider(IvHistoryProvider):
    """Reads our own previously published atm_iv observations from market_events."""

    def __init__(self, limit: int = 5000) -> None:
        self._limit = limit

    async def history(self, instrument: str) -> list[float]:
        try:
            from sqlalchemy import text

            from app.database.session import get_session_factory

            sessions = get_session_factory()
            async with sessions() as session:
                result = await session.execute(
                    text(
                        "SELECT (data->>'normalized_value')::float FROM market_events "
                        "WHERE event_type = 'options.observation' "
                        "AND data->>'instrument' = :instrument "
                        "AND data->'metadata'->>'feature' = 'atm_iv' "
                        "ORDER BY id DESC LIMIT :limit"
                    ),
                    {"instrument": instrument, "limit": self._limit},
                )
                values = [row[0] for row in result if row[0] is not None]
            values.reverse()
            return values
        except Exception:
            return []


@dataclass(frozen=True)
class _Leg:
    """One side (call or put) of a strike row."""

    oi: float
    oi_change: float
    iv: float | None
    volume: float
    ltp: float | None
    gamma: float | None
    delta: float | None
    theta: float | None
    vega: float | None


@dataclass(frozen=True)
class _StrikeRow:
    strike: float
    call: _Leg
    put: _Leg


_BUILDUP_SCORE = {
    "long_buildup": 1.0,
    "short_covering": 0.5,
    "long_unwinding": -0.5,
    "short_buildup": -1.0,
}
_BUILDUP_DIRECTION = {
    "long_buildup": Direction.BULLISH,
    "short_covering": Direction.BULLISH,
    "long_unwinding": Direction.BEARISH,
    "short_buildup": Direction.BEARISH,
}


def _parse_leg(payload: dict[str, Any]) -> _Leg:
    def required(key: str) -> float:
        value = payload.get(key)
        return float(value) if value is not None else 0.0

    def optional(key: str) -> float | None:
        value = payload.get(key)
        return float(value) if value is not None else None

    return _Leg(
        oi=required("oi"),
        oi_change=required("oi_change"),
        iv=optional("iv"),
        volume=required("volume"),
        ltp=optional("ltp"),
        gamma=optional("gamma"),
        delta=optional("delta"),
        theta=optional("theta"),
        vega=optional("vega"),
    )


def _parse_chain(chain: dict[str, Any]) -> tuple[float, float | None, list[_StrikeRow]]:
    spot = chain.get("spot")
    strikes = chain.get("strikes") or []
    if spot is None or not strikes:
        raise CollectionError("options chain is missing spot or strikes")
    prev_spot = chain.get("prev_spot")
    rows = [
        _StrikeRow(
            strike=float(entry["strike"]),
            call=_parse_leg(entry.get("call") or {}),
            put=_parse_leg(entry.get("put") or {}),
        )
        for entry in strikes
    ]
    rows.sort(key=lambda row: row.strike)
    return float(spot), float(prev_spot) if prev_spot is not None else None, rows


def _max_pain(rows: list[_StrikeRow]) -> float | None:
    """Strike minimizing total intrinsic payout to option buyers at expiry."""
    best_strike: float | None = None
    best_pain: float | None = None
    for candidate in rows:
        pain = sum(
            row.call.oi * max(candidate.strike - row.strike, 0.0)
            + row.put.oi * max(row.strike - candidate.strike, 0.0)
            for row in rows
        )
        if best_pain is None or pain < best_pain:
            best_strike, best_pain = candidate.strike, pain
    return best_strike


class OptionsIntelligenceCollector(BaseCollector):
    """Derive normalized options-chain features for every watchlist underlying."""

    name = "options_intelligence"
    category = CollectorCategory.OPTIONS
    source = "options_chain"
    interval_seconds = 60
    priority = 10
    market_hours_only = True  # the chain does not update outside NSE hours
    # Buildup classification reads prev-day close from stored daily candles.
    depends_on = ("historical_candles",)

    # IV percentile needs a minimum history before it is meaningful.
    min_iv_observations = 100

    def __init__(
        self,
        source: OptionsChainSource | None = None,
        iv_history: IvHistoryProvider | None = None,
    ) -> None:
        super().__init__()
        if source is None:
            from app.collectors.sources.nse_options import NseOptionChainSource

            source = NseOptionChainSource()
        self.chain_source: OptionsChainSource = source
        self.iv_history: IvHistoryProvider = iv_history or DbIvHistoryProvider()
        self.symbols: list[str] = get_settings().watchlist

    async def cleanup(self) -> None:
        closer = getattr(self.chain_source, "close", None)
        if closer is not None:
            await closer()

    async def collect(self) -> list[CollectorOutput]:
        records: list[CollectorOutput] = []
        for symbol in self.symbols:
            chain = await self.chain_source.fetch_chain(symbol)
            records.extend(await self._derive_features(symbol, chain))
        return records

    # --- feature derivation -----------------------------------------------------

    async def _derive_features(
        self, instrument: str, chain: dict[str, Any]
    ) -> list[CollectorOutput]:
        spot, prev_spot, rows = _parse_chain(chain)
        records: list[CollectorOutput] = []

        records.extend(self._pcr(instrument, rows))
        records.extend(self._max_pain_feature(instrument, spot, rows))
        atm_records = self._atm_iv(instrument, spot, rows)
        records.extend(atm_records)
        records.extend(self._iv_skew(instrument, spot, rows))
        records.extend(self._oi_concentration(instrument, rows))
        records.extend(self._oi_distribution(instrument, spot, rows))
        records.extend(self._volume_distribution(instrument, rows))
        records.extend(self._buildup(instrument, spot, prev_spot, rows))
        records.extend(self._writing_intensity(instrument, rows))
        records.extend(self._exposure_proxies(instrument, rows))
        records.extend(self._atm_greeks_risk(instrument, spot, rows))
        if atm_records:
            iv_value = atm_records[0].normalized_value
            if iv_value is not None:
                records.extend(await self._iv_percentile(instrument, iv_value))
        return records

    def _oi_distribution(
        self, instrument: str, spot: float, rows: list[_StrikeRow]
    ) -> list[CollectorOutput]:
        """Positioning structure: put OI below spot (support) vs call OI above
        spot (resistance), plus the OI-weighted strike vs spot."""
        support_oi = sum(row.put.oi for row in rows if row.strike < spot)
        resistance_oi = sum(row.call.oi for row in rows if row.strike > spot)
        total = support_oi + resistance_oi
        if total <= 0:
            return []
        net = (support_oi - resistance_oi) / total  # [-1, 1]
        total_oi = sum(row.call.oi + row.put.oi for row in rows)
        weighted_strike = (
            sum(row.strike * (row.call.oi + row.put.oi) for row in rows) / total_oi
            if total_oi
            else None
        )
        if net > 0.1:
            direction = Direction.BULLISH  # heavier put wall below = support
        elif net < -0.1:
            direction = Direction.BEARISH  # heavier call wall above = resistance
        else:
            direction = Direction.NEUTRAL
        return [
            self._record(
                instrument,
                "oi_distribution",
                net,
                direction,
                {
                    "support_put_oi_below_spot": support_oi,
                    "resistance_call_oi_above_spot": resistance_oi,
                    "oi_weighted_strike": weighted_strike,
                    "spot": spot,
                },
            )
        ]

    def _volume_distribution(
        self, instrument: str, rows: list[_StrikeRow]
    ) -> list[CollectorOutput]:
        """Where option volume sits today: concentration and call/put split."""
        volumes = [(row.strike, row.call.volume + row.put.volume) for row in rows]
        total_volume = sum(volume for _, volume in volumes)
        if total_volume <= 0:
            return []
        top = sorted(volumes, key=lambda item: item[1], reverse=True)[:3]
        top_share = sum(volume for _, volume in top) / total_volume
        call_volume = sum(row.call.volume for row in rows)
        put_volume = sum(row.put.volume for row in rows)
        volume_pcr = put_volume / call_volume if call_volume else None
        weighted_strike = sum(strike * volume for strike, volume in volumes) / total_volume
        return [
            self._record(
                instrument,
                "volume_distribution",
                top_share,
                Direction.NEUTRAL,
                {
                    "top_strikes": [{"strike": s, "volume": v} for s, v in top],
                    "call_volume": call_volume,
                    "put_volume": put_volume,
                    "volume_pcr": volume_pcr,
                    "volume_weighted_strike": weighted_strike,
                },
            )
        ]

    async def _iv_percentile(
        self, instrument: str, current_iv: float
    ) -> list[CollectorOutput]:
        """Percentile of the current ATM IV within our own stored IV history.

        Emits nothing until enough observations have accumulated — the value
        is meaningless on day one and is never fabricated.
        """
        history = await self.iv_history.history(instrument)
        if len(history) < self.min_iv_observations:
            return []
        below = sum(1 for value in history if value < current_iv)
        percentile = 100.0 * below / len(history)
        return [
            self._record(
                instrument,
                "iv_percentile",
                percentile,
                Direction.NEUTRAL,
                {
                    "current_atm_iv": current_iv,
                    "observations": len(history),
                    "history_min": min(history),
                    "history_max": max(history),
                },
            )
        ]

    def _pcr(self, instrument: str, rows: list[_StrikeRow]) -> list[CollectorOutput]:
        total_call_oi = sum(row.call.oi for row in rows)
        total_put_oi = sum(row.put.oi for row in rows)
        if total_call_oi <= 0:
            return []
        pcr = total_put_oi / total_call_oi
        if pcr > 1.2:
            direction = Direction.BULLISH
        elif pcr < 0.8:
            direction = Direction.BEARISH
        else:
            direction = Direction.NEUTRAL
        return [
            self._record(
                instrument,
                "pcr",
                pcr,
                direction,
                {"total_call_oi": total_call_oi, "total_put_oi": total_put_oi},
            )
        ]

    def _max_pain_feature(
        self, instrument: str, spot: float, rows: list[_StrikeRow]
    ) -> list[CollectorOutput]:
        max_pain = _max_pain(rows)
        if max_pain is None:
            return []
        if max_pain > spot:
            direction = Direction.BULLISH
        elif max_pain < spot:
            direction = Direction.BEARISH
        else:
            direction = Direction.NEUTRAL
        return [
            self._record(
                instrument,
                "max_pain",
                max_pain,
                direction,
                {"spot": spot, "distance_from_spot": max_pain - spot},
            )
        ]

    def _atm_iv(
        self, instrument: str, spot: float, rows: list[_StrikeRow]
    ) -> list[CollectorOutput]:
        atm = min(rows, key=lambda row: abs(row.strike - spot))
        ivs = [iv for iv in (atm.call.iv, atm.put.iv) if iv is not None]
        if not ivs:
            return []
        return [
            self._record(
                instrument,
                "atm_iv",
                fmean(ivs),
                Direction.NEUTRAL,
                {"atm_strike": atm.strike, "call_iv": atm.call.iv, "put_iv": atm.put.iv},
            )
        ]

    def _iv_skew(
        self, instrument: str, spot: float, rows: list[_StrikeRow]
    ) -> list[CollectorOutput]:
        """OTM put IV minus OTM call IV by moneyness (positive = downside fear)."""
        put_ivs = [row.put.iv for row in rows if row.strike < spot and row.put.iv is not None]
        call_ivs = [row.call.iv for row in rows if row.strike > spot and row.call.iv is not None]
        if not put_ivs or not call_ivs:
            return []
        skew = fmean(put_ivs) - fmean(call_ivs)
        if skew > 0:
            direction = Direction.BEARISH
        elif skew < 0:
            direction = Direction.BULLISH
        else:
            direction = Direction.NEUTRAL
        return [
            self._record(
                instrument,
                "iv_skew",
                skew,
                direction,
                {"otm_put_iv_mean": fmean(put_ivs), "otm_call_iv_mean": fmean(call_ivs)},
            )
        ]

    def _oi_concentration(self, instrument: str, rows: list[_StrikeRow]) -> list[CollectorOutput]:
        """Share of total chain OI held by the three heaviest strikes."""
        by_oi = sorted(rows, key=lambda row: row.call.oi + row.put.oi, reverse=True)
        total = sum(row.call.oi + row.put.oi for row in rows)
        if total <= 0:
            return []
        top = by_oi[:3]
        share = sum(row.call.oi + row.put.oi for row in top) / total
        return [
            self._record(
                instrument,
                "oi_concentration",
                share,
                Direction.NEUTRAL,
                {
                    "top_strikes": [
                        {"strike": row.strike, "oi": row.call.oi + row.put.oi} for row in top
                    ],
                    "total_oi": total,
                },
            )
        ]

    def _buildup(
        self,
        instrument: str,
        spot: float,
        prev_spot: float | None,
        rows: list[_StrikeRow],
    ) -> list[CollectorOutput]:
        """Classic price-change vs OI-change classification for the underlying."""
        if prev_spot is None:
            return []
        price_change = spot - prev_spot
        oi_change = sum(row.call.oi_change + row.put.oi_change for row in rows)
        if price_change > 0:
            label = "long_buildup" if oi_change > 0 else "short_covering"
        elif price_change < 0:
            label = "short_buildup" if oi_change > 0 else "long_unwinding"
        else:
            return []
        return [
            self._record(
                instrument,
                "buildup",
                _BUILDUP_SCORE[label],
                _BUILDUP_DIRECTION[label],
                {
                    "label": label,
                    "price_change": price_change,
                    "total_oi_change": oi_change,
                    "spot": spot,
                    "prev_spot": prev_spot,
                },
                raw_value=label,
            )
        ]

    def _writing_intensity(self, instrument: str, rows: list[_StrikeRow]) -> list[CollectorOutput]:
        """Fresh OI added on each side, relative to that side's open interest."""
        records: list[CollectorOutput] = []
        sides: list[tuple[str, list[_Leg], Direction]] = [
            ("call_writing", [row.call for row in rows], Direction.BEARISH),
            ("put_writing", [row.put for row in rows], Direction.BULLISH),
        ]
        for feature, legs, active_direction in sides:
            total_oi = sum(leg.oi for leg in legs)
            if total_oi <= 0:
                continue
            added = sum(max(leg.oi_change, 0.0) for leg in legs)
            intensity = added / total_oi
            direction = active_direction if intensity > 0 else Direction.NEUTRAL
            records.append(
                self._record(
                    instrument,
                    feature,
                    intensity,
                    direction,
                    {"oi_added": added, "total_oi": total_oi},
                )
            )
        return records

    def _exposure_proxies(self, instrument: str, rows: list[_StrikeRow]) -> list[CollectorOutput]:
        """Gamma/delta exposure proxies — only when Greeks arrive with the chain."""
        gamma_terms: list[float] = []
        delta_terms: list[float] = []
        for row in rows:
            if row.call.gamma is not None and row.put.gamma is not None:
                gamma_terms.append(row.call.gamma * row.call.oi - row.put.gamma * row.put.oi)
            if row.call.delta is not None and row.put.delta is not None:
                delta_terms.append(row.call.delta * row.call.oi + row.put.delta * row.put.oi)
        records: list[CollectorOutput] = []
        if gamma_terms:
            records.append(
                self._record(
                    instrument,
                    "gamma_exposure",
                    sum(gamma_terms),
                    Direction.NEUTRAL,
                    {"strikes_with_gamma": len(gamma_terms)},
                )
            )
        if delta_terms:
            dex = sum(delta_terms)
            if dex > 0:
                direction = Direction.BULLISH
            elif dex < 0:
                direction = Direction.BEARISH
            else:
                direction = Direction.NEUTRAL
            records.append(
                self._record(
                    instrument,
                    "delta_exposure",
                    dex,
                    direction,
                    {"strikes_with_delta": len(delta_terms)},
                )
            )
        return records

    def _atm_greeks_risk(
        self, instrument: str, spot: float, rows: list[_StrikeRow]
    ) -> list[CollectorOutput]:
        """Same-day F&O risk from the Greeks at the ATM strike (where gamma
        and theta both peak): Theta burn as a % of premium, raw Gamma, raw
        Vega. Only when the chain carries Greeks -- never fabricated."""
        atm = min(rows, key=lambda row: abs(row.strike - spot))
        records: list[CollectorOutput] = []

        thetas = [t for t in (atm.call.theta, atm.put.theta) if t is not None]
        ltps = [p for p in (atm.call.ltp, atm.put.ltp) if p is not None]
        if thetas and ltps:
            avg_premium = fmean(ltps)
            if avg_premium > 0:
                theta_pct = abs(fmean(thetas)) / avg_premium * 100
                records.append(
                    self._record(
                        instrument,
                        "atm_theta_pct",
                        theta_pct,
                        Direction.NEUTRAL,
                        {
                            "atm_strike": atm.strike,
                            "call_theta": atm.call.theta,
                            "put_theta": atm.put.theta,
                            "call_ltp": atm.call.ltp,
                            "put_ltp": atm.put.ltp,
                        },
                    )
                )

        gammas = [g for g in (atm.call.gamma, atm.put.gamma) if g is not None]
        if gammas:
            records.append(
                self._record(
                    instrument,
                    "atm_gamma",
                    sum(gammas),
                    Direction.NEUTRAL,
                    {"atm_strike": atm.strike, "call_gamma": atm.call.gamma,
                     "put_gamma": atm.put.gamma},
                )
            )

        vegas = [v for v in (atm.call.vega, atm.put.vega) if v is not None]
        if vegas:
            records.append(
                self._record(
                    instrument,
                    "atm_vega",
                    sum(vegas),
                    Direction.NEUTRAL,
                    {"atm_strike": atm.strike, "call_vega": atm.call.vega,
                     "put_vega": atm.put.vega},
                )
            )
        return records

    def _record(
        self,
        instrument: str,
        feature: str,
        value: float,
        direction: Direction,
        metadata: dict[str, Any],
        raw_value: Any = None,
    ) -> CollectorOutput:
        return CollectorOutput(
            collector_name=self.name,
            collector_category=self.category,
            source=self.source,
            instrument=instrument,
            raw_value=value if raw_value is None else raw_value,
            normalized_value=value,
            direction=direction,
            confidence=0.8,
            metadata={"feature": feature, **metadata},
        )
