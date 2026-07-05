"""News intelligence collector (Volume 2, Chapter 15, Prompt 2.10).

Aggregates articles from an injectable :class:`NewsSource`, classifies each one
(stock / sector / macro / global / policy / corporate), scores sentiment via a
pluggable :class:`SentimentProvider`, extracts known entities, grades urgency by
recency and novelty via semantic-ish dedup (normalized-token Jaccard), and emits
one :class:`CollectorOutput` per unique article with provenance preserved.

No network calls and no fabricated articles: the default source is
:class:`UnconfiguredNewsSource`, which always raises ``CollectionError``.
"""

import re
from abc import ABC, abstractmethod
from collections import deque
from datetime import UTC, datetime
from typing import Any

from app.collectors.base import BaseCollector, CollectionError
from app.collectors.schema import CollectorCategory, CollectorOutput, Direction
from app.core.config import get_settings

# --- lexicons and reference data (module-level, no ML dependencies) ------------

POSITIVE_TERMS: dict[str, float] = {
    "beats": 1.0,
    "beat estimates": 1.2,
    "upgrade": 1.0,
    "upgrades": 1.0,
    "surge": 1.0,
    "surges": 1.0,
    "rally": 1.0,
    "record profit": 1.5,
    "rate cut": 1.0,
    "outperform": 1.0,
    "bullish": 1.0,
    "gains": 0.8,
    "growth": 0.5,
    "buyback": 0.8,
    "dividend hike": 0.8,
}

NEGATIVE_TERMS: dict[str, float] = {
    "misses": 1.0,
    "missed estimates": 1.2,
    "downgrade": 1.0,
    "downgrades": 1.0,
    "plunge": 1.0,
    "plunges": 1.0,
    "selloff": 1.0,
    "default": 1.5,
    "fraud": 1.5,
    "rate hike": 1.0,
    "underperform": 1.0,
    "bearish": 1.0,
    "losses": 0.8,
    "slump": 1.0,
    "probe": 0.8,
    "recession": 1.2,
}

# Source trust map used by the impact score; unknown sources get DEFAULT_SOURCE_TRUST.
SOURCE_TRUST: dict[str, float] = {
    "reuters": 0.95,
    "bloomberg": 0.95,
    "pti": 0.85,
    "moneycontrol": 0.85,
    "economic times": 0.85,
    "livemint": 0.80,
    "business standard": 0.80,
    "cnbc": 0.75,
}
DEFAULT_SOURCE_TRUST: float = 0.6

KNOWN_SECTOR_NAMES: tuple[str, ...] = (
    "BANKING",
    "PHARMA",
    "AUTO",
    "ENERGY",
    "FMCG",
    "METALS",
    "REALTY",
    "INFRA",
    "TELECOM",
)

_WATCHLIST_SYMBOLS: tuple[str, ...] = tuple(get_settings().watchlist)

# Known entities for extraction: watchlist symbols first (preferred as instrument),
# then sector names.
KNOWN_ENTITIES: list[str] = [*_WATCHLIST_SYMBOLS, *KNOWN_SECTOR_NAMES]

_POLICY_KEYWORDS: tuple[str, ...] = (
    "rbi",
    "sebi",
    "policy",
    "regulation",
    "regulator",
    "budget",
    "tariff",
    "ministry",
    "government",
)
_MACRO_KEYWORDS: tuple[str, ...] = (
    "gdp",
    "inflation",
    "cpi",
    "unemployment",
    "fiscal deficit",
    "macro",
    "economy",
    "recession",
    "rate hike",
    "rate cut",
)
_GLOBAL_KEYWORDS: tuple[str, ...] = (
    "fed",
    "federal reserve",
    "china",
    "europe",
    "ecb",
    "global",
    "geopolitical",
    "crude",
    "treasury",
    "us markets",
)
_CORPORATE_KEYWORDS: tuple[str, ...] = (
    "merger",
    "acquisition",
    "earnings",
    "dividend",
    "buyback",
    "ipo",
    "results",
    "ceo",
    "board",
    "stake",
)

_STOPWORDS: frozenset[str] = frozenset(
    "a an the and or of in on at as to for with after over from by is are was were".split()
)

DUPLICATE_SIMILARITY: float = 0.7
SEEN_SET_MAX: int = 512

_TOKEN_RE = re.compile(r"[a-z0-9]+")


# --- text helpers ---------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _padded(text: str) -> str:
    """Normalized token stream padded with spaces for whole-phrase matching."""
    return f" {' '.join(_tokenize(text))} "


def _content_tokens(text: str) -> frozenset[str]:
    """Normalized token set (stopwords removed) used for Jaccard dedup."""
    return frozenset(token for token in _tokenize(text) if token not in _STOPWORDS)


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _urgency(published: datetime | None, now: datetime) -> tuple[str, float]:
    """Recency-based urgency: published under 30 minutes ago is high."""
    if published is None:
        return "low", 0.3
    age_minutes = (now - published).total_seconds() / 60
    if age_minutes < 30:
        return "high", 1.0
    if age_minutes < 120:
        return "medium", 0.6
    return "low", 0.3


# --- pluggable interfaces -------------------------------------------------------


class NewsSource(ABC):
    """Injectable article source. Implementations own transport and auth."""

    @abstractmethod
    async def fetch_articles(self) -> list[dict[str, Any]]:
        """Return articles: {title, body, source, published_at (ISO), url}."""


