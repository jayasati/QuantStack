"""Market breadth collector (Volume 2, Chapter 10, Prompt 2.5).

Computes advance/decline statistics, new 52-week highs/lows, percentage of the
universe above key EMAs, equal-weight vs cap-weight index returns, breadth
momentum/divergence, and a composite Breadth Score with trend and confidence.

The universe snapshot comes from an injectable ``BreadthSource``; the default
source is unconfigured and raises — market data is never fabricated.
"""

from abc import ABC, abstractmethod
from typing import Any

from app.collectors.base import BaseCollector, CollectionError
from app.collectors.schema import CollectorCategory, CollectorOutput, Direction

REQUIRED_FIELDS = (
    "symbol",
    "last",
    "prev_close",
    "ema20",
    "ema50",
    "ema100",
    "ema200",
    "high_252",
    "low_252",
    "volume",
    "mcap",
)


class BreadthSource(ABC):
    """Provides a point-in-time snapshot of the tracked stock universe."""

    @abstractmethod
    async def fetch_universe(self) -> list[dict]:
        """Return one row per stock with keys: symbol, last, prev_close,
        ema20, ema50, ema100, ema200, high_252, low_252, volume, mcap."""


class UnconfiguredBreadthSource(BreadthSource):
    """Default placeholder source. Real market data must be wired in."""

    async def fetch_universe(self) -> list[dict]:
        raise CollectionError("breadth source not configured")


def _sign_direction(value: float) -> Direction:
    if value > 0:
        return Direction.BULLISH
    if value < 0:
        return Direction.BEARISH
    return Direction.NEUTRAL


class MarketBreadthCollector(BaseCollector):
    """Market-wide breadth metrics and composite breadth score."""

    name = "market_breadth"
    category = CollectorCategory.BREADTH
    source = "breadth_universe"
    interval_seconds = 60
    priority = 15

    def __init__(self, breadth_source: BreadthSource | None = None) -> None:
        super().__init__()
        self._breadth_source = breadth_source or UnconfiguredBreadthSource()
        self._ad_line = 0.0  # cumulative advance-decline line across runs

    async def collect(self) -> list[CollectorOutput]:
        universe = await self._breadth_source.fetch_universe()
        rows = [row for row in universe if all(field in row for field in REQUIRED_FIELDS)]
        if not rows:
            raise CollectionError("breadth universe is empty or malformed")
        return self._build_records(rows)

    # --- computation -----------------------------------------------------------

    def _build_records(self, rows: list[dict]) -> list[CollectorOutput]:
        n = len(rows)
        advances = sum(1 for r in rows if r["last"] > r["prev_close"])
        declines = sum(1 for r in rows if r["last"] < r["prev_close"])
        unchanged = n - advances - declines

        ad_ratio = advances / declines if declines else float(advances)
        ad_delta = float(advances - declines)
        self._ad_line += ad_delta

        new_highs = sum(1 for r in rows if r["last"] >= r["high_252"])
        new_lows = sum(1 for r in rows if r["last"] <= r["low_252"])

        pct_above = {
            ema: 100.0 * sum(1 for r in rows if r["last"] > r[ema]) / n
            for ema in ("ema20", "ema50", "ema100", "ema200")
        }

        returns = [
            (r["last"] - r["prev_close"]) / r["prev_close"] * 100.0
            for r in rows
            if r["prev_close"]
        ]
        equal_weight_return = sum(returns) / len(returns) if returns else 0.0
        total_mcap = sum(r["mcap"] for r in rows if r["prev_close"])
        cap_weight_return = (
            sum(
                r["mcap"] * (r["last"] - r["prev_close"]) / r["prev_close"] * 100.0
                for r in rows
                if r["prev_close"]
            )
            / total_mcap
            if total_mcap
            else 0.0
        )
        # Positive when the average stock outperforms the cap-weighted index
        # (broad participation); negative when a few large caps mask weakness.
        divergence = equal_weight_return - cap_weight_return

        momentum = ad_delta / n  # advances minus declines, normalized to [-1, 1]

        components = {
            "advancer_pct": 100.0 * advances / n,
            "ema_breadth": sum(pct_above.values()) / len(pct_above),
            "highs_lows": 50.0 + 50.0 * (new_highs - new_lows) / max(new_highs + new_lows, 1),
            "momentum": 50.0 + 50.0 * momentum,
        }
        score = sum(components.values()) / len(components)
        score = min(max(score, 0.0), 100.0)

        if score >= 60.0:
            trend = Direction.BULLISH
        elif score <= 40.0:
            trend = Direction.BEARISH
        else:
            trend = Direction.NEUTRAL

        signals = [(value - 50.0) / 50.0 for value in components.values()]
        strength = sum(abs(s) for s in signals)
        confidence = abs(sum(signals)) / strength if strength else 0.5
        confidence = min(max(confidence, 0.0), 1.0)

        records = [
            self._metric("advances", float(advances), Direction.NEUTRAL),
            self._metric("declines", float(declines), Direction.NEUTRAL),
            self._metric("unchanged", float(unchanged), Direction.NEUTRAL),
            self._metric("ad_ratio", ad_ratio, _sign_direction(ad_ratio - 1.0)),
            self._metric(
                "ad_line_delta",
                ad_delta,
                _sign_direction(ad_delta),
                metadata={"ad_line": self._ad_line},
            ),
            self._metric("new_highs_52w", float(new_highs), Direction.NEUTRAL),
            self._metric("new_lows_52w", float(new_lows), Direction.NEUTRAL),
            self._metric(
                "equal_weight_return", equal_weight_return, _sign_direction(equal_weight_return)
            ),
            self._metric(
                "cap_weight_return", cap_weight_return, _sign_direction(cap_weight_return)
            ),
            self._metric("breadth_divergence", divergence, _sign_direction(divergence)),
            self._metric("breadth_momentum", momentum, _sign_direction(momentum)),
        ]
        records.extend(
            self._metric(
                f"pct_above_{ema}", pct_above[ema], _sign_direction(pct_above[ema] - 50.0)
            )
            for ema in ("ema20", "ema50", "ema100", "ema200")
        )
        records.append(
            self._metric(
                "breadth_score",
                round(score, 2),
                trend,
                confidence=round(confidence, 4),
                metadata={
                    "components": {k: round(v, 2) for k, v in components.items()},
                    "advances": advances,
                    "declines": declines,
                    "unchanged": unchanged,
                    "ad_ratio": round(ad_ratio, 4),
                    "ad_line": self._ad_line,
                    "new_highs_52w": new_highs,
                    "new_lows_52w": new_lows,
                    "pct_above": {k: round(v, 2) for k, v in pct_above.items()},
                    "equal_weight_return": round(equal_weight_return, 4),
                    "cap_weight_return": round(cap_weight_return, 4),
                    "breadth_divergence": round(divergence, 4),
                    "breadth_momentum": round(momentum, 4),
                    "universe_size": n,
                    "breadth_trend": trend.value,
                },
            )
        )
        return records

    def _metric(
        self,
        metric: str,
        value: float,
        direction: Direction,
        confidence: float = 0.9,
        metadata: dict[str, Any] | None = None,
    ) -> CollectorOutput:
        return CollectorOutput(
            collector_name=self.name,
            collector_category=self.category,
            source=self.source,
            instrument="MARKET",
            raw_value=value,
            normalized_value=value,
            direction=direction,
            confidence=confidence,
            metadata={"metric": metric, **(metadata or {})},
        )
