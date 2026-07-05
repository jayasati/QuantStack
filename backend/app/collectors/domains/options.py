"""Options intelligence collector (Volume 2, Chapter 9, Prompt 2.4).

Derives normalized option-chain features — PCR, max pain, ATM IV, IV skew,
OI concentration, buildup classification, writing intensity, and exposure
proxies — instead of publishing raw chain rows. The chain itself comes from
an injectable ``OptionsChainSource``; the default is the public NSE
option-chain feed (``NseOptionChainSource``).
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
                    "call": {"oi", "oi_change", "iv", "volume", "ltp"[, "gamma", "delta"]},
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

    def __init__(self, source: OptionsChainSource | None = None) -> None:
        super().__init__()
        if source is None:
            from app.collectors.sources.nse_options import NseOptionChainSource

            source = NseOptionChainSource()
        self.chain_source: OptionsChainSource = source
        self.symbols: list[str] = get_settings().watchlist

    async def cleanup(self) -> None:
        closer = getattr(self.chain_source, "close", None)
        if closer is not None:
            await closer()

    async def collect(self) -> list[CollectorOutput]:
        records: list[CollectorOutput] = []
        for symbol in self.symbols:
            chain = await self.chain_source.fetch_chain(symbol)
            records.extend(self._derive_features(symbol, chain))
        return records

    # --- feature derivation -----------------------------------------------------

    def _derive_features(self, instrument: str, chain: dict[str, Any]) -> list[CollectorOutput]:
        spot, prev_spot, rows = _parse_chain(chain)
        records: list[CollectorOutput] = []

        records.extend(self._pcr(instrument, rows))
        records.extend(self._max_pain_feature(instrument, spot, rows))
        records.extend(self._atm_iv(instrument, spot, rows))
        records.extend(self._iv_skew(instrument, spot, rows))
        records.extend(self._oi_concentration(instrument, rows))
        records.extend(self._buildup(instrument, spot, prev_spot, rows))
        records.extend(self._writing_intensity(instrument, rows))
        records.extend(self._exposure_proxies(instrument, rows))
        return records

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
