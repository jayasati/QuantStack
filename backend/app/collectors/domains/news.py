"""News intelligence collector (Volume 2, Chapter 15, Prompt 2.10).

Aggregates articles from an injectable :class:`NewsSource`, classifies each one
(stock / sector / macro / global / policy / corporate), scores sentiment via a
pluggable :class:`SentimentProvider`, extracts known entities, grades urgency by
recency and novelty via semantic-ish dedup (normalized-token Jaccard), and emits
one :class:`CollectorOutput` per unique article with provenance preserved.

No network calls and no fabricated articles: the default source is
:class:`UnconfiguredNewsSource`, which always raises ``CollectionError``.
"""

import asyncio
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
    # Geopolitical de-escalation: markets read these as risk-off unwinding.
    # Bare "ceasefire"/"truce" are deliberately excluded — the word alone is
    # equally common in headlines reporting one BREAKING, so it must be
    # scored from the specific phrase, not the word (see ceasefire-collapse
    # entries in NEGATIVE_TERMS below).
    "ceasefire deal": 1.2,
    "ceasefire holds": 1.1,
    "ceasefire agreed": 1.2,
    "peace deal": 1.3,
    "de-escalation": 1.0,
    "peace talks": 0.8,
    "troop withdrawal": 0.8,
    "trade deal": 1.0,
    "tariff cut": 1.0,
    "tariffs lifted": 1.1,
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
    # Geopolitical shock vocabulary: war/conflict language moves index and crude
    # prices even with zero finance-specific terms in the headline.
    "war": 1.3,
    "attack": 1.1,
    "attacks": 1.1,
    "strike": 0.9,
    "strikes": 0.9,
    "airstrike": 1.2,
    "airstrikes": 1.2,
    "missile": 1.1,
    "missiles": 1.1,
    "invasion": 1.3,
    "invades": 1.3,
    "escalation": 0.9,
    "escalates": 0.9,
    "sanctions": 0.9,
    "conflict": 0.7,
    "nuclear threat": 1.2,
    "troops": 0.5,
    "retaliation": 1.0,
    "retaliatory": 1.0,
    "ceasefire collapse": 1.4,
    "ceasefire collapses": 1.4,
    "ceasefire collapsed": 1.4,
    "ceasefire over": 1.4,
    "ceasefire broken": 1.4,
    "ceasefire violated": 1.4,
    "truce collapses": 1.4,
    "truce broken": 1.4,
    "martial law": 1.2,
    # Trade-policy vocabulary: tariff/trade-war language moves export-heavy
    # sectors and the rupee even without a war/conflict word in the headline.
    "tariff war": 1.1,
    "trade war": 1.1,
    "new tariffs": 1.0,
    "tariff hike": 1.0,
    "tariffs imposed": 1.0,
    "retaliatory tariffs": 1.1,
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
    "al jazeera": 0.80,
    "associated press": 0.90,
    "ap": 0.90,
    "afp": 0.85,
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

# Countries/regions whose news routinely moves Indian markets (war, sanctions,
# trade policy, central bank action) even when no Indian entity is named.
KNOWN_COUNTRY_NAMES: tuple[str, ...] = (
    # "US" is deliberately excluded: as a bare token it collides with the
    # common pronoun ("join us", "with us") and would false-positive on almost
    # any article. "USA" and "america(n)" below are unambiguous instead.
    "USA",
    "AMERICA",
    "CHINA",
    "RUSSIA",
    "IRAN",
    "ISRAEL",
    "HONG KONG",
    "TAIWAN",
    "UKRAINE",
    "SAUDI ARABIA",
    "PAKISTAN",
)

# Heads of state / geopolitical figures whose statements move crude, the
# rupee, or global risk sentiment even when framed as "just talk" — a war,
# tariff, or sanctions headline attached to one of these names is the
# highest-conviction shock signal available before price actually moves.
# Full names/surnames chosen to avoid common-word collisions (see the "US"
# note above); "XI JINPING" is spelled out in full for the same reason bare
# "XI" would be ("Article XI", chapter numbering, etc.).
KNOWN_GLOBAL_LEADER_NAMES: tuple[str, ...] = (
    "TRUMP",
    "PUTIN",
    "XI JINPING",
    "ZELENSKYY",
    "ZELENSKY",
    "NETANYAHU",
    "KHAMENEI",
)

# Central bankers: statements move US10Y/DXY/EUR or India's own repo path
# directly — the same channel MacroIntelligenceCollector's factors capture,
# just attributable to a named speaker instead of a price move after the fact.
KNOWN_CENTRAL_BANKER_NAMES: tuple[str, ...] = (
    "POWELL",
    "WARSH",
    "LAGARDE",
)

