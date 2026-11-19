
from __future__ import annotations

import asyncio
import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from marketing_dashboard import whiteboard as wb

log = logging.getLogger("marketing.whiteboard.scheduler")

ENABLED = os.getenv("WB_SCHEDULER_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
TIMEZONE = os.getenv("WB_SCHEDULER_TZ", "America/Denver")
SYNC_HOUR = int(os.getenv("WB_SYNC_HOUR", "5"))
REMINDER_HOUR = int(os.getenv("WB_PROVIDER_REMINDER_HOUR", "9"))

_scheduler: AsyncIOScheduler | None = None


async def sync_job() -> None:
    sync_id = await asyncio.to_thread(wb._start_log, "scheduler")
    if not sync_id:
        log.info("Whiteboard scheduled sync skipped: another sync is running.")
        return
    await wb.run_sync(sync_id)
    log.info("Whiteboard scheduled sync finished (sync_id=%s).", sync_id)


async def reminder_job() -> None:
    result = await asyncio.to_thread(wb.send_provider_reminder)
    log.info("Whiteboard provider reminder: %s", result)


def start() -> None:
    """Idempotent: nested router startup hooks can fire more than once."""
    global _scheduler
    if _scheduler is not None or not ENABLED:
        return
    sched = AsyncIOScheduler(timezone=TIMEZONE)
    common = dict(replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600)
    # jitter spreads the start across workers so they do not race for the same run
    sched.add_job(sync_job, CronTrigger(hour=SYNC_HOUR, minute=0, jitter=120, timezone=TIMEZONE),
                  id="wb_neos_sync", **common)
    sched.add_job(reminder_job, CronTrigger(hour=REMINDER_HOUR, minute=0, jitter=120, timezone=TIMEZONE),
                  id="wb_provider_reminder", **common)
    sched.start()
    _scheduler = sched
    log.info("Whiteboard scheduler started (sync %02d:00, reminder check %02d:00, %s).",
             SYNC_HOUR, REMINDER_HOUR, TIMEZONE)


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def status() -> dict:
    if _scheduler is None:
        return {"running": False, "enabled": ENABLED, "timezone": TIMEZONE}
    return {
        "running": True, "enabled": ENABLED, "timezone": TIMEZONE,
        "jobs": [{"id": j.id, "next_run": j.next_run_time.isoformat() if j.next_run_time else None}
                 for j in _scheduler.get_jobs()],
    }
