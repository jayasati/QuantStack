"""Tests for app/scheduler (IRR-2026-07-11 finding #10: no dedicated test
file existed for the scheduler service before this)."""

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.scheduler.jobs import heartbeat
from app.scheduler.service import create_scheduler, start_scheduler


def test_create_scheduler_registers_the_heartbeat_job_but_does_not_start() -> None:
    scheduler = create_scheduler()
    assert isinstance(scheduler, AsyncIOScheduler)
    assert scheduler.running is False
    job = scheduler.get_job("system.heartbeat")
    assert job is not None
    assert job.trigger.interval.total_seconds() == 60
    # Never started -- nothing to shut down.


async def test_start_scheduler_actually_starts_it() -> None:
    # AsyncIOScheduler.start() binds to the currently running event loop,
    # so this must run inside an async test.
    scheduler = start_scheduler()
    try:
        assert scheduler.running is True
        assert scheduler.get_job("system.heartbeat") is not None
    finally:
        scheduler.shutdown(wait=False)


async def test_heartbeat_job_runs_without_raising() -> None:
    await heartbeat()  # the job body itself -- just logs, must never raise
