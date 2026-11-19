import os
import logging
from typing import List, Optional

import httpx
from sqlalchemy.orm import Session

from db import ExitInterview, SyncLog
from Cs_dashboard.services.client_normalizer import normalize_rows as normalize_client
from Cs_dashboard.services.ramos_normalizer import normalize_rows as normalize_ramos

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


def _detect_file_type(headers: list) -> str:
    headers = [str(h).strip().lower() if h else "" for h in headers]

    if (
        "submitter email" in headers
        or "google review status" in headers
        or "neos id" in headers
    ):
        return "ramos"

    if (
        "call type" in headers
        or "case number" in headers
        or "phone" in headers
    ):
        return "client"

    raise ValueError(
        "Unsupported Excel format."
    )

def _get_token(
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> str:

    url = TOKEN_URL.format(tenant=tenant_id)

    resp = httpx.post(
        url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=30,
    )

    resp.raise_for_status()

    return resp.json()["access_token"]


def _fetch_worksheet_rows(
    token: str,
    site_id: str,
    drive_id: str,
    file_id: str,
) -> List[List]:

    url = (
        f"{GRAPH_BASE}/sites/{site_id}/drives/{drive_id}"
        f"/items/{file_id}/workbook/worksheets/Forms%20Entries/usedRange"
    )

    resp = httpx.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )

    resp.raise_for_status()

    return resp.json().get("values", [])


def sync_from_graph(
    db: Session,
    triggered_by: Optional[str] = None,
) -> SyncLog:

    tenant_id = os.environ["Tenant_ID"]
    client_id = os.environ["AZURE_CLIENT_ID"]
    client_secret = os.environ["AZURE_CLIENT_SECRET"]

    site_id = os.environ["SHAREPOINT_SITE_ID"]
    drive_id = os.environ["SHAREPOINT_DRIVE_ID"]
    file_id = os.environ["SHAREPOINT_FILE_ID"]

    log = SyncLog(
        sync_type="graph_api",
        status="failed",
        triggered_by=triggered_by,
    )

    try:

        token = _get_token(
            tenant_id,
            client_id,
            client_secret,
        )

        raw_rows = _fetch_worksheet_rows(
            token,
            site_id,
            drive_id,
            file_id,
        )

        if not raw_rows:
            raise ValueError("No data found in SharePoint workbook.")

        headers = raw_rows[0]
        data_rows = raw_rows[1:]

        file_type = _detect_file_type(headers)

        logger.info("Detected Excel format: %s", file_type)

        if file_type == "ramos":
            objects = normalize_ramos(data_rows)

        elif file_type == "client":
            objects = normalize_client(data_rows)

        else:
            raise ValueError("Unsupported Excel format.")

        inserted = 0
        skipped = 0

        for obj in objects:

            existing = (
                db.query(ExitInterview)
                .filter(
                    ExitInterview.source_row_id == obj.source_row_id
                )
                .first()
            )

            if existing:
                skipped += 1
                continue

            db.add(obj)
            inserted += 1

        db.flush()

        log.status = "success"
        log.rows_fetched = len(data_rows)
        log.rows_inserted = inserted
        log.rows_skipped = skipped

    except Exception as exc:

        logger.exception("Graph sync failed")

        db.rollback()

        log.status = "failed"
        log.error_message = str(exc)[:1000]

    db.add(log)
    db.commit()

    return log


def get_last_sync(db: Session) -> Optional[SyncLog]:
    return (
        db.query(SyncLog)
        .order_by(SyncLog.synced_at.desc())
        .first()
    )