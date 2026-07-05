"""Macro intelligence collector (Volume 2, Chapter 13, Prompt 2.8).

Collects global macro factors (currency, rates, commodities, global equity
indices, optional crypto market cap) through an injectable :class:`MacroSource`
and normalizes each into a bounded, India-equity-impact-signed factor score.
Also emits a composite **Macro Pressure Score** (weighted average of the
per-factor scores).

Market data is NEVER fabricated: the default source is unconfigured and raises
``CollectionError`` so the collector reports "degraded" instead of emitting
synthetic values.
"""

from abc import ABC, abstractmethod

from app.collectors.base import BaseCollector, CollectionError
from app.collectors.schema import CollectorCategory, CollectorOutput, Direction

MacroFactorPayload = dict[str, float | None]

# India-equity-impact sign conventions. Each factor's raw 20-day z-score is
# multiplied by this sign so that POSITIVE scores always mean supportive
# conditions for Indian equities and NEGATIVE scores mean macro pressure.
#
#   USDINR   -1  rising USDINR = weaker rupee = FII outflow pressure
#   DXY      -1  strong dollar = EM risk-off, imported inflation
#   US10Y    -1  rising US yields = EM outflows, higher discount rates
#   INDIA10Y -1  rising domestic yields = tighter financial conditions
#   CRUDE    -1  India is a net crude importer (CAD / inflation pressure)
#   GOLD     -1  rising gold = risk-off tilt (safe-haven demand)
#   SILVER   -1  precious-metal risk-off proxy (weaker signal than gold)
#   NATGAS   -1  rising energy import costs
#   SPX/NDX/NIKKEI/HANGSENG/DAX  +1  rising global equities = risk-on
#   CRYPTO_MCAP +1  optional speculative risk-appetite proxy
FACTOR_SIGNS: dict[str, float] = {
    "USDINR": -1.0,
    "DXY": -1.0,
    "US10Y": -1.0,
    "INDIA10Y": -1.0,
    "CRUDE": -1.0,
    "GOLD": -1.0,
    "SILVER": -1.0,
    "NATGAS": -1.0,
    "SPX": 1.0,
    "NDX": 1.0,
    "NIKKEI": 1.0,
    "HANGSENG": 1.0,
    "DAX": 1.0,
    "CRYPTO_MCAP": 1.0,
}

# Composite Macro Pressure Score weights. The composite is a weighted average
# over the factors actually present in a fetch (renormalized by the sum of the
# present factors' weights), so missing optional factors do not bias the score.
FACTOR_WEIGHTS: dict[str, float] = {
    "USDINR": 0.15,
    "DXY": 0.10,
    "US10Y": 0.10,
    "INDIA10Y": 0.10,
    "CRUDE": 0.15,
    "GOLD": 0.05,
    "SILVER": 0.03,
    "NATGAS": 0.04,
    "SPX": 0.08,
    "NDX": 0.05,
    "NIKKEI": 0.04,
    "HANGSENG": 0.04,
    "DAX": 0.04,
    "CRYPTO_MCAP": 0.03,
}

ZSCORE_CLAMP = 3.0  # z-scores are clamped to [-3, 3] then scaled to [-1, 1]
DIRECTION_EPSILON = 0.05  # |score| below this is considered NEUTRAL
COMPOSITE_INSTRUMENT = "MACRO_PRESSURE"


class MacroSource(ABC):
    """Async source of raw macro factor readings.

    ``fetch_macro`` returns ``{factor_name: {value, change_1d_pct, zscore_20d}}``
    for the factors in :data:`FACTOR_SIGNS` (unknown factors are ignored,
    missing optional factors are fine).
    """

    @abstractmethod
    async def fetch_macro(self) -> dict[str, MacroFactorPayload]: ...


class UnconfiguredMacroSource(MacroSource):
    """Default source: refuses to run rather than fabricate market data."""

    async def fetch_macro(self) -> dict[str, MacroFactorPayload]:
        raise CollectionError("macro source not configured")


class MacroIntelligenceCollector(BaseCollector):
    """Normalize macro factors into signed scores plus a Macro Pressure Score."""

    name = "macro_intelligence"
    category = CollectorCategory.MACRO
    source = "macro_feed"
    interval_seconds = 300
    priority = 20

    def __init__(self, macro_source: MacroSource | None = None) -> None:
        super().__init__()
        self._source: MacroSource = macro_source or UnconfiguredMacroSource()

    async def collect(self) -> list[CollectorOutput]:
        raw = await self._source.fetch_macro()

        records: list[CollectorOutput] = []
        components: dict[str, float] = {}
        for factor, payload in raw.items():
            sign = FACTOR_SIGNS.get(factor)
            if sign is None:
                self.logger.warning("skipping unknown macro factor", extra={"factor": factor})
                continue
            score = self._factor_score(sign, payload)
            if score is None:
                self.logger.warning("macro factor has no usable data", extra={"factor": factor})
                continue
            components[factor] = score
            records.append(
                CollectorOutput(
                    collector_name=self.name,
                    collector_category=self.category,
                    source=self.source,
                    instrument=factor,
                    exchange="GLOBAL",
                    raw_value=payload.get("value"),
                    normalized_value=score,
                    direction=self._direction(score),
                    confidence=0.8,
                    metadata={
                        "value": payload.get("value"),
                        "change_1d_pct": payload.get("change_1d_pct"),
                        "zscore_20d": payload.get("zscore_20d"),
                        "sign": sign,
                        "weight": FACTOR_WEIGHTS[factor],
                    },
                )
            )

        if not components:
            raise CollectionError("macro source returned no recognized factors")

        records.append(self._composite_record(components))
        return records

    @staticmethod
    def _factor_score(sign: float, payload: MacroFactorPayload) -> float | None:
        """Signed, bounded factor score in [-1, 1].

        Prefers the 20-day z-score (clamped to [-3, 3], scaled to [-1, 1]).
        Falls back to the 1-day % change under the same clamp/scale when the
        z-score is unavailable. Returns None when neither input exists.
        """
        magnitude = payload.get("zscore_20d")
        if magnitude is None:
            magnitude = payload.get("change_1d_pct")
        if magnitude is None:
            return None
        clamped = max(-ZSCORE_CLAMP, min(ZSCORE_CLAMP, float(magnitude)))
        return sign * clamped / ZSCORE_CLAMP

    @staticmethod
    def _direction(score: float) -> Direction:
        if score > DIRECTION_EPSILON:
            return Direction.BULLISH
        if score < -DIRECTION_EPSILON:
            return Direction.BEARISH
        return Direction.NEUTRAL

    def _composite_record(self, components: dict[str, float]) -> CollectorOutput:
        """Weighted-average Macro Pressure Score over the factors present."""
        weights = {factor: FACTOR_WEIGHTS[factor] for factor in components}
        total_weight = sum(weights.values())
        composite = sum(components[f] * w for f, w in weights.items()) / total_weight
        coverage = total_weight / sum(FACTOR_WEIGHTS.values())
        return CollectorOutput(
            collector_name=self.name,
            collector_category=self.category,
            source=self.source,
            instrument=COMPOSITE_INSTRUMENT,
            exchange="GLOBAL",
            raw_value=composite,
            normalized_value=composite,
            direction=self._direction(composite),
            confidence=round(0.5 + 0.4 * coverage, 3),
            metadata={
                "components": components,
                "weights_used": weights,
                "coverage": round(coverage, 4),
            },
        )