# India's own policymakers: routed to "policy" (domestic), not "global".
# RBI Governor's full name reduces collision risk vs. a bare surname.
KNOWN_INDIA_POLICYMAKER_NAMES: tuple[str, ...] = (
    "MODI",
    "SITHARAMAN",
    "SANJAY MALHOTRA",
)

# Institutions whose decisions/statements are themselves the market event —
# OPEC production quotas, a sovereign rating action, a Fed/ECB statement.
KNOWN_INSTITUTION_NAMES: tuple[str, ...] = (
    "OPEC",
    "MOODY",  # matches "Moody's" — tokenizer splits the trailing 's separately
    "FITCH",
    "S&P",
    "IMF",
    "WORLD BANK",
    "NATO",
    "FEDERAL RESERVE",
    "ECB",
)

# Promoters whose own name can move their flagship stock even when the
# ticker/symbol itself isn't named in the headline (index-weight concentration:
# Reliance alone is a large share of Nifty). Resolved to the tradable symbol
# during entity extraction so stock classification and instrument tagging
# work exactly like a direct symbol mention.
PROMOTER_ALIASES: dict[str, str] = {
    "AMBANI": "RELIANCE",
    "ADANI": "ADANIENT",
}
KNOWN_PROMOTER_NAMES: tuple[str, ...] = tuple(PROMOTER_ALIASES)

_WATCHLIST_SYMBOLS: tuple[str, ...] = tuple(get_settings().watchlist)

# Known entities for extraction, in instrument-preference order: watchlist
# symbols and promoter aliases first, then sectors, then countries, then
# named leaders/central bankers/policymakers/institutions.
KNOWN_ENTITIES: list[str] = [
    *_WATCHLIST_SYMBOLS,
    *KNOWN_PROMOTER_NAMES,
    *KNOWN_SECTOR_NAMES,
    *KNOWN_COUNTRY_NAMES,
    *KNOWN_GLOBAL_LEADER_NAMES,
    *KNOWN_CENTRAL_BANKER_NAMES,
    *KNOWN_INDIA_POLICYMAKER_NAMES,
    *KNOWN_INSTITUTION_NAMES,
]

