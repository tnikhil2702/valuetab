from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from db import PeakTracAccount, Draw, NeosExpenseLine
from PeakTrac.category_map import category_for_neos_code, category_matches, draw_matches_code
from PeakTrac.neos_client import NeosClient, NeosRequestError

log = logging.getLogger("peaktrac.neos.expenses")

CENT = Decimal("0.01")
AMOUNT_TOLERANCE = Decimal("0.01")
DATE_WINDOW_DAYS = 30

APPROVED_STATUSES = {
    "approved",
    "approve",
    "approved for payment",
    "paid",
    "posted",
}

FUNDED_DRAW_STATUSES = {"funded"}

REQUIRE_APPROVED = os.getenv("NEOS_MATCH_REQUIRE_APPROVED", "false").strip().lower() == "true"

# Accounts in these states are not "active": no expense pull for them.
INACTIVE_ACCOUNT_STATUSES = {"paid off", "closed", "denied"}

ID_KEYS = (
    "Id", "ID", "ValueId", "ValueID",
    "ValueTransactionId", "TimesAndValueId",
    "TransactionId", "valueId", "id",
)

CASE_ID_KEYS = (
    "CaseId", "CaseID", "caseId",
    "MatterId", "matterId", "NeosCaseId",
    "Case.Id", "Case.CaseId", "Case.NeosCaseId",
    "Matter.Id", "Case",
)

CASE_NUMBER_KEYS = (
    "CaseNumber", "caseNumber", "MatterNumber",
    "FileNumber", "Case.Number", "Case.CaseNumber",
    "Matter.Number",
)

AMOUNT_KEYS = (
    "TotalAmount", "totalAmount", "Amount", "amount",
    "TransactionAmount", "transactionAmount",
    "ValueAmount", "valueAmount", "Value",
)

CODE_KEYS = (
    "ExpenseCode", "ExpenseCodeCode", "Code",
    "code", "ValueCode", "valueCode",
    "ExpenseCategory",
)

CODE_TYPE_KEYS = (
    "CodeType", "ValueCodeType", "ValueCodeTypeName",
    "ExpenseCodeType", "ValueCode.Type", "ExpenseCode.Type",
)

PAYEE_KEYS = (
    "Payee", "PayeeName", "Vendor",
    "VendorName", "Provider", "Name",
)

DUE_KEYS = ("DueDate", "Due", "due", "DateDue")

SERVICE_KEYS = (
    "ServiceDate", "DateOfService",
    "TransactionDate", "Date", "serviceDate",
)

CREATED_KEYS = ("Created", "CreatedAt", "CreatedDate", "DateCreated")
MODIFIED_KEYS = ("Modified", "ModifiedAt", "ModifiedDate", "LastModified")
APPROVAL_KEYS = ("ApprovalStatus", "Status", "status", "ApprovalState")
BILLED_KEYS = ("Billed", "IsBilled", "billed")
# Do not persist Memo or Notes; PT-6 explicitly excludes free-text memo fields.
REFERENCE_KEYS = ("Reference", "Description", "InvoiceNumber")


def _first(row: Any, keys: tuple[str, ...]) -> Any:
    """Find an explicit field, case-insensitively, including nested objects."""
    if not isinstance(row, dict):
        return None

    wanted = {key.casefold() for key in keys}

    # Resolve explicit paths such as Case.Id first.
    for path in keys:
        if "." not in path:
            continue

        value = row
        for part in path.split("."):
            if not isinstance(value, dict):
                value = None
                break

            value = next(
                (
                    item
                    for key, item in value.items()
                    if str(key).casefold() == part.casefold()
                ),
                None,
            )

        if value is not None and value != "":
            return value

    # Prefer fields on the current object.
    for key, value in row.items():
        if str(key).casefold() in wanted and value is not None and value != "":
            return value

    # Search nested objects and lists.
    for value in row.values():
        if isinstance(value, dict):
            found = _first(value, keys)
            if found is not None:
                return found

        elif isinstance(value, list):
            for child in value:
                if isinstance(child, dict):
                    found = _first(child, keys)
                    if found is not None:
                        return found

    return None


