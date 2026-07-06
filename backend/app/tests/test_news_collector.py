"""Tests for the News Intelligence Collector (Prompt 2.10)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.collectors.base import CollectionError
from app.collectors.domains.news import (
    LexiconSentimentProvider,
    NewsIntelligenceCollector,
    NewsSource,
)
from app.collectors.schema import Direction


def _iso(minutes_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


def _articles() -> list[dict[str, Any]]:
    return [
        {
            # Clearly positive stock article mentioning a watchlist symbol (NIFTY).
            "title": "NIFTY heavyweight beats estimates with record profit",
            "body": "Brokerages upgrade the stock after a surge in quarterly numbers.",
            "source": "reuters",
            "published_at": _iso(10),
            "url": "https://news.example/nifty-beats",
        },
        {
            # Clearly negative macro article.
            "title": "GDP misses forecasts as inflation climbs",
            "body": "Economists fear a rate hike and rising default risk across the economy.",
            "source": "bloomberg",
            "published_at": _iso(60),
            "url": "https://news.example/gdp-misses",
        },
        {
            # Near-duplicate pair: only this first one should survive dedup.
            "title": "PHARMA sector downgrade as regulator probe widens",
            "body": "Shares plunge after fraud allegations at a leading drugmaker.",
            "source": "moneycontrol",
            "published_at": _iso(200),
            "url": "https://news.example/pharma-probe-1",
        },
        {
            "title": "PHARMA sector downgrade as regulator probe widens further",
            "body": "Shares plunge after fraud allegations at a leading drugmaker firm.",
            "source": "unknown blog",
            "published_at": _iso(190),
            "url": "https://news.example/pharma-probe-2",
        },
    ]


class FakeNewsSource(NewsSource):
    def __init__(self, articles: list[dict[str, Any]]) -> None:
        self.articles = articles

    async def fetch_articles(self) -> list[dict[str, Any]]:
        return self.articles


async def test_collect_classifies_scores_and_dedups() -> None:
    collector = NewsIntelligenceCollector(news_source=FakeNewsSource(_articles()))
    records = await collector.collect()

    # 4 articles in, near-duplicate dropped: 3 unique records out.
    assert len(records) == 3

    stock, macro, sector = records

    # Positive stock article: entity matched from the watchlist, bullish sentiment.
    assert stock.instrument == "NIFTY"
    assert stock.metadata["category"] == "stock"
    assert stock.metadata["sentiment"] > 0
    assert stock.direction == Direction.BULLISH
    assert stock.metadata["urgency"] == "high"  # published < 30 minutes ago
    assert "NIFTY" in stock.metadata["entities"]

    # Negative macro article: no known entity, bearish sentiment.
    assert macro.instrument == "MARKET"
    assert macro.metadata["category"] == "macro"
    assert macro.metadata["sentiment"] < 0
    assert macro.direction == Direction.BEARISH

    # Surviving member of the near-duplicate pair is the first one seen.
    assert sector.metadata["category"] == "sector"
    assert sector.instrument == "PHARMA"
    assert sector.metadata["url"] == "https://news.example/pharma-probe-1"
    assert sector.direction == Direction.BEARISH

    # Provenance preserved on every record.
    for record in records:
        assert record.metadata["title"]
        assert record.metadata["source"]
        assert record.metadata["url"]
        assert record.metadata["published_at"]
        assert 0.0 <= record.metadata["novelty"] <= 1.0
        assert record.normalized_value is not None and record.normalized_value >= 0.0


async def test_cross_run_dedup_uses_bounded_seen_set() -> None:
    collector = NewsIntelligenceCollector(news_source=FakeNewsSource(_articles()))
    first = await collector.collect()
    second = await collector.collect()
    assert len(first) == 3
    assert second == []  # all articles already seen in the previous run


async def test_unconfigured_source_raises() -> None:
    from app.collectors.domains.news import UnconfiguredNewsSource

    collector = NewsIntelligenceCollector(news_source=UnconfiguredNewsSource())
    with pytest.raises(CollectionError, match="not configured"):
        await collector.collect()


def test_default_source_is_rss() -> None:
    from app.collectors.sources.rss_news import RssNewsSource

    assert isinstance(NewsIntelligenceCollector()._news_source, RssNewsSource)


def test_lexicon_sentiment_signs_and_bounds() -> None:
    provider = LexiconSentimentProvider()
    positive = provider.score("Company beats estimates, record profit and upgrade")
    negative = provider.score("Firm misses estimates, downgrade and fraud probe")
    assert 0 < positive <= 1
    assert -1 <= negative < 0
    assert provider.score("completely neutral text about weather") == 0.0