class UnconfiguredNewsSource(NewsSource):
    """Default source: always fails. Articles are NEVER fabricated."""

    async def fetch_articles(self) -> list[dict[str, Any]]:
        raise CollectionError("news source not configured")


class SentimentProvider(ABC):
    """Pluggable sentiment scorer returning a value in [-1, 1]."""

    @abstractmethod
    def score(self, text: str) -> float:
        """Score ``text`` in [-1, 1]; positive means bullish tone."""


class LexiconSentimentProvider(SentimentProvider):
    """Finance-lexicon sentiment via simple token/phrase matching.

    Intentionally dependency-free. A finance-specific ML model (e.g. FinBERT)
    can be plugged in later through the :class:`SentimentProvider` interface
    without touching the collector — do not add ML dependencies here.
    """

    def score(self, text: str) -> float:
        padded = _padded(text)
        positive = sum(w * padded.count(f" {term} ") for term, w in POSITIVE_TERMS.items())
        negative = sum(w * padded.count(f" {term} ") for term, w in NEGATIVE_TERMS.items())
        total = positive + negative
        if total == 0:
            return 0.0
        return max(-1.0, min(1.0, (positive - negative) / total))


# --- classification and entity extraction ---------------------------------------


def extract_entities(text: str) -> list[str]:
    """Match known watchlist symbols and sector names in ``text``."""
    padded = _padded(text)
    return [entity for entity in KNOWN_ENTITIES if f" {entity.lower()} " in padded]


def classify(article: dict[str, Any]) -> str:
    """Rule-based classification into stock/sector/macro/global/policy/corporate."""
    text = f"{article.get('title', '')} {article.get('body', '')}"
    padded = _padded(text)
    entities = extract_entities(text)
    if any(entity in _WATCHLIST_SYMBOLS for entity in entities):
        return "stock"
    if any(entity in KNOWN_SECTOR_NAMES for entity in entities):
        return "sector"
    for label, keywords in (
        ("policy", _POLICY_KEYWORDS),
        ("macro", _MACRO_KEYWORDS),
        ("global", _GLOBAL_KEYWORDS),
        ("corporate", _CORPORATE_KEYWORDS),
    ):
        if any(f" {keyword} " in padded for keyword in keywords):
            return label
    return "macro"


# --- collector -------------------------------------------------------------------


class NewsIntelligenceCollector(BaseCollector):
    """Aggregate, classify, score, and dedup financial news articles."""

    name = "news_intelligence"
    category = CollectorCategory.NEWS
    source = "news_feed"
    interval_seconds = 120
    priority = 25

    def __init__(
        self,
        news_source: NewsSource | None = None,
        sentiment_provider: SentimentProvider | None = None,
    ) -> None:
        super().__init__()
        self._news_source: NewsSource = news_source or UnconfiguredNewsSource()
        self._sentiment: SentimentProvider = sentiment_provider or LexiconSentimentProvider()
        # Bounded memory of normalized token sets across runs (cross-run dedup).
        self._seen_tokens: deque[frozenset[str]] = deque(maxlen=SEEN_SET_MAX)

    async def collect(self) -> list[CollectorOutput]:
        articles = await self._news_source.fetch_articles()
        now = datetime.now(UTC)
        records: list[CollectorOutput] = []
        run_seen: list[frozenset[str]] = []

        for article in articles:
            title = str(article.get("title") or "")
            body = str(article.get("body") or "")
            text = f"{title} {body}".strip()
            tokens = _content_tokens(text)
            if not tokens:
                continue

            history = [*run_seen, *self._seen_tokens]
            max_similarity = max((_jaccard(tokens, seen) for seen in history), default=0.0)
            if max_similarity > DUPLICATE_SIMILARITY:
                continue  # semantically similar to an already-seen article
            run_seen.append(tokens)
            novelty = round(1.0 - max_similarity, 4)

            sentiment = max(-1.0, min(1.0, self._sentiment.score(text)))
            entities = extract_entities(text)
            published = _parse_timestamp(article.get("published_at"))
            urgency_label, urgency_weight = _urgency(published, now)
            source_name = str(article.get("source") or "unknown")
            trust = SOURCE_TRUST.get(source_name.lower(), DEFAULT_SOURCE_TRUST)
            impact = abs(sentiment) * urgency_weight * trust

            if sentiment > 0:
                direction = Direction.BULLISH
            elif sentiment < 0:
                direction = Direction.BEARISH
            else:
                direction = Direction.NEUTRAL

            records.append(
                CollectorOutput(
                    collector_name=self.name,
                    collector_category=self.category,
                    source=self.source,
                    instrument=entities[0] if entities else "MARKET",
                    raw_value=title,
                    normalized_value=impact,
                    direction=direction,
                    confidence=trust,
                    freshness_seconds=(
                        (now - published).total_seconds() if published else None
                    ),
                    metadata={
                        "title": title,
                        "source": source_name,
                        "url": article.get("url"),
                        "published_at": article.get("published_at"),
                        "category": classify(article),
                        "sentiment": sentiment,
                        "urgency": urgency_label,
                        "novelty": novelty,
                        "entities": entities,
                    },
                )
            )

        self._seen_tokens.extend(run_seen)
        return records