def _text_value(value: Any, preferred_keys: tuple[str, ...]) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        wanted = {key.casefold() for key in preferred_keys}
        for key, child in value.items():
            if str(key).casefold() in wanted and child not in (None, "", {}, []):
                result = _text_value(child, preferred_keys)
                if result:
                    return result
        # A nested object may itself wrap the useful object.
        for child in value.values():
            if isinstance(child, dict):
                result = _text_value(child, preferred_keys)
                if result:
                    return result
        return None
    if isinstance(value, (list, tuple)):
        return None
    return str(value).strip() or None


def _text_field(row: dict, keys: tuple[str, ...], preferred_keys: tuple[str, ...]) -> str | None:
    return _text_value(_first(row, keys), preferred_keys)


def _records(payload: Any) -> list[dict]:
    """Unwrap common Neos list response envelopes."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        return []

    for key in (
        "Results", "results", "Data", "data",
        "Items", "items", "ValueTransactions",
        "TimesAndValues",
    ):
        value = payload.get(key)

        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

        if isinstance(value, dict):
            found = _records(value)
            if found:
                return found

    for value in payload.values():
        if isinstance(value, (dict, list)):
            found = _records(value)
            if found:
                return found

    return []


def _money(value: Any) -> Decimal | None:
    if value is None:
        return None

    try:
        text = str(value).strip().replace("$", "").replace(",", "")
        amount = Decimal(text).quantize(CENT)
        return amount if amount.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _date(value: Any) -> date | None:
    if not value:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    text = str(value).strip()

    for fmt in (
        "%m/%d/%Y",
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%fZ",
    ):
        try:
            return datetime.strptime(text[:26], fmt).date()
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(
            text.replace("Z", "+00:00")
        ).date()
    except ValueError:
        return None


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None

    if isinstance(value, datetime):
        return value

    try:
        return datetime.fromisoformat(
            str(value).strip().replace("Z", "+00:00")
        ).replace(tzinfo=None)
    except ValueError:
        return None


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _approved(status: Any) -> bool:
    return _norm(status) in APPROVED_STATUSES


def _identifier(value: Any) -> str | None:
    """Convert a scalar identifier to a normalized lookup string."""
    if value is None or isinstance(value, (dict, list)):
        return None

    text = str(value).strip()
    return text or None


def _case_identifiers(row: dict) -> tuple[str | None, str | None]:
    case_value = _first(row, ("Case",))

    case_id = None
    case_number = None

    if isinstance(case_value, dict):
        case_id = _first(
            case_value,
            ("NeosCaseId", "CaseId", "CaseID", "Id", "ID", "Guid", "UUID"),
        )
        case_number = _first(
            case_value,
            ("CaseNumber", "Number", "MatterNumber", "FileNumber"),
        )

    if case_id is None:
        case_id = _first(
            row,
            ("NeosCaseId", "CaseId", "CaseID", "MatterId",
             "Case.Id", "Case.CaseId", "Case.NeosCaseId", "Matter.Id"),
        )

    if case_number is None:
        case_number = _first(
            row,
            ("CaseNumber", "caseNumber", "MatterNumber", "FileNumber",
             "Case.Number", "Case.CaseNumber", "Matter.Number"),
        )

    # Some endpoints return Case as a scalar UUID or case number.
    if not isinstance(case_value, dict) and case_value is not None:
        scalar_case = _identifier(case_value)
        if scalar_case and case_id is None:
            case_id = scalar_case

    return _identifier(case_id), _identifier(case_number)


def _normalise_case_key(value: Any) -> str | None:
    """Normalize UUIDs and case numbers for stable lookup."""
    text = _identifier(value)
    return text.casefold() if text else None


def _is_due_to_firm(row: dict) -> bool:
    code_type = _norm(_first(row, CODE_TYPE_KEYS))
    code = _norm(_first(row, CODE_KEYS))

    normalized_type = (
        code_type.replace(" ", "")
        .replace("-", "")
        .replace("_", "")
    )
    normalized_code = (
        code.replace(" ", "")
        .replace("-", "")
        .replace("_", "")
    )

    return normalized_type == "duetofirm" or normalized_code in {"caseexp", "dtf"}


def _map_record(
    row: dict,
    accounts_by_neos_id: dict[str, PeakTracAccount],
    skip_reasons: dict[str, int] | None = None,
) -> dict | None:

    def skip(reason: str) -> None:
        if skip_reasons is not None:
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

        log.warning(
            "Skipping Neos expense: reason=%s; available_keys=%s; "
            "amount_candidates=%s",
            reason,
            sorted(map(str, row.keys())),
            {
                key: row.get(key)
                for key in AMOUNT_KEYS
                if key in row and not isinstance(row.get(key), (dict, list))
            },
        )

    value_id = _identifier(_first(row, ID_KEYS))
    case_id, case_number = _case_identifiers(row)
    if case_id is None:
        case_id = _identifier(row.get("_peaktrac_requested_neos_case_id"))
    amount = _money(_first(row, AMOUNT_KEYS))

    if value_id is None:
        skip("missing_expense_id")
        return None

    if case_id is None and case_number is None:
        skip("missing_case_id")
        return None

    if amount is None:
        skip("missing_or_invalid_amount")
        return None

    # Match exact Neos UUID first, then exact case number.
    account = None

    case_id_key = _normalise_case_key(case_id)
    case_number_key = _normalise_case_key(case_number)

    if case_id_key:
        account = accounts_by_neos_id.get(case_id_key)

    if account is None and case_number_key:
        account = accounts_by_neos_id.get(case_number_key)

    if account is None:
        # If Case was scalar, it may be a case number.
        raw_case = _identifier(_first(row, ("Case",)))

        raw_case_key = _normalise_case_key(raw_case)
        if raw_case_key:
            account = accounts_by_neos_id.get(raw_case_key)

    if account is None:
        log.warning(
            "Neos case mapping failed: expense_id=%s case_id=%r "
            "case_number=%r; known_mapping_keys=%s",
            value_id,
            case_id,
            case_number,
            len(accounts_by_neos_id),
        )
        skip("case_id_not_found_in_peaktrac_accounts")
        return None

    return {
        "neos_value_id": value_id,
        "account_id": account.id,
        "code_type": _text_field(
            row, CODE_TYPE_KEYS, ("Type", "Title", "Name", "Code")
        ),
        "code": _text_field(
            row, CODE_KEYS, ("Title", "Code", "Name", "Value")
        ),
        "payee_name": _text_field(
            row, PAYEE_KEYS, ("FullName", "Name", "Title", "DisplayName")
        ),
        "amount": amount,
        "due": _date(_first(row, DUE_KEYS)),
        "service_date": _date(_first(row, SERVICE_KEYS)),
        "neos_created": _datetime(_first(row, CREATED_KEYS)),
        "neos_modified": _datetime(_first(row, MODIFIED_KEYS)),
        "approval_status": str(_first(row, APPROVAL_KEYS) or "") or None,
        "billed": str(_first(row, BILLED_KEYS) or "") or None,
        "reference": str(_first(row, REFERENCE_KEYS) or "") or None,
    }


async def fetch_neos_expense_records(
    client: NeosClient,
    accounts: list[PeakTracAccount],
    errors: list[dict] | None = None,
) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    cases_queried = 0

    cases_by_id: dict[str, PeakTracAccount] = {}
    for account in accounts:
        case_id = _identifier(getattr(account, "neos_case_id", None))
        key = _normalise_case_key(case_id)
        if key and key not in cases_by_id:
            cases_by_id[key] = account

    for case_key, account in cases_by_id.items():
        case_id = _identifier(getattr(account, "neos_case_id", None))
        if not case_id:
            continue

        continuation_token = None
        page_number = 0
        seen_tokens: set[str] = set()

        while True:
            params: dict[str, Any] = {
                "caseId": case_id,
                "type": "Value",
                "pageSize": 200,
            }
            if continuation_token:
                params["ContinuationToken"] = continuation_token


            try:
                payload = await client.get("/timesandvalues", params=params)
            except NeosRequestError as exc:
                log.warning("Neos expense pull failed for account_id=%s status=%s",
                            account.id, exc.status)
                if errors is not None:
                    errors.append({"account_id": account.id, "status": exc.status})
                break
            def collection_shape(node):
                if isinstance(node, list): return True
                if isinstance(node, dict):
                    return any(collection_shape(node[k]) for k in (
                        'Results', 'results', 'Data', 'data', 'Items', 'items', 'TimesAndValues'
                    ) if k in node)
                return False
            if not collection_shape(payload):
                if errors is not None:
                    errors.append({'account_id': account.id, 'reason': 'Unrecognized expense collection'})
                break
            page_number += 1
            page_rows = _records(payload)
            log.info(
                "Neos timesandvalues response: case_id=%s page=%s "
                "payload_type=%s payload_keys=%s parsed_rows=%s",
                case_id,
                page_number,
                type(payload).__name__,
                sorted(map(str, payload.keys())) if isinstance(payload, dict) else None,
                len(page_rows),
            )
            due_to_firm_count = 0

            for row in page_rows:
                row_case_id, _ = _case_identifiers(row)
                if row_case_id and _normalise_case_key(row_case_id) != case_key:
                    log.warning(
                        "Ignoring Neos timesandvalues row outside requested case: "
                        "requested_case_id=%s row_case_id=%s",
                        case_id,
                        row_case_id,
                    )
                    continue

                if not _is_due_to_firm(row):
                    log.debug(
                        "Skipping non-matching Neos value: case_id=%s "
                        "row_keys=%s code=%r code_type=%r",
                        case_id,
                        sorted(map(str, row.keys())),
                        _first(row, CODE_KEYS),
                        _first(row, CODE_TYPE_KEYS),
                    )
                    continue

                due_to_firm_count += 1
                row_id = _identifier(_first(row, ID_KEYS))
                dedupe_key = row_id or repr(
                    sorted(row.items(), key=lambda item: str(item[0]))
                )
                dedupe_key = f"{case_key}:{dedupe_key}"
                if dedupe_key not in seen:
                    seen.add(dedupe_key)
                    if not row_case_id:
                        row = dict(row)
                        row["_peaktrac_requested_neos_case_id"] = case_id
                    rows.append(row)

            log.info(
                "Neos expense filtering: case_id=%s page=%s parsed_rows=%s "
                "due_to_firm_rows=%s",
                case_id,
                page_number,
                len(page_rows),
                due_to_firm_count,
            )

            next_token = _first(
                payload,
                ("ContinuationToken", "continuationToken", "continuation_token"),
            )
            if next_token in (None, ""):
                break

            next_token = str(next_token)
            if next_token in seen_tokens:
                raise RuntimeError(
                    f"Neos returned a repeated ContinuationToken for case {case_id}; "
                    "stopping pagination to avoid an infinite loop."
                )
            seen_tokens.add(next_token)
            continuation_token = next_token

        cases_queried += 1
        log.info(
            "Fetched Neos expense pages for stored case: case_id=%s pages=%s",
            case_id,
            page_number,
        )

    log.info(
        "Neos expense fetch complete: stored_cases_queried=%s unique_records=%s",
        cases_queried,
        len(rows),
    )
    return rows


def _upsert_expense(
    db: Session,
    data: dict,
    seen_at: datetime,
) -> bool:
    obj = (
        db.query(NeosExpenseLine)
        .filter_by(neos_value_id=data["neos_value_id"])
        .first()
    )

    is_new = obj is None

    if obj is None:
        obj = NeosExpenseLine(**data, last_seen_at=seen_at)
        db.add(obj)
    else:
        # Preserve the existing reconciliation link during refresh.
        for key, value in data.items():
            setattr(obj, key, value)

        obj.last_seen_at = seen_at

    return is_new


def _draw_date(draw: Draw) -> date | None:
    for field in ("funded_at", "requested_at"):
        value = getattr(draw, field, None)

        if isinstance(value, datetime):
            return value.date()

        if isinstance(value, date):
            return value

    return None


def reconcile_expenses(
    db: Session,
    *,
    dry_run: bool = False,
) -> dict:
    """Greedy deterministic 1:1 matching of approved expenses and funded draws."""
    expenses = (
        db.query(NeosExpenseLine)
        .filter(NeosExpenseLine.is_current.is_(True))
        .order_by(NeosExpenseLine.id.asc())
        .all()
    )
    draws = db.query(Draw).all()

    account_by_id = {
        account.id: account
        for account in db.query(PeakTracAccount).all()
    }

    eligible_draws = []

    for draw in draws:
        status = _norm(getattr(draw, "draw_status", None))

        if status not in FUNDED_DRAW_STATUSES:
            continue

        amount = _money(getattr(draw, "amount", None))
        category = _norm(
            getattr(draw, "expense_category", None)
            or getattr(draw, "category", None)
        )
        draw_date = _draw_date(draw)

        if amount is None or not category or draw_date is None:
            continue

        eligible_draws.append(
            (draw, amount, category, draw_date)
        )

    if not dry_run:
        eligible_by_id = {d.id: (d, amount, category, day) for d, amount, category, day in eligible_draws}
        for expense in expenses:
            if expense.match_source != 'auto' or expense.matched_draw_id is None: continue
            candidate = eligible_by_id.get(expense.matched_draw_id)
            day = expense.service_date or expense.due or (expense.neos_created.date() if expense.neos_created else None)
            valid = candidate is not None and day is not None
            if valid:
                draw, amount, category, draw_day = candidate
                valid = (draw.account_id == expense.account_id and
                         abs(amount - Decimal(str(expense.amount))) <= AMOUNT_TOLERANCE and
                         draw_matches_code(draw, expense.code, db) and
                         abs((day - draw_day).days) <= DATE_WINDOW_DAYS)
            if not valid:
                expense.matched_draw_id = None
                expense.match_source = None
        db.flush()  # release unique links before assigning new ones

    used_draw_ids = {
        expense.matched_draw_id
        for expense in expenses
        if expense.matched_draw_id is not None
    }

    matched = 0

    for expense in expenses:
        if expense.matched_draw_id is not None:
            continue

        if expense.manual_unlinked:
            continue          # a person unlinked it; do not re-link

        if REQUIRE_APPROVED and not _approved(expense.approval_status):
            continue

        account = account_by_id.get(expense.account_id)

        expense_date = (
            expense.service_date
            or expense.due
            or (
                expense.neos_created.date()
                if expense.neos_created
                else None
            )
        )

        if account is None or expense_date is None:
            continue

        expense_code = _norm(expense.code)

        if not expense_code:
            continue

        candidates = []

        for draw, draw_amount, draw_category, draw_date in eligible_draws:
            if draw.id in used_draw_ids:
                continue

            if draw.account_id != expense.account_id:
                continue

            if abs(draw_amount - Decimal(str(expense.amount))) > AMOUNT_TOLERANCE:
                continue

            if not draw_matches_code(draw, expense_code, db):
                continue

            day_difference = abs((expense_date - draw_date).days)

            if day_difference > DATE_WINDOW_DAYS:
                continue

            candidates.append(
                (day_difference, draw.id, draw)
            )

        if candidates:
            _, _, chosen = sorted(
                candidates,
                key=lambda item: (item[0], item[1]),
            )[0]

            if not dry_run:
                expense.matched_draw_id = chosen.id
                expense.match_source = "auto"

            used_draw_ids.add(chosen.id)
            matched += 1

    if not dry_run:
        db.commit()

    linked_ids = {
        expense.matched_draw_id
        for expense in expenses
        if expense.matched_draw_id is not None
    }

    neos_only = [
        expense
        for expense in expenses
        if expense.matched_draw_id is None
    ]

    peaktrac_only = [
        draw
        for draw, *_ in eligible_draws
        if draw.id not in linked_ids
    ]

    return {
        "matched_count": matched,
        "matched_total": len(linked_ids),
        "neos_only_count": len(neos_only),
        "peaktrac_only_count": len(peaktrac_only),
        "dry_run": dry_run,
    }


async def sync_neos_expenses(
    db: Session,
    client: NeosClient,
    account_ids: set[int] | None = None,
) -> dict:
    """Import Neos expenses for mapped accounts, then reconcile."""
    accounts = db.query(PeakTracAccount).all()

    accounts_by_neos_id: dict[str, PeakTracAccount] = {}

    for account in accounts:
        neos_case_id = _identifier(
            getattr(account, "neos_case_id", None)
        )
        case_number = _identifier(
            getattr(account, "case_number", None)
        )

        neos_case_key = _normalise_case_key(neos_case_id)
        case_number_key = _normalise_case_key(case_number)

        if neos_case_key:
            accounts_by_neos_id[neos_case_key] = account

        if case_number_key:
            accounts_by_neos_id[case_number_key] = account

    accounts_with_neos_ids = [
        account for account in accounts
        if _identifier(getattr(account, "neos_case_id", None))
        and _norm(getattr(account, "account_status", None)) not in INACTIVE_ACCOUNT_STATUSES
        and (account_ids is None or account.id in account_ids)
    ]
    account_errors: list[dict] = []
    if not accounts_with_neos_ids:
        log.warning(
            "Neos expense sync skipped: no PeakTrac accounts have neos_case_id set"
        )
        raw = []
    else:
        raw = await fetch_neos_expense_records(client, accounts_with_neos_ids, account_errors)

    now = datetime.utcnow()
    imported = 0
    updated = 0
    skipped = 0
    skip_reasons: dict[str, int] = {}

    failed_ids = {e['account_id'] for e in account_errors}
    seen_values = {}
    archived = 0
    try:
        for row in raw:
            mapped = _map_record(
                row,
                accounts_by_neos_id,
                skip_reasons,
            )

            if mapped is None:
                skipped += 1
                continue

            if mapped['account_id'] in failed_ids:
                continue  # keep the previous complete account snapshot on paging failure
            mapped['is_current'] = True
            seen_values.setdefault(mapped['account_id'], set()).add(mapped['neos_value_id'])
            existing = (
                db.query(NeosExpenseLine.id)
                .filter_by(neos_value_id=mapped["neos_value_id"])
                .first()
            )

            if _upsert_expense(db, mapped, now):
                imported += 1
            elif existing:
                updated += 1

        if skipped == 0:
            for account in accounts_with_neos_ids:
                if account.id in failed_ids: continue
                for line in db.query(NeosExpenseLine).filter_by(account_id=account.id, is_current=True).all():
                    if line.neos_value_id not in seen_values.get(account.id, set()):
                        line.is_current = False
                        line.matched_draw_id = None
                        line.match_source = None
                        archived += 1
        db.commit()

        result = reconcile_expenses(db)

        return {
            "fetched": len(raw),
            "archived": archived,
            "archival_skipped_invalid_rows": bool(skipped),
            "imported": imported,
            "updated": updated,
            "skipped_unmapped_or_invalid": skipped,
            "skip_reasons": skip_reasons,
            "account_errors": account_errors,
            "peaktrac_accounts_loaded": len(accounts),
            **result,
            "synced_at": now.isoformat() + "Z",
        }

    except Exception:
        db.rollback()
        raise


def reconciliation_report(db: Session) -> dict:
    """Return row-level reconciliation buckets for review."""
    expenses = (
        db.query(NeosExpenseLine)
        .filter(NeosExpenseLine.is_current.is_(True))
        .order_by(NeosExpenseLine.id.desc())
        .all()
    )
    draws = (
        db.query(Draw)
        .order_by(Draw.id.desc())
        .all()
    )

    draw_by_id = {draw.id: draw for draw in draws}

    linked = {
        expense.matched_draw_id
        for expense in expenses
        if expense.matched_draw_id is not None
    }

    neos_only = []
    matched_rows = []

    for expense in expenses:
        draw = (
            draw_by_id.get(expense.matched_draw_id)
            if expense.matched_draw_id is not None
            else None
        )

        item = {
            "neos_expense_id": expense.id,
            "neos_value_id": expense.neos_value_id,
            "account_id": expense.account_id,
            "amount": str(expense.amount),
            "code": expense.code,
            "approval_status": expense.approval_status,
            "matched_draw_id": expense.matched_draw_id,
        }

        if draw:
            item["draw_amount"] = str(draw.amount)
            matched_rows.append(item)
        else:
            neos_only.append(item)

    peaktrac_only = [
        {
            "draw_id": draw.id,
            "account_id": draw.account_id,
            "amount": str(draw.amount),
            "category": (
                getattr(draw, "expense_category", None)
                or getattr(draw, "category", None)
            ),
        }
        for draw in draws
        if (
            _norm(getattr(draw, "draw_status", None))
            in FUNDED_DRAW_STATUSES
            and draw.id not in linked
        )
    ]

    return {
        "matched": matched_rows,
        "neos_only": neos_only,
        "peaktrac_only": peaktrac_only,
    }


# ---------------------------------------------------------------- PT-7 support
class ExpenseLinkError(Exception):
    """A manual link/unlink was refused. The message is safe to show the user."""


def _f(value) -> float:
    return float(_money(value) or 0)


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _expense_item(expense: NeosExpenseLine, *, with_prefill: bool = False, db=None) -> dict:
    item = {
        "id": expense.id,
        "neos_value_id": expense.neos_value_id,
        "payee_name": expense.payee_name,
        "amount": _f(expense.amount),
        "code": expense.code,
        "code_type": expense.code_type,
        "service_date": _iso(expense.service_date),
        "due": _iso(expense.due),
        "approval_status": expense.approval_status,
        "reference": expense.reference,
        "matched_draw_id": expense.matched_draw_id,
        "match_source": getattr(expense, "match_source", None),
    }
    if with_prefill:
        ref = expense.reference or expense.neos_value_id
        item["prefill"] = {
            "payee": expense.payee_name or "",
            "amount": _f(expense.amount),
            "category": category_for_neos_code(expense.code, db),
            "neos_code": expense.code,
            "memo": f"From Neos expense {ref}",
        }
    return item


def _draw_item(draw: Draw) -> dict:
    when = _draw_date(draw)
    return {
        "id": draw.id,
        "payee": getattr(draw, "payee", None),
        "category": getattr(draw, "expense_category", None) or getattr(draw, "category", None),
        "amount": _f(draw.amount),
        "date": _iso(when),
        "status": draw.draw_status,
    }


def account_expense_report(db: Session, account_id: int) -> dict:
    expenses = (
        db.query(NeosExpenseLine)
        .filter(NeosExpenseLine.account_id == account_id, NeosExpenseLine.is_current.is_(True))
        .order_by(NeosExpenseLine.id.asc())
        .all()
    )
    funded = [
        d for d in db.query(Draw).filter(Draw.account_id == account_id).all()
        if _norm(getattr(d, "draw_status", None)) in FUNDED_DRAW_STATUSES
    ]
    draw_by_id = {d.id: d for d in funded}

    matched, neos_only = [], []
    used: set[int] = set()
    for e in expenses:
        draw = draw_by_id.get(e.matched_draw_id) if e.matched_draw_id is not None else None
        if draw is not None and draw.id not in used:
            used.add(draw.id)
            matched.append({"expense": _expense_item(e), "draw": _draw_item(draw),
                            "amount_difference": round(_f(e.amount) - _f(draw.amount), 2)})
        else:
            neos_only.append(_expense_item(e, with_prefill=True, db=db))
    peaktrac_only = [_draw_item(d) for d in funded if d.id not in used]

    m_neos = round(sum(m["expense"]["amount"] for m in matched), 2)
    m_draw = round(sum(m["draw"]["amount"] for m in matched), 2)
    n_total = round(sum(i["amount"] for i in neos_only), 2)
    p_total = round(sum(i["amount"] for i in peaktrac_only), 2)
    neos_total = round(m_neos + n_total, 2)
    funded_total = round(m_draw + p_total, 2)
    return {
        "account_id": account_id,
        "matched": matched,
        "neos_only": neos_only,
        "peaktrac_only": peaktrac_only,
        "totals": {
            "matched_count": len(matched), "matched_neos": m_neos, "matched_draws": m_draw,
            "neos_only_count": len(neos_only), "neos_only": n_total,
            "peaktrac_only_count": len(peaktrac_only), "peaktrac_only": p_total,
            "neos_due_to_firm": neos_total, "peaktrac_funded": funded_total,
            "difference": round(neos_total - funded_total, 2),
            "reconciles": abs(neos_total - funded_total) < 0.005,
        },
    }


def link_expense(db: Session, account_id: int, expense_id: int, draw_id: int) -> dict:
    expense = (db.query(NeosExpenseLine)
               .filter(NeosExpenseLine.id == expense_id,
                       NeosExpenseLine.account_id == account_id, NeosExpenseLine.is_current.is_(True)).first())
    if expense is None:
        raise ExpenseLinkError("Neos expense not found on this account.")
    draw = db.query(Draw).filter(Draw.id == draw_id, Draw.account_id == account_id).first()
    if draw is None:
        raise ExpenseLinkError("Draw not found on this account.")
    if _norm(draw.draw_status) not in FUNDED_DRAW_STATUSES:
        raise ExpenseLinkError("Only funded draws can be linked to a Neos expense.")
    other = (db.query(NeosExpenseLine.id)
             .filter(NeosExpenseLine.matched_draw_id == draw_id,
                     NeosExpenseLine.id != expense_id).first())
    if other:
        raise ExpenseLinkError("That draw is already linked to another Neos expense.")
    expense.matched_draw_id = draw_id
    expense.match_source = "manual"
    expense.manual_unlinked = False
    db.commit()
    return account_expense_report(db, account_id)


def unlink_expense(db: Session, account_id: int, expense_id: int) -> dict:
    expense = (db.query(NeosExpenseLine)
               .filter(NeosExpenseLine.id == expense_id,
                       NeosExpenseLine.account_id == account_id, NeosExpenseLine.is_current.is_(True)).first())
    if expense is None:
        raise ExpenseLinkError("Neos expense not found on this account.")
    if expense.matched_draw_id is None:
        raise ExpenseLinkError("This expense is not linked.")
    expense.matched_draw_id = None
    expense.match_source = None
    expense.manual_unlinked = True       # stop the nightly run from re-linking it
    db.commit()
    return account_expense_report(db, account_id)


def accounts_with_unmatched_expenses(db: Session) -> dict:
    """Overview Action Queue: how many accounts have any mismatch, and how many items."""
    flagged: dict[int, int] = {}
    for e in db.query(NeosExpenseLine).filter(NeosExpenseLine.is_current.is_(True), NeosExpenseLine.matched_draw_id.is_(None)).all():
        flagged[e.account_id] = flagged.get(e.account_id, 0) + 1
    linked = {x[0] for x in db.query(NeosExpenseLine.matched_draw_id)
              .filter(NeosExpenseLine.is_current.is_(True), NeosExpenseLine.matched_draw_id.isnot(None)).all()}
    for d in db.query(Draw).all():
        if _norm(getattr(d, "draw_status", None)) in FUNDED_DRAW_STATUSES and d.id not in linked:
            flagged[d.account_id] = flagged.get(d.account_id, 0) + 1
    return {"accounts": len(flagged), "items": sum(flagged.values()), "by_account": flagged}