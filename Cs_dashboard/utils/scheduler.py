# utils/scheduler.py
"""
APScheduler-based background cron — polls Graph API every 5 minutes.
Registered on FastAPI startup via lifespan.
"""
import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler

from db import SessionLocal
from services.graph_sync import sync_from_graph

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()


def _sync_job():
    # Only run if Graph API env vars are configured
    required = ["AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
                "SHAREPOINT_SITE_ID", "SHAREPOINT_DRIVE_ID", "SHAREPOINT_FILE_ID"]
    if not all(os.getenv(k) for k in required):
        logger.info("Graph API env vars not set — skipping scheduled sync.")
        return

    db = SessionLocal()
    try:
        log = sync_from_graph(db, triggered_by="scheduler")
        logger.info(f"Scheduled sync: {log.status} — inserted={log.rows_inserted}")
    except Exception as e:
        logger.exception(f"Scheduled sync error: {e}")
    finally:
        db.close()


def start_scheduler():
    scheduler.add_job(_sync_job, "interval", minutes=5, id="graph_sync", replace_existing=True)
    scheduler.start()
    logger.info("CSD background scheduler started (5-min Graph sync).")


def stop_scheduler():
    scheduler.shutdown(wait=False)
