from __future__ import annotations

import os
from datetime import datetime
from typing import Callable, Iterable

from PeakTrac.neos_client import NeosClient, case_search_body, query_filter

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


OPEN_FILTER_VALUE = os.getenv("NEOS_ISCLOSED_FILTER_VALUE", "false")
CLIENT_NAME_COLUMN = os.getenv("NEOS_CLIENT_NAME_COLUMN", "CaseName")
LAST_NAME_BINDING = os.getenv("NEOS_LASTNAME_BINDING", "CaseName")
LAST_NAME_OPERATOR = os.getenv("NEOS_LASTNAME_OPERATOR", "Contains")
PHASE_COLUMN = os.getenv("NEOS_PHASE_COLUMN") or None


def _columns() -> list[str]:
    cols = [
        "CaseNumber", "CaseType_Code", "DateOpened", "IsClosed",
        "CaseDetails_CurrentStatus_Name", "StaffAssigned[Lead Attorney]",
        "UserCaseData[Office Location]", "LimitationDate", CLIENT_NAME_COLUMN,
    ]
    if PHASE_COLUMN:
        cols.append(PHASE_COLUMN)
    return cols


def _open_value():
    return False if OPEN_FILTER_VALUE == "bool" else OPEN_FILTER_VALUE


def build_open_cases_body(q: str | None, page: int, page_size: int) -> dict:
    page = max(1, page)
    page_size = min(max(1, page_size), MAX_PAGE_SIZE)
    filters = [query_filter("IsClosed", _open_value(), "Equal")]
    q = (q or "").strip()
    if q:
        if q.isdigit():
            filters.append(query_filter("CaseNumber", q, "Equal"))
        else:
            filters.append(query_filter(LAST_NAME_BINDING, q, LAST_NAME_OPERATOR))
    return case_search_body(
        filters, take=page_size, skip=(page - 1) * page_size, columns=_columns()
    )


def _iso(value):
    if not value:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value)[:19] if "T" in str(value) else str(value), fmt).date().isoformat()
        except ValueError:
            continue
    return str(value)


def map_row(row: dict, imported: set[str]) -> dict:
    number = str(row.get("CaseNumber") or "")
    return {
        "neos_case_id": row.get("Id"),
        "case_number": number,
        "client_name": row.get(CLIENT_NAME_COLUMN),
        "case_type": row.get("CaseType_Code"),
        "lead_attorney": row.get("StaffAssigned[Lead Attorney]"),
        "office": row.get("UserCaseData[Office Location]"),
        "date_opened": _iso(row.get("DateOpened")),
        "phase": row.get(PHASE_COLUMN) if PHASE_COLUMN else None,
        "status": row.get("CaseDetails_CurrentStatus_Name"),
        "sol_date": _iso(row.get("LimitationDate")),
        "already_imported": number in imported,
    }


async def get_open_cases(
    client: NeosClient,
    imported_lookup: Callable[[Iterable[str]], set[str]],
    q: str | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict:
    page = max(1, page)
    page_size = min(max(1, page_size), MAX_PAGE_SIZE)
    data = await client.advanced_search(build_open_cases_body(q, page, page_size))
    rows = (data or {}).get("Results") or []
    total = int((data or {}).get("TotalResultsCount") or 0)
    imported = imported_lookup([str(r.get("CaseNumber") or "") for r in rows])
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": page * page_size < total,
        "data": [map_row(r, imported) for r in rows],
    }