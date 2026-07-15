"""Institutional flow collector (Volume 2, Chapter 12, Prompt 2.7).

Collects FII/DII cash flows, ETF flows, block/bulk deals, promoter activity,
SAST filings, and insider transactions (all values in INR crore). Each feature
is normalized into a standardized score in [-1, 1]; a weighted composite
Institutional Participation Index (0-100, 50 = neutral) summarizes them.

Flow data comes from an injectable ``FlowSource``; the default source is
unconfigured and raises — institutional flows are never fabricated.
"""

from abc import ABC, abstractmethod
from typing import Any

from app.collectors.base import BaseCollector, CollectionError
from app.collectors.schema import CollectorCategory, CollectorOutput, Direction

REQUIRED_FIELDS = ("fii_cash_cr", "dii_cash_cr", "fii_20d_avg_cr", "dii_20d_avg_cr")

# Weighted components of the Institutional Participation Index. Each component
# score lives in [-1, 1]; the index maps the weighted sum onto 0-100 (50 = neutral).
PARTICIPATION_WEIGHTS: dict[str, float] = {
    "fii_flow": 0.30,
    "dii_flow": 0.20,
    "etf_flow": 0.10,
    "deal_activity": 0.15,
    "promoter_net": 0.15,
    "insider_net": 0.10,
}

# Scale constants (INR crore) used to normalize magnitude-style features.
DEAL_SCALE_CR = 500.0  # a single deal of this size saturates its score
INSIDER_SCALE_CR = 100.0  # net insider buying/selling that saturates its score
SAST_SCALE = 10.0  # SAST filing count that saturates the activity score
_MIN_AVG_CR = 1.0  # floor so tiny/zero averages never explode the ratio


class FlowSource(ABC):
    """Provides one snapshot of institutional flow data (values in INR crore)."""

    @abstractmethod
    async def fetch_flows(self) -> dict:
        """Return a dict with keys: fii_cash_cr, dii_cash_cr, fii_20d_avg_cr,
        dii_20d_avg_cr, etf_flows_cr (optional), block_deals and bulk_deals
        (lists of {symbol, side, value_cr}), promoter_buys_cr,
        promoter_sells_cr, sast_filings, insider_net_cr."""


class UnconfiguredFlowSource(FlowSource):
    """Default placeholder source. A real flow feed must be wired in."""

    async def fetch_flows(self) -> dict:
        raise CollectionError("institutional flow source not configured")


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return min(max(value, low), high)


def _sign_direction(value: float) -> Direction:
    if value > 0:
        return Direction.BULLISH
    if value < 0:
        return Direction.BEARISH
    return Direction.NEUTRAL


def _flow_score(today_cr: float, avg_20d_cr: float) -> float:
    """Today's net flow relative to its 20-day average magnitude, clamped."""
    return _clamp(today_cr / max(abs(avg_20d_cr), _MIN_AVG_CR))


def _data_age_days(as_of: str | None) -> int | None:
    """Age in days of the flow snapshot (0 = today), from NSE's dd-Mon-yyyy date."""
    if not as_of:
        return None
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        published = datetime.strptime(as_of, "%d-%b-%Y").date()
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        return max((today - published).days, 0)
    except ValueError:
        return None


def _net_over_gross(net_cr: float, gross_cr: float) -> float:
    """Net activity normalized by gross activity — 0 when there is no activity."""
    return _clamp(net_cr / gross_cr) if gross_cr > 0 else 0.0


