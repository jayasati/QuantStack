"""Telegram delivery sink for AlertService (Volume 1, Chapter 13).

Posts to the Telegram Bot API so ops alerts (e.g. a broker circuit breaker
opening) reach a human outside of logs, which nobody watches on an
unattended deployment. AlertService already fans out to every sink and
swallows any exception a sink raises, so this sink is free to raise on
delivery failure rather than fail silently.
"""

import httpx

from app.core.alerts import Alert, AlertSink

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramAlertSink(AlertSink):
    def __init__(
        self, token: str, chat_id: str, client: httpx.AsyncClient | None = None
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def send(self, alert: Alert) -> None:
        text = f"[{alert.severity.value.upper()}] {alert.source}: {alert.message}"
        response = await self._client.post(
            f"{TELEGRAM_API_BASE}/bot{self._token}/sendMessage",
            json={"chat_id": self._chat_id, "text": text},
        )
        response.raise_for_status()