# Leaders/institutions whose mentions classify as "global" (foreign) rather
# than "policy" (domestic) — mirrors the KNOWN_COUNTRY_NAMES routing.
_GLOBAL_ENTITY_NAMES: frozenset[str] = frozenset(
    (*KNOWN_GLOBAL_LEADER_NAMES, *KNOWN_CENTRAL_BANKER_NAMES, *KNOWN_INSTITUTION_NAMES)
)

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
    "middle east",
    "opec",
    "war",
    "invasion",
    "sanctions",
    "conflict",
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

    Intentionally dependency-free — no ML dependencies here. The production
    default is :class:`~app.collectors.sources.finbert_sentiment.FinBertSentimentProvider`
    (see ``Settings.news_sentiment_provider``); this remains available as the
    offline/CI fallback and for tests that need fast, deterministic scores.
    """

    def score(self, text: str) -> float:
        padded = _padded(text)
        positive = sum(w * padded.count(f" {term} ") for term, w in POSITIVE_TERMS.items())
        negative = sum(w * padded.count(f" {term} ") for term, w in NEGATIVE_TERMS.items())
        total = positive + negative
        if total == 0:
            return 0.0
        return max(-1.0, min(1.0, (positive - negative) / total))


def _default_sentiment_provider() -> SentimentProvider:
    """Chosen by ``Settings.news_sentiment_provider`` so environments without
    the FinBERT model cached (or without transformers/torch installed) can
    opt back into the dependency-free lexicon scorer."""
    if get_settings().news_sentiment_provider == "finbert":
        from app.collectors.sources.finbert_sentiment import FinBertSentimentProvider

        return FinBertSentimentProvider()
    return LexiconSentimentProvider()


# --- classification and entity extraction ---------------------------------------


def extract_entities(text: str) -> list[str]:
    """Match known watchlist symbols, sectors, countries, and named
    leaders/institutions in ``text``; promoter names resolve to their
    tradable symbol (e.g. "Ambani" -> "RELIANCE").

    Entities are tokenized the same way as ``text`` before matching (not
    just lowered), so punctuation in an entity name — "Moody's", "S&P" —
    still matches correctly against the alnum-only token stream.
    """
    padded = _padded(text)
    matches = []
    for entity in KNOWN_ENTITIES:
        entity_padded = f" {' '.join(_tokenize(entity))} "
        if entity_padded in padded:
            matches.append(PROMOTER_ALIASES.get(entity, entity))
    return matches


def classify(article: dict[str, Any]) -> str:
    """Rule-based classification into stock/sector/macro/global/policy/corporate."""
    text = f"{article.get('title', '')} {article.get('body', '')}"
    padded = _padded(text)
    entities = extract_entities(text)
    if any(entity in _WATCHLIST_SYMBOLS for entity in entities):
        return "stock"
    if any(entity in KNOWN_SECTOR_NAMES for entity in entities):
        return "sector"
    if any(entity in KNOWN_COUNTRY_NAMES for entity in entities):
        return "global"
    if any(entity in _GLOBAL_ENTITY_NAMES for entity in entities):
        return "global"
    if any(entity in KNOWN_INDIA_POLICYMAKER_NAMES for entity in entities):
        return "policy"
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


# NewsIntelligenceCollector (120s) and GlobalShockCollector (30s, subclass
# below) run on independent APScheduler schedules but both funnel through
# FinBERT sentiment scoring in the same process/vCPUs -- without this,
# their schedules can overlap and briefly peg multiple cores at once,
# competing with the request-serving event loop (perf-audit-2026-07-14
# finding 14: py-spy caught active BERT forward threads during slow
# requests). Module-level (not per-instance) so it's shared across both
# collector classes/instances. Only serializes the CPU-heavy scoring step
# against itself -- each collector's own fetch/dedup/classify work, and any
# other collector entirely, is unaffected. A full fix (a separate process
# so this stops sharing the GIL/cores with request handling at all) is a
# bigger architectural change than this conservative mitigation.
_finbert_scoring_lock = asyncio.Lock()


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
        if news_source is None:
            from app.collectors.sources.rss_news import RssNewsSource

            news_source = RssNewsSource()
        self._news_source: NewsSource = news_source
        self._sentiment: SentimentProvider = sentiment_provider or _default_sentiment_provider()
        # Bounded memory of normalized token sets across runs (cross-run dedup).
        self._seen_tokens: deque[frozenset[str]] = deque(maxlen=SEEN_SET_MAX)

    async def cleanup(self) -> None:
        closer = getattr(self._news_source, "close", None)
        if closer is not None:
            await closer()

    async def _score_all(self, texts: list[str]) -> list[float]:
        """One thread-pool hop per collect() call — never blocks the event
        loop, and uses score_batch (a single forward pass) when the provider
        offers it, e.g. FinBertSentimentProvider. Serialized against every
        other FinBERT-scoring collector via _finbert_scoring_lock (see its
        own docstring) so two independent schedules can't both peg CPU at
        once."""
        async with _finbert_scoring_lock:
            batch = getattr(self._sentiment, "score_batch", None)
            if batch is not None:
                return await asyncio.to_thread(batch, texts)
            return await asyncio.to_thread(lambda: [self._sentiment.score(t) for t in texts])

    async def collect(self) -> list[CollectorOutput]:
        articles = await self._news_source.fetch_articles()
        now = datetime.now(UTC)
        texts = [
            f"{a.get('title') or ''} {a.get('body') or ''}".strip() for a in articles
        ]
        # Scored up front for every article (dedup below doesn't affect
        # which texts need scoring, so this is the only batching opportunity).
        raw_sentiments = await self._score_all(texts)
        records: list[CollectorOutput] = []
        run_seen: list[frozenset[str]] = []

        for article, text, raw_sentiment in zip(articles, texts, raw_sentiments, strict=True):
            title = str(article.get("title") or "")
            tokens = _content_tokens(text)
            if not tokens:
                continue

            history = [*run_seen, *self._seen_tokens]
            max_similarity = max((_jaccard(tokens, seen) for seen in history), default=0.0)
            if max_similarity > DUPLICATE_SIMILARITY:
                continue  # semantically similar to an already-seen article
            run_seen.append(tokens)
            novelty = round(1.0 - max_similarity, 4)

            sentiment = max(-1.0, min(1.0, raw_sentiment))
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


class GlobalShockCollector(NewsIntelligenceCollector):
    """International/geopolitical fast-path (Volume 2, Prompt 2.10 extension).

    Runs the exact same aggregate/classify/score/dedup pipeline as
    NewsIntelligenceCollector, pointed at :class:`GlobalNewsSource` (CNBC
    World, Al Jazeera, and Google News queries for US-Iran/Israel,
    Russia-Ukraine, US-China, Fed policy, OPEC) on a much shorter cadence.

    Kept as a separate collector rather than a shorter interval on the
    domestic feed so a slow India-portal poll never delays a geopolitical
    headline, and vice versa.
    """

    name = "global_shock_news"
    category = CollectorCategory.GLOBAL_MARKETS
    source = "global_news_feed"
    interval_seconds = 30
    priority = 5

    def __init__(
        self,
        news_source: NewsSource | None = None,
        sentiment_provider: SentimentProvider | None = None,
    ) -> None:
        if news_source is None:
            from app.collectors.sources.global_news import GlobalNewsSource

            news_source = GlobalNewsSource()
        super().__init__(news_source=news_source, sentiment_provider=sentiment_provider)
