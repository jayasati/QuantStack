"""Global/geopolitical news source (Volume 2, Prompt 2.10 extension).

Feeds the GlobalShockCollector: RSS from international outlets plus targeted
Google News RSS queries for the countries and conflicts that most reliably
move Indian markets (US-Iran/Israel tension, Russia-Ukraine, US-China trade,
Fed policy, OPEC/crude). No API keys required. As with rss_news, this source
only delivers clean articles with provenance — classification, sentiment,
urgency, novelty, dedup, and impact scoring stay in the collector layer.
"""

import asyncio
import time
import xml.etree.ElementTree as ElementTree
from typing import Any
from urllib.parse import quote

import httpx

from app.collectors.base import CollectionError
from app.collectors.domains.news import NewsSource
from app.collectors.sources.rss_news import HEADERS, clean_text, parse_pubdate, parse_rss
from app.core.logging import get_logger

logger = get_logger(__name__)

FIXED_FEEDS: dict[str, str] = {
    "cnbc_world": "https://www.cnbc.com/id/100727362/device/rss/rss.html",
    "al_jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
}

# Topics picked for their track record of moving Nifty/Sensex, crude, and the
# rupee even when no Indian entity is named in the headline.
GOOGLE_NEWS_QUERIES: tuple[str, ...] = (
    "Iran Israel war",
    "US Iran conflict",
    "Russia Ukraine war",
    "US China trade war",
    "China Taiwan tension",
    "crude oil OPEC",
    "US Federal Reserve interest rate",
    "US sanctions",
)

GOOGLE_NEWS_URL = "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"

# Faster than rss_news's 90s: this feed's whole purpose is to shorten the
# time between a geopolitical headline and it reaching the pipeline.
CACHE_TTL_SECONDS = 45
MAX_ARTICLES_PER_QUERY = 15


def _google_news_url(query: str) -> str:
    return GOOGLE_NEWS_URL.format(query=quote(query))


def parse_google_news(xml_text: str, query: str) -> list[dict[str, Any]]:
    """Parse a Google News RSS search result.

    Same RSS 2.0 shape as plain publisher feeds, except the source name comes
    from a <source url="..."> tag rather than being fixed per-feed, and the
    link is a Google News redirect rather than the publisher's own URL.
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise CollectionError(f"google_news[{query}]: invalid rss xml: {exc}") from exc

    articles: list[dict[str, Any]] = []
    for item in root.findall(".//item")[:MAX_ARTICLES_PER_QUERY]:
        title = clean_text(item.findtext("title"))
        if not title:
            continue
        source_el = item.find("source")
        source = (source_el.text or "").strip() if source_el is not None else ""
        articles.append(
            {
                "title": title,
                "body": clean_text(item.findtext("description")),
                "source": source or "google_news",
                "published_at": parse_pubdate(item.findtext("pubDate")),
                "url": (item.findtext("link") or "").strip(),
            }
        )
    return articles


class GlobalNewsSource(NewsSource):
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        fixed_feeds: dict[str, str] | None = None,
        queries: tuple[str, ...] | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            headers=HEADERS, timeout=15.0, follow_redirects=True
        )
        self._fixed_feeds = fixed_feeds if fixed_feeds is not None else FIXED_FEEDS
        self._queries = queries if queries is not None else GOOGLE_NEWS_QUERIES
        self._cache: tuple[float, list[dict[str, Any]]] | None = None

    async def fetch_articles(self) -> list[dict[str, Any]]:
        now = time.time()
        if self._cache is not None and now - self._cache[0] < CACHE_TTL_SECONDS:
            return self._cache[1]

        tasks = [self._fetch_fixed(name, url) for name, url in self._fixed_feeds.items()]
        tasks += [self._fetch_query(query) for query in self._queries]
        results = await asyncio.gather(*tasks)
        articles = [article for feed_articles in results for article in feed_articles]
        feeds_ok = sum(1 for feed_articles in results if feed_articles)
        if feeds_ok == 0:
            raise CollectionError("no global news feeds available")
        self._cache = (now, articles)
        return articles

    async def _fetch_fixed(self, name: str, url: str) -> list[dict[str, Any]]:
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            return parse_rss(response.text, name)
        except Exception as exc:
            logger.warning("global news feed failed", extra={"feed": name, "error": str(exc)})
            return []

    async def _fetch_query(self, query: str) -> list[dict[str, Any]]:
        try:
            response = await self._client.get(_google_news_url(query))
            response.raise_for_status()
            return parse_google_news(response.text, query)
        except Exception as exc:
            logger.warning("google news query failed", extra={"query": query, "error": str(exc)})
            return []

    async def close(self) -> None:
        await self._client.aclose()
