"""Shared NSE web-API session: browser-like cookie handshake + JSON fetch."""

import asyncio
from typing import Any

import httpx

from app.collectors.base import CollectionError
from app.core.logging import get_logger

logger = get_logger(__name__)

BASE = "https://www.nseindia.com"

HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "referer": f"{BASE}/",
}


class NseSession:
    """Cookie-warmed NSE API client with automatic re-warm on rejection."""

    def __init__(
        self, client: httpx.AsyncClient | None = None, warmup_path: str = "/"
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=BASE, headers=HEADERS, timeout=20.0, follow_redirects=True
        )
        self._warmup_path = warmup_path
        self._warmed = False

    async def _warm_up(self) -> None:
        response = await self._client.get(self._warmup_path)
        response.raise_for_status()
        self._warmed = True

    async def get_json(self, path: str) -> dict[str, Any]:
        if not self._warmed:
            await self._warm_up()
        for attempt in (1, 2):
            response = await self._client.get(path)
            if response.status_code in (401, 403) and attempt == 1:
                logger.info("nse session rejected; re-warming cookies")
                await self._warm_up()
                await asyncio.sleep(0.5)
                continue
            response.raise_for_status()
            try:
                return response.json()
            except ValueError as exc:  # HTML error page
                if attempt == 1:
                    self._warmed = False
                    continue
                raise CollectionError(f"nse returned non-json response: {exc}") from exc
        raise CollectionError("nse request failed after retry")

    async def close(self) -> None:
        await self._client.aclose()
