"""Tests for the geopolitical/global news fast-path (Prompt 2.10 extension)."""

from datetime import UTC, datetime, timedelta
from typing import Any

from app.collectors.domains.news import (
    GlobalShockCollector,
    LexiconSentimentProvider,
    NewsSource,
    classify,
)
from app.collectors.schema import CollectorCategory, Direction
from app.collectors.sources.global_news import GlobalNewsSource, parse_google_news


def _iso(minutes_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


class FakeNewsSource(NewsSource):
    def __init__(self, articles: list[dict[str, Any]]) -> None:
        self.articles = articles

    async def fetch_articles(self) -> list[dict[str, Any]]:
        return self.articles


def test_war_headline_scores_bearish_without_finance_words() -> None:
    provider = LexiconSentimentProvider()
    score = provider.score("Iran launches missile attack on US base, war fears escalate")
    assert score < 0


def test_ceasefire_headline_scores_bullish() -> None:
    provider = LexiconSentimentProvider()
    score = provider.score("Iran and Israel agree ceasefire deal after truce talks")
    assert score > 0


def test_country_entity_classifies_as_global_even_without_finance_keywords() -> None:
    article = {
        "title": "Iran launches strikes on US forces amid deepening conflict",
        "body": "Tensions escalate across the Middle East.",
    }
    assert classify(article) == "global"


def test_bare_us_pronoun_does_not_false_positive_as_country() -> None:
    # "US" is deliberately excluded from KNOWN_COUNTRY_NAMES because it
    # collides with the pronoun "us" — this article should NOT classify as
    # global purely because it contains "us" in ordinary English.
    article = {"title": "Company gives us record profit update", "body": "Join us for the call."}
    assert classify(article) != "global"


async def test_global_shock_collector_defaults_to_global_source() -> None:
    from app.collectors.sources.global_news import GlobalNewsSource as RealSource

    collector = GlobalShockCollector()
    assert isinstance(collector._news_source, RealSource)
    assert collector.category == CollectorCategory.GLOBAL_MARKETS
    assert collector.interval_seconds < 60  # faster than the domestic feed


async def test_global_shock_collector_classifies_and_scores_conflict_article() -> None:
    articles = [
        {
            "title": "Iran attacks US base as ceasefire collapses",
            "body": "Missile strikes reported near the Gulf as tensions escalate sharply.",
            "source": "reuters",
            "published_at": _iso(2),
            "url": "https://news.example/iran-us",
        }
    ]
    collector = GlobalShockCollector(news_source=FakeNewsSource(articles))
    records = await collector.collect()

    assert len(records) == 1
    record = records[0]
    assert record.metadata["category"] == "global"
    assert record.metadata["sentiment"] < 0
    assert record.direction == Direction.BEARISH
    assert record.metadata["urgency"] == "high"
    assert "IRAN" in record.metadata["entities"]
    assert record.collector_category == CollectorCategory.GLOBAL_MARKETS


def test_parse_google_news_extracts_source_tag() -> None:
    xml_text = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item>
        <title>Why have US-Iran strikes resumed? - Al Jazeera</title>
        <link>https://news.google.com/rss/articles/abc?oc=5</link>
        <pubDate>Wed, 08 Jul 2026 09:46:32 GMT</pubDate>
        <description>Why have US-Iran strikes resumed?</description>
        <source url="https://www.aljazeera.com">Al Jazeera</source>
      </item>
    </channel></rss>
    """
    articles = parse_google_news(xml_text, query="Iran Israel war")
    assert len(articles) == 1
    assert articles[0]["source"] == "Al Jazeera"
    assert articles[0]["title"] == "Why have US-Iran strikes resumed? - Al Jazeera"
    assert articles[0]["published_at"] is not None


async def test_global_news_source_isolates_per_feed_failures() -> None:
    import httpx

    class FailingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if "news.google.com" in str(request.url):
                good_xml = (
                    '<?xml version="1.0"?><rss version="2.0"><channel>'
                    "<item><title>Russia Ukraine war escalates</title>"
                    "<link>https://news.example/ru</link>"
                    "<pubDate>Wed, 08 Jul 2026 09:00:00 GMT</pubDate>"
                    "<description>Conflict deepens.</description>"
                    '<source url="https://reuters.com">Reuters</source>'
                    "</item></channel></rss>"
                )
                return httpx.Response(200, text=good_xml)
            return httpx.Response(500, text="server error")

    client = httpx.AsyncClient(transport=FailingTransport())
    source = GlobalNewsSource(
        client=client,
        fixed_feeds={"broken_feed": "https://example.invalid/rss"},
        queries=("Russia Ukraine war",),
    )
    articles = await source.fetch_articles()
    await source.close()

    assert len(articles) == 1
    assert articles[0]["source"] == "Reuters"
