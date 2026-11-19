from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from PeakTrac.neos_expenses import sync_neos_expenses
from PeakTrac.neos_session import get_neos_client
from PeakTrac.neos_watcher import run_watcher
from PeakTrac.neos_categories import refresh_codes
from PeakTrac.neos_projections import compute_inputs
from db import NeosCode, ProjectionInput

log = logging.getLogger("peaktrac.neos.jobs")
MOUNTAIN = ZoneInfo("America/Denver")
RUN_HOUR = 2


def seconds_until_next_run(now: datetime | None = None) -> float:
    now = (now or datetime.now(MOUNTAIN)).astimezone(MOUNTAIN)
    target = now.replace(hour=RUN_HOUR, minute=0, second=0, microsecond=0)
    if target <= now:
        target = (target + timedelta(days=1)).replace(hour=RUN_HOUR)   # re-anchor across DST
    utc = ZoneInfo("UTC")
    return (target.astimezone(utc) - now.astimezone(utc)).total_seconds()


async def run_nightly_once(session_factory) -> dict:
    result: dict = {}
    client = get_neos_client()
    for name, job in (("expenses", sync_neos_expenses), ("watcher", run_watcher)):
        db = session_factory()
        try:
            result[name] = await job(db, client)
        except Exception as exc:           # one job failing must not skip the other
            db.rollback()
            log.exception("Nightly %s job failed", name)
            result[name] = {"error": type(exc).__name__}
        finally:
            db.close()
    for name, model, timestamp, age, job in (
        ("codes", NeosCode, "refreshed_at", 7, refresh_codes),
        ("projections", ProjectionInput, "computed_at", 28, compute_inputs),
    ):
        db = session_factory()
        try:
            last = db.query(model).order_by(getattr(model, timestamp).desc()).first()
            if (last is None or
                (name == 'projections' and getattr(last, timestamp).strftime('%Y-%m') != datetime.utcnow().strftime('%Y-%m')) or
                (name == 'codes' and datetime.utcnow() - getattr(last, timestamp) >= timedelta(days=age))):
                result[name] = await job(db, client)
        except Exception as exc:
            db.rollback()
            log.exception("Scheduled %s job failed", name)
            result[name] = {"error": type(exc).__name__}
        finally:
            db.close()
    return result


async def nightly_loop(session_factory) -> None:
    while True:
        await asyncio.sleep(seconds_until_next_run())
        try:
            await run_nightly_once(session_factory)
        except Exception:
            log.exception("Nightly run crashed; will retry tomorrow")
