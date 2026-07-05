"""Scheduled jobs. Volume 1 ships a single sample job proving the scheduler works."""

from app.core.logging import get_logger

logger = get_logger(__name__)


async def heartbeat() -> None:
    logger.info("heartbeat", extra={"job": "system.heartbeat"})
