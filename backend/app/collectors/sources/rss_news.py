"""RSS news source (real feed for Prompt 2.10).

Aggregates articles from multiple trusted Indian financial news feeds via
plain RSS 2.0 — no API keys. The collector layer owns classification,
sentiment, entity matching, urgency, novelty, dedup, and impact scoring;
this source only delivers clean articles with full provenance
(title, body, source, published_at, url).
"""

import asyncio
import html
import re
import time
import xml.etree.ElementTree as ElementTree
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.collectors.base import CollectionError
from app.collectors.domains.news import NewsSource
from app.core.logging import get_logger

logger = get_logger(__name__)

FEEDS: dict[str, str] = {
    "economic_times": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "moneycontrol_markets": "https://www.moneycontrol.com/rss/marketreports.xml",
    "moneycontrol_economy": "https://www.moneycontrol.com/rss/economy.xml",
    "livemint_markets": "https://www.livemint.com/rss/markets",
}

HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "accept": "application/rss+xml, application/xml, text/xml, */*",
}

CACHE_TTL_SECONDS = 90
MAX_ARTICLES_PER_FEED = 50

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def clean_text(value: str | None) -> str:
    """Strip HTML tags/CDATA remnants and collapse whitespace."""
    if not value:
        return ""
    text = _TAG_RE.sub(" ", html.unescape(value))
    return _WHITESPACE_RE.sub(" ", text).strip()


def parse_pubdate(value: str | None) -> str | None:
    """RFC 822 pubDate -> ISO 8601 (publisher timestamp kept verbatim)."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value.strip()).isoformat()
    except (ValueError, TypeError):
        return None


def parse_rss(xml_text: str, source: str) -> list[dict[str, Any]]:
    """Parse one RSS 2.0 document into article dicts."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise CollectionError(f"{source}: invalid rss xml: {exc}") from exc
    articles: list[dict[str, Any]] = []
    for item in root.findall(".//item")[:MAX_ARTICLES_PER_FEED]:
        title = clean_text(item.findtext("title"))
        if not title:
            continue
        articles.append(
            {
                "title": title,
                "body": clean_text(item.findtext("description")),
                "source": source,
                "published_at": parse_pubdate(item.findtext("pubDate")),
                "url": (item.findtext("link") or "").strip(),
            }
        )
    return articles


class RssNewsSource(NewsSource):
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        feeds: dict[str, str] | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            headers=HEADERS, timeout=15.0, follow_redirects=True
        )
        self._feeds = feeds or FEEDS
        self._cache: tuple[float, list[dict[str, Any]]] | None = None

    async def fetch_articles(self) -> list[dict[str, Any]]:
        now = time.time()
        if self._cache is not None and now - self._cache[0] < CACHE_TTL_SECONDS:
            return self._cache[1]

        results = await asyncio.gather(
            *(self._fetch_feed(name, url) for name, url in self._feeds.items())
        )
        articles = [article for feed_articles in results for article in feed_articles]
        feeds_ok = sum(1 for feed_articles in results if feed_articles)
        if feeds_ok == 0:
            raise CollectionError("no news feeds available")
        self._cache = (now, articles)
        return articles

    async def _fetch_feed(self, name: str, url: str) -> list[dict[str, Any]]:
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            return parse_rss(response.text, name)
        except Exception as exc:
            logger.warning("news feed failed", extra={"feed": name, "error": str(exc)})
            return []

    async def close(self) -> None:
        await self._client.aclose()
