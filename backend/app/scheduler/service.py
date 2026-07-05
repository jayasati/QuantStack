"""APScheduler service.

All recurring tasks run through APScheduler — never OS cron jobs.
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.logging import get_logger
from app.scheduler.jobs import heartbeat

logger = get_logger(__name__)


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(
        heartbeat,
        trigger="interval",
        seconds=60,
        id="system.heartbeat",
        replace_existing=True,
    )
    return scheduler


def start_scheduler() -> AsyncIOScheduler:
    scheduler = create_scheduler()
    scheduler.start()
    logger.info("scheduler started", extra={"jobs": [j.id for j in scheduler.get_jobs()]})
    return scheduler
