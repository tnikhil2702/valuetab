
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from db import PeakTracAccount
from PeakTrac.neos_client import NeosClient, NeosRequestError, case_search_body, query_filter
from PeakTrac.neos_details import parse_date, pick_current_phase, truthy
from PeakTrac.neos_expenses import _first, _money, _norm
from PeakTrac import neos_cases

log = logging.getLogger("peaktrac.neos.watcher")

STATUS_COLUMNS = [
    "CaseNumber", "IsClosed", "DateClosed", "CaseDetails_CurrentStatus_Name",
]
BATCH_SIZE = 40
PHASE_CONCURRENCY = 5
PAYOFF_PENDING = "Payoff Pending"

_NS_PHASE = re.compile(r"negotiat\w*\W*(?:and|&)?\W*settle", re.I)
_STATEMENT_CODES = {"caseexp", "dtf"}
_AMOUNT_KEYS = ("TotalAmount", "Total", "Subtotal", "GroupTotal", "Amount")
_CODE_KEYS = ("Code", "ValueCode", "GroupCode", "Name", "Title")


# ---------------------------------------------------------------- pure helpers
def is_settlement_phase(phase: Any) -> bool:
    return bool(phase) and bool(_NS_PHASE.search(str(phase)))


def parse_settlement_summary(payload: Any) -> dict:
    """{settlement_amount, due_to_firm}: Decimal or None."""
    return {
        "settlement_amount": _money(_first(payload, ("TotalSettlementAmount",))),
        "due_to_firm": _money(_first(payload, ("TotalDueToFirm",))),
    }


def parse_statement_expenses(payload: Any) -> Decimal | None:
    """Sum of the CASEEXP + DTF group totals on the settlement statement, else None."""
    found: list[Decimal] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, dict):
            code = _norm(_first_flat(node, _CODE_KEYS)).replace(" ", "").replace("_", "")
            amount = _money(_first_flat(node, _AMOUNT_KEYS))
            if code in _STATEMENT_CODES and amount is not None:
                found.append(amount)          # a group total; do not descend (no double count)
                return
            for child in node.values():
                if isinstance(child, (dict, list)):
                    walk(child)

    walk(payload)
    return sum(found, Decimal("0.00")) if found else None


def _first_flat(node: dict, keys: tuple[str, ...]) -> Any:
    wanted = {k.casefold() for k in keys}
    for k, v in node.items():
        if str(k).casefold() in wanted and v not in (None, "", {}, []) and not isinstance(v, (dict, list)):
            return v
    return None


# ---------------------------------------------------------------- Neos reads
async def fetch_status_rows(client: NeosClient, case_numbers: list[str]) -> dict[str, dict]:
    """CaseNumber -> search row, for all given case numbers.

    First tries ONE query per chunk of numbers (Or-conjoined Equal filters). The plan
    asks for a single query but the Or form is untested, so every answer is verified:
    rows for numbers we did not ask for mean the filter was ignored. Anything missing
    is then fetched with the proven single-number Equal query.
    """
    wanted = {str(n) for n in case_numbers if n}
    out: dict[str, dict] = {}
    cols = list(STATUS_COLUMNS) + ([neos_cases.PHASE_COLUMN] if neos_cases.PHASE_COLUMN else [])
    ordered = sorted(wanted)
    for i in range(0, len(ordered), BATCH_SIZE):
        chunk = ordered[i:i + BATCH_SIZE]
        filters = [{**query_filter("CaseNumber", n, "Equal"), "QueryConjunctionString": "Or"}
                   for n in chunk]
        try:
            rows = await client.search_all(case_search_body(filters, take=100, columns=cols))
        except NeosRequestError as e:
            log.info("PT-9 batch status query refused (status=%s); using single lookups", e.status)
            rows = []
        got = {str(r.get("CaseNumber")): r for r in rows if r.get("CaseNumber") is not None}
        if any(n not in set(chunk) for n in got):      # filter ignored: distrust the batch
            got = {}
        out.update(got)
    for number in ordered:
        if number in out:
            continue
        data = await client.advanced_search(case_search_body(
            [query_filter("CaseNumber", number, "Equal")], take=2, columns=cols))
        rows = (data or {}).get("Results") or []
        if rows:
            exact = next((r for r in rows if str(r.get("CaseNumber")) == number), None)
            if exact is not None:
                out[number] = exact
    return out


async def fetch_phase(client: NeosClient, neos_case_id: str) -> str | None:
    return pick_current_phase(await client.get(f"/cases/{neos_case_id}/phases"))


async def fetch_settlement(client: NeosClient, neos_case_id: str) -> dict:
    """Both settlement reads. A failed call leaves its fields None and says why."""
    summary, statement = await asyncio.gather(
        client.get(f"/cases/{neos_case_id}/settlement/summary"),
        client.get(f"/cases/{neos_case_id}/value_items/settlement"),
        return_exceptions=True,
    )
    result = {"settlement_amount": None, "due_to_firm": None, "statement_expenses": None,
              "summary_ok": False, "reasons": []}
    for res in (summary, statement):
        if isinstance(res, Exception) and not isinstance(res, NeosRequestError):
            raise res                                   # auth errors stop the run
    if isinstance(summary, NeosRequestError):
        result["reasons"].append(f"settlement summary failed (status={summary.status})")
    else:
        parsed = parse_settlement_summary(summary)
        result.update(parsed)
        result["summary_ok"] = parsed["settlement_amount"] is not None
        if not result["summary_ok"]:
            result["reasons"].append("settlement summary had no TotalSettlementAmount/TotalDueToFirm")
    if isinstance(statement, NeosRequestError):
        result["reasons"].append(f"settlement statement failed (status={statement.status})")
    else:
        result["statement_expenses"] = parse_statement_expenses(statement)
        if result["statement_expenses"] is None:
            result["reasons"].append("no CASEEXP/DTF group on settlement statement")
    return result


