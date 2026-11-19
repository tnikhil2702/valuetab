from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from PeakTrac.neos_client import (
    NeosClient, NeosRequestError, case_search_body, query_filter,
)

log = logging.getLogger("peaktrac.neos")
FIRST_PARTY_COLUMN = os.getenv("NEOS_FIRST_PARTY_COLUMN", "FirstPartyIsOurClient")
LEAD_ROLE = os.getenv("NEOS_LEAD_ATTORNEY_ROLE", "Lead Attorney")

_BASE_COLUMNS = [
    "CaseNumber", "CaseName", "CaseType_Code", "DateOpened", "IsClosed", "DateClosed",
    "CaseDetails_CurrentStatus_Name", "StaffAssigned[Lead Attorney]",
    "UserCaseData[Office Location]", "LimitationDate",
]
_SOL_LABEL = re.compile(r"statute|limitation|\bsol\b", re.I)
_DOI_LABEL = re.compile(r"incident|\bdoi\b|date of (loss|injury|accident)", re.I)


# ----------------------------------------------------------------- parsing helpers
def parse_date(value: Any) -> date | None:
    """Accepts MM/DD/YYYY (search) and ISO strings (detail calls)."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def truthy(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


def pick_lead_attorney(assignments: Any) -> tuple[str | None, str | None]:
    """(name, staff_id) of the Lead Attorney assignment, else (None, None)."""
    for a in assignments or []:
        if str(a.get("StaffAssignment") or "").strip().lower() == LEAD_ROLE.lower():
            return a.get("Staff"), a.get("StaffId")
    return None, None


def pick_current_phase(phases: Any) -> str | None:
    phases = [p for p in (phases or []) if isinstance(p, dict) and p.get("Title")]
    for p in phases:
        if not p.get("Completed"):
            return p["Title"]
    return phases[-1]["Title"] if phases else None


def pick_dates(case_dates: Any) -> tuple[date | None, date | None]:
    items = case_dates.get("CaseDates") if isinstance(case_dates, dict) else case_dates
    incident = sol = None
    for d in items or []:
        label, value = str(d.get("DateLabel") or ""), parse_date(d.get("Date"))
        if value is None:
            continue
        if sol is None and _SOL_LABEL.search(label):
            sol = value
        elif incident is None and _DOI_LABEL.search(label):
            incident = value
    return incident, sol


def policy_limit_max(policies: Any) -> Decimal | None:
    best = None
    for p in policies or []:
        try:
            v = Decimal(str(p.get("Maximum")))
        except (InvalidOperation, TypeError):
            continue
        if v > 0 and (best is None or v > best):
            best = v
    return best


# ----------------------------------------------------------------- lookups
async def _find_row(client: NeosClient, case_number: str) -> dict | None:
    async def run(columns):
        body = case_search_body(
            [query_filter("CaseNumber", str(case_number), "Equal")],
            take=2, columns=columns,
        )
        return await client.advanced_search(body)

    columns = _BASE_COLUMNS + ([FIRST_PARTY_COLUMN] if FIRST_PARTY_COLUMN else [])
    try:
        data = await run(columns)
    except NeosRequestError as e:
        if FIRST_PARTY_COLUMN and e.status and 400 <= e.status < 500:
            log.warning("Neos rejected column %s; retrying without it", FIRST_PARTY_COLUMN)
            data = await run(_BASE_COLUMNS)
        else:
            raise
    rows = (data or {}).get("Results") or []
    return next((r for r in rows if str(r.get("CaseNumber")) == case_number), None)


async def fetch_case_details(client: NeosClient, case_number: str | int) -> dict | None:
    """Return the details dict, or None if no such case number exists in Neos."""
    row = await _find_row(client, str(case_number))
    if not row or not row.get("Id"):
        return None
    cid = row["Id"]

    names = ("header", "staffAssignments", "phases", "caseDates", "insurance")
    results = await asyncio.gather(
        client.get(f"/cases/v2/{cid}"),
        client.get(f"/cases/v2/{cid}/staffAssignments"),
        client.get(f"/cases/{cid}/phases"),
        client.get(f"/cases/v2/{cid}/caseDates"),
        client.get(f"/neos/cases/{cid}/lists/insurance"),
        return_exceptions=True,
    )
    got: dict[str, Any] = {}
    reasons: dict[str, str] = {}
    for name, res in zip(names, results):
        if isinstance(res, NeosRequestError):
            got[name] = None
            reasons[name] = f"Neos call failed (status={res.status})"
            log.warning("PT-4 %s unavailable: status=%s", name, res.status)
        elif isinstance(res, Exception):
            raise res                      # auth errors etc. should stop the import
        else:
            got[name] = res

    header = got["header"] if isinstance(got["header"], dict) else {}
    lead_name, lead_id = pick_lead_attorney(got["staffAssignments"])
    incident, sol_from_dates = pick_dates(got["caseDates"])
    sol = parse_date(row.get("LimitationDate")) or sol_from_dates
    incident = parse_date(header.get("DateOfIncident")) or incident
    policy_max = policy_limit_max(got["insurance"])

    details = {
        "neos_case_id": str(cid),
        "case_number": str(row.get("CaseNumber") or case_number),
        "client_name": row.get("CaseName") or header.get("CaseName"),
        "is_closed": truthy(row.get("IsClosed")),
        "date_closed": parse_date(row.get("DateClosed")),
        "case_type_code": row.get("CaseType_Code"),
        "case_status": row.get("CaseDetails_CurrentStatus_Name"),
        "date_opened": parse_date(row.get("DateOpened")),
        "first_party_is_our_client": truthy(row.get(FIRST_PARTY_COLUMN)) if FIRST_PARTY_COLUMN else None,
        "primary_staff_code": header.get("PrimaryStaff"),
        "lead_attorney_name": lead_name or row.get("StaffAssigned[Lead Attorney]"),
        "lead_attorney_staff_id": lead_id,
        "office": row.get("UserCaseData[Office Location]"),
        "phase": pick_current_phase(got["phases"]),
        "incident_date": incident,
        "sol_date": sol,
        "policy_limit_max": policy_max,
        "synced_at": datetime.now(timezone.utc),
        "null_reasons": reasons,
    }
    # Record why a field is empty when no call failed (data simply isn't in Neos).
    for field, label in (("lead_attorney_name", "no lead attorney assigned"),
                         ("phase", "no phases returned"),
                         ("incident_date", "no incident date found"),
                         ("sol_date", "no statute date found"),
                         ("policy_limit_max", "no insurance policy with a limit")):
        if details[field] is None:
            reasons.setdefault(field, label)
    if reasons:
        log.info("PT-4 null fields: %s", sorted(reasons))
    return details