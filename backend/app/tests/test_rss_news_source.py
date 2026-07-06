"""Offline tests for the RSS news source (mocked transport)."""

import httpx
import pytest

from app.collectors.base import CollectionError
from app.collectors.sources.rss_news import RssNewsSource, parse_rss

RSS_DOC = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Test Feed</title>
  <item>
    <title>RBI holds &lt;b&gt;repo rate&lt;/b&gt; steady</title>
    <link>https://example.com/rbi</link>
    <pubDate>Mon, 06 Jul 2026 10:30:00 +0530</pubDate>
    <description>&lt;p&gt;The central   bank kept rates unchanged.&lt;/p&gt;</description>
  </item>
  <item>
    <title>Untitled skipped item placeholder</title>
    <link>https://example.com/second</link>
    <pubDate>not a date</pubDate>
    <description>Body two</description>
  </item>
  <item>
    <title></title>
    <link>https://example.com/empty</link>
  </item>
</channel></rss>"""


def test_parse_rss_cleans_html_and_dates() -> None:
    articles = parse_rss(RSS_DOC, "test_feed")
    assert len(articles) == 2  # empty-title item dropped
    first = articles[0]
    assert first["title"] == "RBI holds repo rate steady"
    assert first["body"] == "The central bank kept rates unchanged."
    assert first["source"] == "test_feed"
    assert first["published_at"] == "2026-07-06T10:30:00+05:30"
    assert first["url"] == "https://example.com/rbi"
    # Unparseable pubDate becomes None, article kept
    assert articles[1]["published_at"] is None


def test_parse_rss_invalid_xml_raises() -> None:
    with pytest.raises(CollectionError, match="invalid rss xml"):
        parse_rss("this is not xml <<<", "bad_feed")


def make_source(handler, feeds: dict[str, str]) -> RssNewsSource:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return RssNewsSource(client=client, feeds=feeds)


async def test_fetch_aggregates_multiple_feeds() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=RSS_DOC)

    source = make_source(
        handler, {"feed_a": "https://a.example/rss", "feed_b": "https://b.example/rss"}
    )
    articles = await source.fetch_articles()
    assert len(articles) == 4  # 2 per feed
    assert {a["source"] for a in articles} == {"feed_a", "feed_b"}


async def test_partial_feed_failure_tolerated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "bad" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, text=RSS_DOC)

    source = make_source(
        handler, {"good": "https://good.example/rss", "bad": "https://bad.example/rss"}
    )
    articles = await source.fetch_articles()
    assert len(articles) == 2
    assert all(a["source"] == "good" for a in articles)


async def test_all_feeds_down_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    source = make_source(handler, {"a": "https://a.example/rss"})
    with pytest.raises(CollectionError, match="no news feeds available"):
        await source.fetch_articles()


async def test_cache_prevents_refetch() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=RSS_DOC)

    source = make_source(handler, {"a": "https://a.example/rss"})
    await source.fetch_articles()
    await source.fetch_articles()
    assert calls["n"] == 1