# ---------------------------------------------------------------- rules
def _open_balance(account: PeakTracAccount) -> bool:
    return Decimal(str(account.principal or 0)) > 0 or Decimal(str(getattr(account, "payoff_amount", 0) or 0)) > 0


def apply_rules(account: PeakTracAccount, *, closed: bool | None, phase: str | None,
                settlement: dict | None, now: datetime) -> list[str]:
    """Apply the PT-9 status rules. Returns human-readable transitions (no names/IDs)."""
    events: list[str] = []

    if phase is not None:
        was = bool(getattr(account, "settlement_phase", False))
        account.settlement_phase = is_settlement_phase(phase)
        if account.settlement_phase and not was:
            events.append("settlement_phase set")

    amount = None
    if settlement is not None:
        if settlement.get("summary_ok") and settlement.get("statement_expenses") is not None:
            account.neos_settlement_checked_at = now
        else:
            account.neos_settlement_checked_at = None  # retry incomplete reads on the next run
        if settlement["settlement_amount"] is not None:
            account.neos_settlement_amount = settlement["settlement_amount"]
        if settlement["due_to_firm"] is not None:
            account.neos_due_to_firm = settlement["due_to_firm"]
        if settlement["statement_expenses"] is not None:
            account.neos_case_expenses_on_statement = settlement["statement_expenses"]
        amount = settlement["settlement_amount"]
        if amount is None:
            amount = account.neos_settlement_amount if settlement["summary_ok"] else None

    status = _norm(account.account_status)
    if amount is not None and amount > 0 and status == "active":
        account.account_status = PAYOFF_PENDING
        events.append("moved to Payoff Pending")

    if closed and settlement is not None and settlement["summary_ok"]:
        unrecovered = _open_balance(account) and not (amount is not None and amount > 0)
        if unrecovered and not getattr(account, "closed_unrecovered", False):
            events.append("closed_unrecovered set")
        account.closed_unrecovered = unrecovered
    elif closed is False and getattr(account, "closed_unrecovered", False):
        account.closed_unrecovered = False
        events.append("closed_unrecovered cleared (case reopened)")
    return events


# ---------------------------------------------------------------- the nightly run
async def run_watcher(db: Session, client: NeosClient, *, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    accounts = [
        a for a in db.query(PeakTracAccount).all()
        if a.case_number and a.neos_case_id and _norm(a.account_status) not in
        {"paid off", "closed", "denied", "pending approval"}
    ]
    summary = {"checked": 0, "settlement_pulls": 0, "transitions": [], "errors": [],
               "not_found": 0, "ran_at": now.isoformat() + "Z"}
    if not accounts:
        return summary

    rows = await fetch_status_rows(client, [a.case_number for a in accounts])
    sem = asyncio.Semaphore(PHASE_CONCURRENCY)

    async def one(account: PeakTracAccount) -> None:
        row = rows.get(str(account.case_number))
        if row is None:
            summary["not_found"] += 1
            summary["errors"].append({"account_id": account.id, "reason": "case not found in Neos"})
            return
        try:
            phase = row.get(neos_cases.PHASE_COLUMN) if neos_cases.PHASE_COLUMN else None
            if not neos_cases.PHASE_COLUMN:
                try:
                    async with sem:
                        phase = await fetch_phase(client, account.neos_case_id)
                except NeosRequestError as e:      # keep going: closed/settlement checks still matter
                    summary["errors"].append(
                        {"account_id": account.id, "reason": f"phase lookup failed (status={e.status})"})
            closed = truthy(row.get("IsClosed"))
            status_name = row.get("CaseDetails_CurrentStatus_Name")
            changed = (_norm(phase) != _norm(account.case_phase) and phase is not None) or \
                      (_norm(status_name) != _norm(account.case_status) and status_name is not None)

            if phase is not None:
                account.case_phase = phase
            if status_name is not None:
                account.case_status = status_name
            if closed is not None:
                account.neos_is_closed = closed
                account.neos_date_closed = parse_date(row.get("DateClosed")) if closed else None

            settlement = None
            if is_settlement_phase(phase) or changed or closed or account.neos_settlement_checked_at is None:
                async with sem:
                    settlement = await fetch_settlement(client, account.neos_case_id)
                summary["settlement_pulls"] += 1
                for reason in settlement["reasons"]:
                    summary["errors"].append({"account_id": account.id, "reason": reason})

            events = apply_rules(account, closed=closed, phase=phase, settlement=settlement, now=now)
            for ev in events:
                summary["transitions"].append({"account_id": account.id, "event": ev})
            summary["checked"] += 1
        except NeosRequestError as e:
            summary["errors"].append({"account_id": account.id, "reason": f"Neos call failed (status={e.status})"})

    await asyncio.gather(*(one(a) for a in accounts))
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    log.info("PT-9 watcher: checked=%s pulls=%s transitions=%s errors=%s",
             summary["checked"], summary["settlement_pulls"],
             len(summary["transitions"]), len(summary["errors"]))
    return summary
