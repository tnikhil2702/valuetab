# services/xlsx_ingest.py
import io
import logging
from typing import Optional

import openpyxl
from sqlalchemy.orm import Session

from db import SyncLog, CustomServiceFile
from Cs_dashboard.services.client_normalizer import normalize_rows as normalize_client
from Cs_dashboard.services.ramos_normalizer import normalize_rows as normalize_ramos
logger = logging.getLogger(__name__)


def _detect_file_type(raw_rows):
    for row_index, row in enumerate(raw_rows[:10]): 
        headers = [
            str(cell).strip().lower()
            for cell in row
            if cell is not None
        ]

        header_text = " | ".join(headers)

        logger.info("Checking row %s: %s", row_index, header_text)
        if (
            "submitter email" in header_text
            or "google review status" in header_text
            or "neos id" in header_text
        ):
            return "ramos", row_index

        if (
            ("call type" in header_text and "case" in header_text)
            or ("phone" in header_text and "attorney" in header_text)
            or ("name" in header_text and "date" in header_text)
        ):
            return "client", row_index

    raise ValueError(
        "Unsupported Excel format. "
        "Could not detect either Ramos Law Exit Interview.xlsx or Client.xlsx."
    )

def ingest_xlsx(
    file_bytes: bytes,
    file_name: str,
    db: Session,
    triggered_by: Optional[str] = None,
) -> SyncLog:

    log = SyncLog(
        sync_type="manual_upload",
        status="failed",
        triggered_by=triggered_by,
    )

    try:
        wb = openpyxl.load_workbook(
            io.BytesIO(file_bytes),
            data_only=True,
        )

        if "Forms Entries" not in wb.sheetnames:
            raise ValueError(
                "Sheet 'Forms Entries' not found in workbook."
            )

        ws = wb["Forms Entries"]

        raw_rows = [
            list(row)
            for row in ws.iter_rows(values_only=True)
        ]

        if not raw_rows:
            raise ValueError("Excel file is empty.")

        headers = raw_rows[0]
        data_rows = raw_rows[1:]

        file_type, header_index = _detect_file_type(raw_rows)

        logger.info(
            "Detected Excel type: %s (header row=%s)",
            file_type,
            header_index,
        )

        # Skip the detected header row
        data_rows = raw_rows[header_index + 1:]

        if file_type == "ramos":
            objects = normalize_ramos(data_rows)

        elif file_type == "client":
            objects = normalize_client(data_rows)

        else:
            raise ValueError("Unsupported Excel format.")

        upload = CustomServiceFile(
            file_name=file_name,
            uploaded_by=triggered_by,
            rows_fetched=len(data_rows),
        )

        db.add(upload)
        db.flush()

        inserted = 0
        skipped = 0

        for obj in objects:
            obj.file_id = upload.id
            db.add(obj)
            inserted += 1

        log.status = "success"
        log.rows_fetched = len(data_rows)
        log.rows_inserted = inserted
        log.rows_skipped = skipped

    except Exception as exc:
        logger.exception("XLSX ingest failed")

        db.rollback()

        log.status = "failed"
        log.error_message = str(exc)[:1000]

    db.add(log)
    db.commit()

    return log