class InstitutionalFlowCollector(BaseCollector):
    """FII/DII/ETF flows, deals, promoter and insider activity, participation index."""

    name = "institutional_flow"
    category = CollectorCategory.INSTITUTIONAL_FLOW
    source = "institutional_flows"
    interval_seconds = 3600
    priority = 20
    # FII/DII cash flows, deal/promoter/insider filings all publish
    # end-of-day, same reasoning as DeliveryCollector -- running while the
    # market's open just checks for data that isn't out yet.
    after_hours_only = True

    def __init__(self, flow_source: FlowSource | None = None) -> None:
        super().__init__()
        if flow_source is None:
            from app.collectors.sources.nse_flows import NseFlowSource

            flow_source = NseFlowSource()
        self._flow_source = flow_source

    async def cleanup(self) -> None:
        closer = getattr(self._flow_source, "close", None)
        if closer is not None:
            await closer()

    async def collect(self) -> list[CollectorOutput]:
        flows = await self._flow_source.fetch_flows()
        missing = [field for field in REQUIRED_FIELDS if field not in flows]
        if missing:
            raise CollectionError(f"flow snapshot missing fields: {', '.join(missing)}")
        return self._build_records(flows)

    # --- computation -----------------------------------------------------------

    def _build_records(self, flows: dict) -> list[CollectorOutput]:
        fii_cr = float(flows["fii_cash_cr"])
        dii_cr = float(flows["dii_cash_cr"])
        fii_avg_cr = float(flows["fii_20d_avg_cr"])
        dii_avg_cr = float(flows["dii_20d_avg_cr"])
        etf_cr = flows.get("etf_flows_cr")
        block_deals: list[dict] = list(flows.get("block_deals") or [])
        bulk_deals: list[dict] = list(flows.get("bulk_deals") or [])
        promoter_buys_cr = float(flows.get("promoter_buys_cr", 0.0))
        promoter_sells_cr = float(flows.get("promoter_sells_cr", 0.0))
        sast_filings = int(flows.get("sast_filings", 0))
        insider_net_cr = float(flows.get("insider_net_cr", 0.0))

        fii_score = _flow_score(fii_cr, fii_avg_cr)
        dii_score = _flow_score(dii_cr, dii_avg_cr)

        deal_net_cr, deal_gross_cr = self._deal_totals(block_deals + bulk_deals)
        deal_score = _net_over_gross(deal_net_cr, deal_gross_cr)

        promoter_net_cr = promoter_buys_cr - promoter_sells_cr
        promoter_gross_cr = promoter_buys_cr + promoter_sells_cr
        promoter_score = _net_over_gross(promoter_net_cr, promoter_gross_cr)

        insider_score = _clamp(insider_net_cr / INSIDER_SCALE_CR)
        sast_score = _clamp(sast_filings / SAST_SCALE, 0.0, 1.0)

        component_scores: dict[str, float] = {
            "fii_flow": fii_score,
            "dii_flow": dii_score,
            "deal_activity": deal_score,
            "promoter_net": promoter_score,
            "insider_net": insider_score,
        }
        if etf_cr is not None:
            # No dedicated ETF baseline in the feed — use the FII 20-day average
            # magnitude as the market-scale reference for ETF flows.
            component_scores["etf_flow"] = _flow_score(float(etf_cr), fii_avg_cr)

        index = self._participation_index(component_scores)
        index_direction = _sign_direction(round(index - 50.0, 6))

        records = [
            self._feature("fii_flow", fii_cr, fii_score, extra={"fii_20d_avg_cr": fii_avg_cr}),
            self._feature("dii_flow", dii_cr, dii_score, extra={"dii_20d_avg_cr": dii_avg_cr}),
        ]
        if etf_cr is not None:
            records.append(self._feature("etf_flow", float(etf_cr), component_scores["etf_flow"]))
        records.extend(self._deal_record("block_deal", deal) for deal in block_deals)
        records.extend(self._deal_record("bulk_deal", deal) for deal in bulk_deals)
        records.append(
            self._feature(
                "promoter_net",
                promoter_net_cr,
                promoter_score,
                extra={
                    "promoter_buys_cr": promoter_buys_cr,
                    "promoter_sells_cr": promoter_sells_cr,
                },
            )
        )
        records.append(
            self._feature(
                "sast_filings",
                float(sast_filings),
                sast_score,
                direction=Direction.NEUTRAL,
            )
        )
        records.append(self._feature("insider_net", insider_net_cr, insider_score))
        records.append(
            CollectorOutput(
                collector_name=self.name,
                collector_category=self.category,
                source=self.source,
                instrument="MARKET",
                raw_value=round(index, 2),
                normalized_value=_clamp((index - 50.0) / 50.0),
                direction=index_direction,
                confidence=0.9,
                metadata={
                    "metric": "participation_index",
                    "components": {k: round(v, 4) for k, v in component_scores.items()},
                    "weights": {
                        k: PARTICIPATION_WEIGHTS[k]
                        for k in component_scores
                        if k in PARTICIPATION_WEIGHTS
                    },
                    "deal_net_cr": round(deal_net_cr, 2),
                    "deal_gross_cr": round(deal_gross_cr, 2),
                    "sast_filings": sast_filings,
                    "block_deal_count": len(block_deals),
                    "bulk_deal_count": len(bulk_deals),
                },
            )
        )
        as_of = flows.get("as_of")
        age_days = _data_age_days(as_of)
        for record in records:
            record.metadata["as_of"] = as_of
            if age_days is not None:
                record.metadata["data_age_days"] = age_days
        return records

    @staticmethod
    def _participation_index(component_scores: dict[str, float]) -> float:
        """Weighted [-1, 1] composite mapped onto 0-100 (50 = neutral)."""
        weighted = 0.0
        total_weight = 0.0
        for component, score in component_scores.items():
            weight = PARTICIPATION_WEIGHTS.get(component, 0.0)
            weighted += weight * score
            total_weight += weight
        composite = weighted / total_weight if total_weight else 0.0
        return _clamp(50.0 + 50.0 * composite, 0.0, 100.0)

    @staticmethod
    def _deal_totals(deals: list[dict]) -> tuple[float, float]:
        """Signed net and gross deal value in INR crore across block + bulk deals."""
        net = 0.0
        gross = 0.0
        for deal in deals:
            value = abs(float(deal.get("value_cr", 0.0)))
            sign = 1.0 if str(deal.get("side", "")).lower() == "buy" else -1.0
            net += sign * value
            gross += value
        return net, gross

    def _deal_record(self, metric: str, deal: dict) -> CollectorOutput:
        value_cr = abs(float(deal.get("value_cr", 0.0)))
        side = str(deal.get("side", "")).lower()
        signed = value_cr if side == "buy" else -value_cr
        score = _clamp(signed / DEAL_SCALE_CR)
        return CollectorOutput(
            collector_name=self.name,
            collector_category=self.category,
            source=self.source,
            instrument=str(deal.get("symbol", "MARKET")),
            raw_value=value_cr,
            normalized_value=score,
            direction=_sign_direction(signed),
            confidence=0.85,
            metadata={"metric": metric, "side": side, "value_cr": value_cr},
        )

    def _feature(
        self,
        metric: str,
        raw_cr: float,
        score: float,
        direction: Direction | None = None,
        extra: dict[str, Any] | None = None,
    ) -> CollectorOutput:
        return CollectorOutput(
            collector_name=self.name,
            collector_category=self.category,
            source=self.source,
            instrument="MARKET",
            raw_value=raw_cr,
            normalized_value=round(score, 4),
            direction=_sign_direction(score) if direction is None else direction,
            confidence=0.85,
            metadata={"metric": metric, **(extra or {})},
        )
