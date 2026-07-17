"""News Feature Engine (Volume 3, Prompt 3.10).

Aggregates the news_intelligence collector's article-level observations into
hourly news-flow features (symbol MARKET, synthetic timeframe "news"). Hours
without articles stay in the timeline so flow intensity is honest; their
content features are simply absent.

Feature conventions (per hour bucket):
- Sentiment Score: mean article sentiment (-1..1).
- Novelty Score: mean article novelty (0..1).
- Urgency: mean urgency with low=0, medium=0.5, high=1.
- News Momentum: article count relative to the trailing 24-hour mean flow
  (1 = normal, >1 = accelerating news flow).
- Entity Frequency: mean tagged entities per article (0 while the feed's
  entity extraction stays sparse — never fabricated).
- Headline Similarity: mean pairwise Jaccard similarity of headline tokens —
  high similarity means one story dominates the tape.
- Topic Distribution: normalized Shannon entropy of article categories
  (0 = single topic, 1 = evenly spread).
- Market Impact Probability: v1 heuristic from urgency, absolute sentiment,
  novelty, and flow momentum, 0.05..0.95.
- Sector Impact: share of sector/corporate/earnings-category articles.
- Stock Impact: share of articles carrying tagged stock entities.

Every feature ships a look-ahead-safe rolling z-score companion (_z).
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import fmean

from sqlalchemy import desc, select

from app.core.logging import get_logger
from app.features.base import BaseFeatureEngine
from app.features.normalize import add_normalized_series, normalized_definition
from app.features.schema import Candle, FeatureDefinition, Series

logger = get_logger(__name__)

ENGINE_NAME = "news_feature_engine"
ENGINE_VERSION = "v1"
CATEGORY = "news"

MARKET_SYMBOL = "MARKET"
NEWS_TIMEFRAME = "news"

URGENCY_SCORES = {"low": 0.0, "medium": 0.5, "high": 1.0}
SECTOR_CATEGORIES = {"sector", "corporate", "earnings"}
MOMENTUM_TRAILING_HOURS = 24
MAX_SIMILARITY_PAIRS = 200


@dataclass(frozen=True)
class Article:
    ts: datetime
    sentiment: float | None = None
    novelty: float | None = None
    urgency: str | None = None
    category: str | None = None
    title: str = ""
    entities: tuple[str, ...] = field(default_factory=tuple)


# --- Feature definitions -------------------------------------------------------

def news_feature_definitions(
    normalization_window: int,
    calculation_frequency: str = "on_schedule",
) -> list[FeatureDefinition]:
    def define(name: str, description: str, unit: str,
               expected: tuple[float | None, float | None],
               dependencies: tuple[str, ...] = (),
               ) -> FeatureDefinition:
        return FeatureDefinition(
            feature_name=name,
            category=CATEGORY,
            description=description,
            version=ENGINE_VERSION,
            dependencies=dependencies,
            calculation_frequency=calculation_frequency,
            owner=ENGINE_NAME,
            unit=unit,
            expected_range=expected,
        )

    definitions = [
        define("news_sentiment", "Mean article sentiment in the hour, -1..1.",
               "ratio", (-1.0, 1.0)),
        define("news_novelty", "Mean article novelty in the hour, 0..1.",
               "ratio", (0.0, 1.0)),
        define("news_urgency", "Mean urgency (low=0, medium=0.5, high=1).",
               "ratio", (0.0, 1.0)),
        define("news_momentum",
               f"Hourly article count vs the trailing {MOMENTUM_TRAILING_HOURS}h "
               "mean flow.",
               "ratio", (0.0, 20.0)),
        define("news_entity_frequency", "Mean tagged entities per article.",
               "count", (0.0, None)),
        define("news_headline_similarity",
               "Mean pairwise Jaccard similarity of headline tokens, 0..1.",
               "ratio", (0.0, 1.0)),
        define("news_topic_entropy",
               "Normalized entropy of article categories (0 = one topic).",
               "ratio", (0.0, 1.0)),
        define("news_impact_probability",
               "v1 heuristic from urgency, |sentiment|, novelty, and momentum.",
               "probability", (0.0, 1.0),
               ("news_urgency", "news_sentiment", "news_novelty", "news_momentum")),
        define("news_sector_impact",
               "Share of sector/corporate/earnings articles in the hour.",
               "ratio", (0.0, 1.0)),
        define("news_stock_impact",
               "Share of articles carrying tagged stock entities.",
               "ratio", (0.0, 1.0), ("news_entity_frequency",)),
    ]
    definitions.extend(
        normalized_definition(d, normalization_window) for d in list(definitions)
    )
    return definitions


# --- Pure calculations -----------------------------------------------------------

def _tokenize(title: str) -> frozenset[str]:
    return frozenset(word for word in title.lower().split() if len(word) > 2)


def _mean_pairwise_similarity(titles: Sequence[str]) -> float | None:
    token_sets = [_tokenize(t) for t in titles if t]
    token_sets = [s for s in token_sets if s]
    if len(token_sets) < 2:
        return None
    similarities: list[float] = []
    for a in range(len(token_sets)):
        for b in range(a + 1, len(token_sets)):
            union = token_sets[a] | token_sets[b]
            if union:
                similarities.append(len(token_sets[a] & token_sets[b]) / len(union))
            if len(similarities) >= MAX_SIMILARITY_PAIRS:
                return fmean(similarities)
    return fmean(similarities) if similarities else None


def _topic_entropy(categories: Sequence[str]) -> float | None:
    present = [c for c in categories if c]
    if not present:
        return None
    counts: dict[str, int] = {}
    for c in present:
        counts[c] = counts.get(c, 0) + 1
    if len(counts) == 1:
        return 0.0
    total = len(present)
    entropy = -sum(
        (count / total) * math.log(count / total) for count in counts.values()
    )
    return entropy / math.log(len(counts))


def compute_news_features(
    articles: Sequence[Article],
    normalization_window: int = 100,
) -> tuple[list[datetime], dict[str, Series]]:
    """Hourly news-flow features from a time-ordered article stream."""
    if not articles:
        return [], {}
    buckets: dict[datetime, list[Article]] = {}
    for article in articles:
        hour = article.ts.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour, []).append(article)

    first, last = min(buckets), max(buckets)
    timestamps: list[datetime] = []
    hour = first
    while hour <= last:
        timestamps.append(hour)
        hour += timedelta(hours=1)
    n = len(timestamps)

    counts = [len(buckets.get(ts, [])) for ts in timestamps]
    sentiment: Series = [None] * n
    novelty: Series = [None] * n
    urgency: Series = [None] * n
    momentum: Series = [None] * n
    entity_frequency: Series = [None] * n
    similarity: Series = [None] * n
    entropy: Series = [None] * n
    impact: Series = [None] * n
    sector_impact: Series = [None] * n
    stock_impact: Series = [None] * n

    for i, ts in enumerate(timestamps):
        trailing = counts[max(0, i - MOMENTUM_TRAILING_HOURS + 1) : i + 1]
        mean_flow = fmean(trailing)
        if mean_flow > 0:
            momentum[i] = counts[i] / mean_flow

        bucket = buckets.get(ts)
        if not bucket:
            continue
        sentiments = [a.sentiment for a in bucket if a.sentiment is not None]
        if sentiments:
            sentiment[i] = fmean(sentiments)
        novelties = [a.novelty for a in bucket if a.novelty is not None]
        if novelties:
            novelty[i] = fmean(novelties)
        urgencies = [
            URGENCY_SCORES[a.urgency] for a in bucket
            if a.urgency in URGENCY_SCORES
        ]
        if urgencies:
            urgency[i] = fmean(urgencies)
        entity_frequency[i] = fmean([float(len(a.entities)) for a in bucket])
        similarity[i] = _mean_pairwise_similarity([a.title for a in bucket])
        entropy[i] = _topic_entropy([a.category or "" for a in bucket])
        sector_impact[i] = fmean(
            [1.0 if (a.category or "") in SECTOR_CATEGORIES else 0.0 for a in bucket]
        )
        stock_impact[i] = fmean([1.0 if a.entities else 0.0 for a in bucket])

        urgency_term = urgency[i] or 0.0
        sentiment_value = sentiment[i]
        sentiment_term = abs(sentiment_value) if sentiment_value is not None else 0.0
        novelty_term = novelty[i] or 0.0
        momentum_term = min((momentum[i] or 1.0) / 3.0, 1.0)
        impact[i] = max(0.05, min(
            0.95,
            0.1 + 0.3 * urgency_term + 0.25 * sentiment_term
            + 0.2 * novelty_term + 0.15 * momentum_term,
        ))

    series: dict[str, Series] = {
        "news_sentiment": sentiment,
        "news_novelty": novelty,
        "news_urgency": urgency,
        "news_momentum": momentum,
        "news_entity_frequency": entity_frequency,
        "news_headline_similarity": similarity,
        "news_topic_entropy": entropy,
        "news_impact_probability": impact,
        "news_sector_impact": sector_impact,
        "news_stock_impact": stock_impact,
    }
    return timestamps, add_normalized_series(series, normalization_window)


# --- Engine -------------------------------------------------------------------------

class NewsFeatureEngine(BaseFeatureEngine):
    name = ENGINE_NAME
    category = CATEGORY

    def _definitions(self) -> list[FeatureDefinition]:
        return news_feature_definitions(
            self._settings.feature_normalization_window,
            calculation_frequency=f"{self._settings.feature_engine_interval}s",
        )

    def _compute(
        self, candles: Sequence[Candle], benchmark: Sequence[Candle] | None = None
    ) -> dict[str, Series]:
        return {}  # news features live on article time, not bars

    async def run(
        self,
        symbol: str = MARKET_SYMBOL,
        timeframe: str = "D",
        full: bool = False,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict:
        """News flow is market-wide: symbol/timeframe arguments are ignored
        (as are start/end -- accepted only for signature compatibility with
        the base class; article loading isn't date-ranged, data foundation
        audit 2026-07-17, historical regeneration item)."""
        articles = await self._load_articles()
        timestamps, series = compute_news_features(
            articles, self._settings.feature_normalization_window
        )
        if len(timestamps) < 2:
            return {
                "symbol": MARKET_SYMBOL,
                "timeframe": NEWS_TIMEFRAME,
                "stored": 0,
                "skipped": True,
            }
        return await self._process_series(
            MARKET_SYMBOL, NEWS_TIMEFRAME, timestamps, series, full=full
        )

    async def run_all(
        self, full: bool = False, start: datetime | None = None, end: datetime | None = None,
    ) -> list[dict]:
        try:
            return [await self.run()]
        except Exception as exc:
            logger.error("news feature run failed", extra={"error": str(exc)})
            return [{"symbol": MARKET_SYMBOL, "error": str(exc)}]

    async def _load_articles(self) -> list[Article]:
        if self._sessions is None:
            return []
        from app.database.tables import MarketEvent

        async with self._sessions() as session:
            result = await session.execute(
                select(MarketEvent.data)
                .where(MarketEvent.event_type == "news.observation")
                .order_by(desc(MarketEvent.id))
                .limit(self._settings.feature_news_lookback)
            )
            rows = result.scalars().all()
        articles: list[Article] = []
        for data in reversed(rows):
            if not data:
                continue
            meta = data.get("metadata") or {}
            ts_raw = meta.get("published_at") or data.get("timestamp")
            try:
                ts = datetime.fromisoformat(ts_raw)
            except (TypeError, ValueError):
                continue
            sentiment = meta.get("sentiment")
            novelty = meta.get("novelty")
            articles.append(
                Article(
                    ts=ts,
                    sentiment=float(sentiment) if sentiment is not None else None,
                    novelty=float(novelty) if novelty is not None else None,
                    urgency=meta.get("urgency"),
                    category=meta.get("category"),
                    title=meta.get("title") or "",
                    entities=tuple(meta.get("entities") or ()),
                )
            )
        articles.sort(key=lambda a: a.ts)
        return articles
