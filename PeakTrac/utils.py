from datetime import date, datetime
from fastapi import HTTPException
from db import Session
from decimal import Decimal
from PeakTrac.neos_session import get_neos_client
from PeakTrac.neos_details import fetch_case_details
from db import PeakTracAccount, Session, get_db
DEFAULT_CREDIT_LIMIT = 1500
CASE_INTEREST_RATE_PCT = 7.75   
import json
import os
CASE_TYPE_CREDIT_LIMITS: dict[str, int] = json.loads(os.getenv('PEAKTRAC_CASE_TYPE_LIMITS_JSON', '{}'))
if not isinstance(CASE_TYPE_CREDIT_LIMITS, dict) or any(
    isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0
    for v in CASE_TYPE_CREDIT_LIMITS.values()
):
    raise ValueError('PEAKTRAC_CASE_TYPE_LIMITS_JSON must map case codes to positive limits.')


def credit_limit_for(case_type: str | None) -> int:
    return CASE_TYPE_CREDIT_LIMITS.get(case_type or "", DEFAULT_CREDIT_LIMIT)


def apply_neos_details(account, d: dict) -> None:
    """Copy Neos-derived fields onto an account. Never touches money fields."""
    account.client_name = d["client_name"] or account.client_name
    account.case_number = d["case_number"]
    account.case_type = d["case_type_code"]
    account.case_status = d["case_status"]
    account.case_phase = d["phase"]
    account.attorney_name = d["lead_attorney_name"]
    account.attorney_id = d["lead_attorney_staff_id"]
    account.primary_staff_code = d["primary_staff_code"]
    account.office = d["office"]
    account.date_of_incident = d["incident_date"]
    account.date_opened = d["date_opened"]
    account.sol_date = d["sol_date"]
    account.policy_limit_max = d["policy_limit_max"]
    account.first_party_is_our_client = d["first_party_is_our_client"]
    account.neos_synced_at = d["synced_at"]
    limit = Decimal(str(account.credit_limit or 0))
    account.limit_above_policy = bool(d["policy_limit_max"] is not None and limit > d["policy_limit_max"])


def _parse_neos_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def _parse_neos_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
 
 
async def peaktrac_account(
    db: Session,
    neos_case_number: int,
    rpc_1_5_disclosure: bool = False,
    first_name: str | None = None,   # no longer needed; kept so old callers don't break
    last_name: str | None = None,
):
    existing = (
        db.query(PeakTracAccount)
        .filter(PeakTracAccount.case_number == str(neos_case_number))
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail="Case already imported.")

    if rpc_1_5_disclosure is not True:
        raise HTTPException(
            status_code=400,
            detail="RPC 1.5 interest-disclosure confirmation is required before import.",
        )

    details = await fetch_case_details(get_neos_client(), neos_case_number)
    if not details:
        raise HTTPException(status_code=404, detail="Case not found in Neos.")

    if details["is_closed"]:
        raise HTTPException(status_code=400, detail="This case is already closed.")

    # Same rule as before. Only enforced when Neos actually answers the question.
    if details["first_party_is_our_client"] is False:
        raise HTTPException(
            status_code=400,
            detail="Only retained cases where the first party is our client can be imported.",
        )

    existing_by_id = (
        db.query(PeakTracAccount)
        .filter(PeakTracAccount.neos_case_id == details["neos_case_id"])
        .first()
    )
    if existing_by_id:
        raise HTTPException(status_code=400, detail="Case already imported.")

    limit = credit_limit_for(details["case_type_code"])
    account = PeakTracAccount(
        neos_case_id=details["neos_case_id"],
        credit_limit=limit,
        available_credit=limit,
        principal=0,
        accrued_interest=0,
        payoff_amount=0,
        utilization_percent=0,
        rpc_1_5_disclosure=rpc_1_5_disclosure,
        interest_rate=CASE_INTEREST_RATE_PCT,
        total_draws=0,
        account_status="Pending Approval",
    )
    apply_neos_details(account, details)

    try:
        db.add(account)
        db.commit()
        db.refresh(account)
        return account
    except Exception:
        db.rollback()
        raise


async def refresh_account_from_neos(db: Session, account) -> dict:
    details = await fetch_case_details(get_neos_client(), account.case_number)
    if not details:
        raise HTTPException(status_code=404, detail="Case not found in Neos.")
    apply_neos_details(account, details)
    try:
        db.commit()
        db.refresh(account)
    except Exception:
        db.rollback()
        raise
    return details