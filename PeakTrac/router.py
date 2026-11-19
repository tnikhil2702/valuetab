from fastapi import APIRouter, HTTPException, Query, Body, Depends, Request
from datetime import datetime
from collections import defaultdict
import csv
import io
import os

from PeakTrac.neos_client import NeosAuthError, NeosRequestError
from PeakTrac.neos_session import get_neos_client
from PeakTrac.neos_cases import get_open_cases, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from PeakTrac.neos_expenses import (
    sync_neos_expenses, reconciliation_report, reconcile_expenses,
    account_expense_report, link_expense, unlink_expense,
    accounts_with_unmatched_expenses, ExpenseLinkError,
)
from PeakTrac.neos_watcher import run_watcher
from PeakTrac.neos_api import finance
import logging
from PeakTrac.utils import peaktrac_account, refresh_account_from_neos, Session, get_db
from PeakTrac.interest import (
    accrued_interest,
    payoff_quote,
    current_case_rate_pct,
    current_firm_operating_rate_pct,
)
from db import PeakTracAccount, Draw, FirmOperatingDraw, NeosExpenseLine
from decimal import Decimal, InvalidOperation
import calendar
from db import get_db,  Session, ActiveSession,PeakTracHelpReport,Whitelist
from auth_utils import authenticate_user, create_access_token, get_current_user, ACCESS_TOKEN_EXPIRE_MINUTES, authenticate_editor, create_editor_token, get_current_editor, reset_password, generate_otp, verify_otp, SessionLocal, create_password, EDITOR_ACCESS_TOKEN_EXPIRE_MINUTES

from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime, timedelta
from PeakTrac.help_assistant import (
    process_history,
    run_chat_turn,
    user_email,
)
from pydantic import BaseModel, Field
import re
import time
from collections import defaultdict, deque


PROJECT_NAME = "Peaktrac"
router = APIRouter()
jinja_templates = Jinja2Templates(directory="PeakTrac/templates")


FIRM_LOC_CONFIG = {
    "facility_limit": 2_000_000,  
}

FIRM_LOC_CONFIG_EXTRA = {
    "facility_maturity": None,  
}

FACILITY_LIMIT = 2_000_000  # confirmed elsewhere in the router (FIRM_LOC_CONFIG)


def _simple_interest(principal: float, rate_pct: float, days: float) -> float:
    """Matches the 'Simple · actual/365' label shown in the Overview UI."""
    if days <= 0 or not principal or not rate_pct:
        return 0.0
    return principal * (rate_pct / 100) * (days / 365)
 
 
def _month_end_dates(months: int, anchor: datetime) -> list[tuple[str, datetime]]:
    """
    Returns `months` (label, month_end_datetime) pairs, oldest first,
    ending with the anchor month. Labels match the mock: 'Aug 25', 'Sep 25', ...
    """
    out = []
    year, month = anchor.year, anchor.month
    for i in range(months - 1, -1, -1):
        m = month - i
        y = year
        while m <= 0:
            m += 12
            y -= 1
        last_day = calendar.monthrange(y, m)[1]
        month_end = datetime(y, m, last_day, 23, 59, 59)
        label = f"{calendar.month_abbr[m]} {str(y)[2:]}"
        out.append((label, month_end))
    return out

# API Endpoints
@router.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    return jinja_templates.get_template("index.html").render({"request": request})

@router.post("/login")
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    user = await authenticate_user(
        form_data.username,
        form_data.password
    )

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password"
        )

    access_token = await create_access_token(
        data={
            "sub": user["email"],
            "role": user["role"]
        },
        expires_delta=timedelta(
            minutes=ACCESS_TOKEN_EXPIRE_MINUTES
        )
    )

    active = db.query(ActiveSession).filter(
        ActiveSession.project_name == PROJECT_NAME,
        ActiveSession.user_email == user["email"]
    ).first()

    if active:

        active.last_activity_timestamp = datetime.utcnow()
    else:
        db.add(
            ActiveSession(
                project_name=PROJECT_NAME,
                user_email=user["email"],
                last_activity_timestamp=datetime.utcnow()
            )
        )

    db.commit()
    return {
        "access_token": access_token,
        "token_type": "bearer"
    }

@router.get("/verify-email")
async def verify_email(email: str):

    db = SessionLocal()

    try:

        user = db.query(Whitelist).filter(
            Whitelist.email == email.lower()
        ).first()

        if not user:
            raise HTTPException(
                status_code=404,
                detail="Email not authorized"
            )

        return {
            "valid": True,
            "password_created": bool(user.password_hash),
            "role": user.role
        }

    finally:
        db.close()

@router.post("/send/otp")
async def send_otp_api(email: str):
    return await generate_otp(email)

@router.post("/verify/otp")
async def verify_otp_api(
    email: str,
    otp: str
):
    await verify_otp(email, otp)
    return {
        "message": "OTP verified successfully"
    }

@router.post("/create/password")
async def create_password_api(
    email: str,
    password: str
):
    return await create_password(
        email,
        password
    )

@router.post("/forgot/password")
async def forgot_password(email: str):
    return await generate_otp(email)

@router.post("/reset/password")
async def reset_password_api(
    email: str,
    otp: str,
    new_password: str
):
    return await reset_password(
        email,
        otp,
        new_password
    )
 
@router.get("/overview/summary")
async def overview_summary(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    try:
        now = datetime.utcnow()
 
        active_accounts = (
            db.query(PeakTracAccount)
            .filter(PeakTracAccount.account_status == "Active")
            .all()
        )
        active_ids = {a.id for a in active_accounts}
 
        account_calcs = []  
        for a in active_accounts:
            interest, payoff, funded = _compute_live_interest(a, db)
            account_calcs.append((a, interest, payoff, funded))
 
        outstanding_principal = sum(float(a.principal or 0) for a in active_accounts)
        accrued_interest_total = sum(interest for _, interest, _, _ in account_calcs)
 
        firm_funded_draws = (
            db.query(FirmOperatingDraw)
            .filter(FirmOperatingDraw.status == "Funded")
            .all()
        )
        firm_drawn = sum(float(d.amount or 0) for d in firm_funded_draws)
        case_drawn = outstanding_principal
        total_drawn = case_drawn + firm_drawn
        facility_available = round(FACILITY_LIMIT - total_drawn, 2)
 
        active_accounts_count = len(active_accounts)
        paid_off_accounts_ytd = None  # see module docstring — no schema support yet
 
        pending_draws = (
            db.query(Draw).filter(Draw.draw_status == "Pending Approval").all()
        )
        pending_accounts = (
            db.query(PeakTracAccount)
            .filter(PeakTracAccount.account_status == "Pending Approval")
            .all()
        )
        pending_accounts_with_pending_draws = {d.account_id for d in pending_draws}
        pending_actions_total = len(pending_draws) + len(pending_accounts)
 
        all_funded_draws = (
            db.query(Draw)
            .filter(Draw.draw_status == "Funded", Draw.account_id.in_(active_ids))
            .all()
        ) if active_ids else []
 
        rate_by_account = {
            a.id: float(a.interest_rate or current_case_rate_pct()) for a in active_accounts
        }
 
        trend = []
        for label, month_end in _month_end_dates(12, now):
            principal_as_of = 0.0
            interest_as_of = 0.0
            for d in all_funded_draws:
                if not d.funded_at or d.funded_at > month_end:
                    continue
                amount = float(d.amount or 0)
                principal_as_of += amount
                days = (month_end - d.funded_at).days
                rate = rate_by_account.get(d.account_id, current_case_rate_pct())
                interest_as_of += _simple_interest(amount, rate, days)
 
            trend.append(
                {
                    "month": label,
                    "principal": round(principal_as_of, 2),
                    "accrued_interest": round(interest_as_of, 2),
                    "total_balance": round(principal_as_of + interest_as_of, 2),
                }
            )

        category_totals = defaultdict(float)
        for d in all_funded_draws:
            category_totals[d.category or "Other"] += float(d.amount or 0)
 
        draws_by_category = [
            {"category": k, "amount": round(v, 2)}
            for k, v in sorted(category_totals.items(), key=lambda kv: -kv[1])
        ]
 
        ranked = sorted(account_calcs, key=lambda t: -t[2])
        largest_open_balances = [
            {
                "case_number": a.case_number,
                "client_name": a.client_name,
                "principal": round(float(a.principal or 0), 2),
                "accrued_interest": round(interest, 2),
                "payoff_amount": round(payoff, 2),
                "utilization_percent": float(a.utilization_percent or 0),
            }
            for a, interest, payoff, _ in ranked
        ]
 
        action_queue = []
 
        for acc in pending_accounts:
            action_queue.append(
                {
                    "type": "account_approval",
                    "client_name": acc.client_name,
                    "status": "Pending Approval",
                    "detail": (
                        f"New account · {acc.case_number} · "
                        f"limit ${float(acc.credit_limit or 0):,.0f}"
                    ),
                    "account_id": acc.id,
                    "case_number": acc.case_number,
                }
            )
 
        account_cache = {a.id: a for a in active_accounts}
        for d in pending_draws:
            acc = account_cache.get(d.account_id) or (
                db.query(PeakTracAccount)
                .filter(PeakTracAccount.id == d.account_id)
                .first()
            )
            action_queue.append(
                {
                    "type": "draw_approval",
                    "client_name": acc.client_name if acc else None,
                    "status": "Pending Approval",
                    "detail": (
                        f"Draw request · {d.category} · ${float(d.amount or 0):,.0f}"
                    ),
                    "draw_id": d.id,
                    "account_id": d.account_id,
                    "case_number": acc.case_number if acc else None,
                    "requested_at": d.requested_at.isoformat() if d.requested_at else None,
                }
            )
 
        try:
            unmatched = accounts_with_unmatched_expenses(db)
            if unmatched["accounts"]:
                n = unmatched["accounts"]
                action_queue.append(
                    {
                        "type": "unmatched_neos_expenses",
                        "client_name": f"{n} account{'s' if n != 1 else ''}",
                        "status": "Review",
                        "detail": (
                            f"{n} account{'s have' if n != 1 else ' has'} unmatched Neos expenses "
                            f"({unmatched['items']} item{'s' if unmatched['items'] != 1 else ''})"
                        ),
                        "account_id": None,
                        "case_number": None,
                    }
                )
        except Exception:
            logging.getLogger("peaktrac.neos.expenses").exception("Unmatched expense count failed")

        action_queue.sort(
            key=lambda item: item.get("requested_at") or "", reverse=True
        )
 
        return {
            "success": True,
            "as_of": now.date().isoformat(),
            "case_rate_pct": current_case_rate_pct(),
            "cards": {
                "outstanding_principal": round(outstanding_principal, 2),
                "open_accounts": active_accounts_count,
                "accrued_interest": round(accrued_interest_total, 2),
                "facility_available": facility_available,
                "facility_limit": FACILITY_LIMIT,
                "case_drawn": round(case_drawn, 2),
                "firm_drawn": round(firm_drawn, 2),
                "active_accounts": active_accounts_count,
                "paid_off_accounts_ytd": paid_off_accounts_ytd,
                "pending_actions_total": pending_actions_total,
                "pending_draws_count": len(pending_draws),
                "pending_accounts_count": len(pending_accounts),
            },
            "outstanding_balance_trend": trend,
            "draws_by_category": draws_by_category,
            "largest_open_balances": largest_open_balances,
            "action_queue": action_queue,
        }
 
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    

# OPEN NEOS CASES (PT-2): searchable, paged, open cases only.
# GET /peaktrac/cases/neos?q=<case number or last name>&page=1&page_size=25
@router.get("/cases/neos")
async def get_neos_cases(
    q: str | None = Query(None, max_length=60),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    def imported_lookup(case_numbers):
        numbers = [n for n in case_numbers if n]
        if not numbers:
            return set()
        rows = (
            db.query(PeakTracAccount.case_number)
            .filter(PeakTracAccount.case_number.in_(numbers))
            .all()
        )
        return {r[0] for r in rows}

    try:
        return await get_open_cases(
            get_neos_client(), imported_lookup, q=q, page=page, page_size=page_size
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except NeosAuthError:
        logging.getLogger("peaktrac.neos").exception("Neos auth failed")
        raise HTTPException(status_code=503, detail="Neos sign-in is unavailable. Try again shortly.")
    except NeosRequestError as e:
        raise HTTPException(status_code=502, detail=f"Neos did not respond correctly (status {e.status}).")


# Backward-compatible URL for the existing UI.
@router.get("/intakes", deprecated=True)
async def get_intakes_compat(
    q: str | None = Query(None, max_length=60),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return await get_neos_cases(q=q, page=page, page_size=page_size, db=db, current_user=current_user)


# PT-6: Neos expense sync and draw reconciliation. These routes require login.
# Run POST manually during validation; schedule the same service function with a
# protected worker/cron after validating the tenant's actual Neos payload fields.
@router.post("/neos/expenses/sync", dependencies=[Depends(finance)])
async def sync_neos_expenses_endpoint(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        return await sync_neos_expenses(db, get_neos_client())
    except NeosAuthError:
        logging.getLogger("peaktrac.neos").exception("Neos expense sync auth failed")
        raise HTTPException(status_code=503, detail="Neos sign-in is unavailable.")
    except NeosRequestError as exc:
        raise HTTPException(status_code=502, detail=f"Neos expense request failed (status {exc.status}).")
    except Exception:
        db.rollback()
        logging.getLogger("peaktrac.neos.expenses").exception("Neos expense sync failed")
        raise HTTPException(status_code=500, detail="Neos expense sync failed. Check server logs.")


@router.get("/neos/expenses/reconciliation")
async def neos_expense_reconciliation_endpoint(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        return reconciliation_report(db)
    except Exception:
        logging.getLogger("peaktrac.neos.expenses").exception("Reconciliation report failed")
        raise HTTPException(status_code=500, detail="Could not build reconciliation report.")


@router.post("/neos/expenses/reconcile", dependencies=[Depends(finance)])
async def reconcile_neos_expenses_endpoint(
    dry_run: bool = Query(True, description="True previews matches without saving links"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        return reconcile_expenses(db, dry_run=dry_run)
    except Exception:
        db.rollback()
        logging.getLogger("peaktrac.neos.expenses").exception("Expense reconciliation failed")
        raise HTTPException(status_code=500, detail="Expense reconciliation failed. Check server logs.")


def _actor(current_user) -> str:
    if isinstance(current_user, dict):
        return str(current_user.get("email") or current_user.get("username") or current_user.get("sub") or "unknown")
    return str(getattr(current_user, "email", None) or getattr(current_user, "username", None) or "unknown")


def _account_or_404(db: Session, account_id: int) -> PeakTracAccount:
    account = db.query(PeakTracAccount).filter(PeakTracAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found.")
    return account


# PT-7: per-account "Expenses vs Neos". Reads our own tables only (no Neos call).
@router.get("/accounts/{account_id}/neos-expenses")
async def get_account_neos_expenses(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _account_or_404(db, account_id)
    return {"success": True, **account_expense_report(db, account_id)}


# Refresh button: pull this account's Neos expenses now, then re-match.
@router.post("/accounts/{account_id}/neos-expenses/sync")
async def sync_account_neos_expenses(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _account_or_404(db, account_id)
    try:
        result = await sync_neos_expenses(db, get_neos_client(), account_ids={account_id})
    except NeosAuthError:
        raise HTTPException(status_code=503, detail="Neos sign-in is unavailable. Try again shortly.")
    except NeosRequestError as exc:
        raise HTTPException(status_code=502, detail=f"Neos expense request failed (status {exc.status}).")
    except Exception:
        db.rollback()
        logging.getLogger("peaktrac.neos.expenses").exception("Account expense sync failed")
        raise HTTPException(status_code=500, detail="Neos expense sync failed. Check server logs.")
    return {"success": True, "sync": result, **account_expense_report(db, account_id)}


@router.post("/accounts/{account_id}/neos-expenses/{expense_id}/link", dependencies=[Depends(finance)])
async def link_account_neos_expense(
    account_id: int,
    expense_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _account_or_404(db, account_id)
    try:
        draw_id = int(body.get("draw_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="draw_id is required.")
    try:
        report = link_expense(db, account_id, expense_id, draw_id)
    except ExpenseLinkError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    logging.getLogger("peaktrac.audit").info(
        "neos_expense_link account=%s expense=%s draw=%s by=%s", account_id, expense_id, draw_id, _actor(current_user))
    return {"success": True, **report}


@router.post("/accounts/{account_id}/neos-expenses/{expense_id}/unlink", dependencies=[Depends(finance)])
async def unlink_account_neos_expense(
    account_id: int,
    expense_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _account_or_404(db, account_id)
    try:
        report = unlink_expense(db, account_id, expense_id)
    except ExpenseLinkError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    logging.getLogger("peaktrac.audit").info(
        "neos_expense_unlink account=%s expense=%s by=%s", account_id, expense_id, _actor(current_user))
    return {"success": True, **report}


# PT-9: run the settlement / closed-case watcher now (the nightly job runs the same function).
@router.post("/neos/watcher/run", dependencies=[Depends(finance)])
async def run_neos_watcher_endpoint(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        return {"success": True, **(await run_watcher(db, get_neos_client()))}
    except NeosAuthError:
        raise HTTPException(status_code=503, detail="Neos sign-in is unavailable. Try again shortly.")
    except NeosRequestError as exc:
        raise HTTPException(status_code=502, detail=f"Neos request failed (status {exc.status}).")
    except Exception:
        db.rollback()
        logging.getLogger("peaktrac.neos.watcher").exception("Watcher run failed")
        raise HTTPException(status_code=500, detail="Watcher run failed. Check server logs.")


# ACCOUNT IMPORT & APPROVAL
@router.post("/accounts/import")
async def import_account(
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    try:
        # The browser sends only the case number (+ the RPC 1.5 flag).
        # Everything else is filled in on the server from Neos (PT-4).
        neos_case_number = body.get("neos_case_number")
        rpc_1_5_disclosure = body.get("rpc_1_5_disclosure", False)

        if not neos_case_number:
            raise HTTPException(status_code=400, detail="neos_case_number is required.")
        try:
            neos_case_number = int(neos_case_number)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="neos_case_number must be a number.")

        account = await peaktrac_account(
            db=db,
            neos_case_number=neos_case_number,
            rpc_1_5_disclosure=rpc_1_5_disclosure,
        )

        return {
            "success": True,
            "message": "Account imported successfully.",
            "data": {
                "id": account.id,
                "client_name": account.client_name,
                "case_type": account.case_type,
                "credit_limit": account.credit_limit,
                "interest_rate": account.interest_rate,
                "status": account.account_status,
                "limit_above_policy": account.limit_above_policy,
            },
        }

    except HTTPException:
        raise

    except NeosAuthError:
        raise HTTPException(status_code=503, detail="Neos sign-in is unavailable. Try again shortly.")

    except NeosRequestError as e:
        raise HTTPException(status_code=502, detail=f"Neos did not respond correctly (status {e.status}).")

    except Exception:
        logging.getLogger("peaktrac").exception("Account import failed")
        raise HTTPException(status_code=500, detail="Import failed. Check the server log.")


@router.post("/accounts/{account_id}/neos-refresh")
async def neos_refresh_account(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    account = db.query(PeakTracAccount).filter(PeakTracAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found.")
    try:
        details = await refresh_account_from_neos(db, account)
    except NeosAuthError:
        raise HTTPException(status_code=503, detail="Neos sign-in is unavailable. Try again shortly.")
    except NeosRequestError as e:
        raise HTTPException(status_code=502, detail=f"Neos did not respond correctly (status {e.status}).")
    return {
        "success": True,
        "data": _account_detail_dict(account, db),
        "null_reasons": details["null_reasons"],
    }


@router.get("/accounts")
async def list_accounts(
    status: str | None = Query(
        None,
        description="Filter by account_status, e.g. 'Pending Approval', 'Active'.",
    ),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        q = db.query(PeakTracAccount)

        if status:
            q = q.filter(
                PeakTracAccount.account_status == status
            )

        accounts = (
            q.order_by(
                PeakTracAccount.id.desc()
            )
            .all()
        )

        return {
            "total": len(accounts),
            "data": [
                _account_summary_dict(a, db)
                for a in accounts
            ],
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


@router.get("/accounts/{account_id}")
async def get_account(account_id: int, db: Session = Depends(get_db),current_user: dict = Depends(get_current_user),):
    try:
        account = (
            db.query(PeakTracAccount)
            .filter(PeakTracAccount.id == account_id)
            .first()
        )
        if not account:
            raise HTTPException(status_code=404, detail="Account not found.")

        return {"success": True, "data": _account_detail_dict(account, db)}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/accounts/{account_id}/approve", dependencies=[Depends(finance)])
async def approve_account(
    account_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        approved_by = body.get("approved_by")
        if not approved_by:
            raise HTTPException(status_code=400, detail="approved_by is required.")

        account = (
            db.query(PeakTracAccount)
            .filter(PeakTracAccount.id == account_id)
            .first()
        )
        if not account:
            raise HTTPException(status_code=404, detail="Account not found.")

        if account.account_status != "Pending Approval":
            raise HTTPException(
                status_code=400,
                detail="Only Pending Approval accounts can be approved.",
            )

        account.account_status = "Active"
        account.approved_by = approved_by
        account.approved_at = datetime.utcnow()

        db.commit()
        db.refresh(account)

        return {
            "success": True,
            "message": "Account approved successfully.",
            "data": {
                "id": account.id,
                "case_number": account.case_number,
                "client_name": account.client_name,
                "case_type": account.case_type,
                "account_status": account.account_status,
                "interest_rate": account.interest_rate,
                "approved_by": account.approved_by,
                "approved_at": account.approved_at,
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/accounts/{account_id}/draws")
async def request_draw(
    account_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        payee = body.get("payee")
        category = body.get("category")
        amount = body.get("amount")
        memo = body.get("memo", "")
        requested_by = body.get("requested_by")

        # The category is a Neos expense code picked from the dropdown.
        neos_code = str(body.get("neos_code") or "").strip() or None
        if neos_code:
            from PeakTrac.neos_categories import expense_code_options
            try:
                options = await expense_code_options(get_neos_client())
            except NeosAuthError:
                raise HTTPException(503, "Neos sign-in is unavailable. Try again shortly.")
            except (NeosRequestError, ValueError):
                raise HTTPException(502, "Expense categories are unavailable from Neos right now. Try again shortly.")
            chosen = next((o for o in options if o["code"].casefold() == neos_code.casefold()), None)
            if chosen is None:
                raise HTTPException(400, "Choose a valid expense category from the list.")
            neos_code = chosen["code"]
            category = chosen["description"] or chosen["code"]

        if not all([payee, category, amount, requested_by]):
            raise HTTPException(
                status_code=400,
                detail="payee, category, amount, and requested_by are required.",
            )

        if not neos_code:      # older clients that still send a free-text category
            from PeakTrac.category_map import neos_code_for_category
            neos_code = neos_code_for_category(category, db)
            if not neos_code:
                raise HTTPException(409, "Choose an expense category from the list.")

        if amount <= 0:
            raise HTTPException(
                status_code=400,
                detail="amount must be positive.",
            )

        account = (
            db.query(PeakTracAccount)
            .filter(PeakTracAccount.id == account_id)
            .first()
        )

        if not account:
            raise HTTPException(
                status_code=404,
                detail="Account not found.",
            )

        if amount > (account.available_credit or 0):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Requested amount ${amount:,.2f} exceeds available credit "
                    f"${account.available_credit:,.2f}."
                ),
            )

        draw = Draw(
            neos_code=neos_code,
            account_id=account.id,
            payee=payee,
            category=category,

            # Database requires this column
            expense_category=category,

            amount=amount,
            memo=memo,
            requested_by=requested_by,
            requested_at=datetime.utcnow(),

            # Database column is draw_status, NOT status
            draw_status="Pending Approval",
        )

        db.add(draw)
        db.commit()
        db.refresh(draw)

        return {
            "success": True,
            "message": "Draw requested. Awaiting attorney approval.",
            "data": _draw_dict(draw),
        }

    except HTTPException:
        raise

    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=str(e),
        )

@router.get("/draws/pending")
async def list_pending_draws(db: Session = Depends(get_db),current_user: dict = Depends(get_current_user)):
    try:
        draws = (
            db.query(Draw)
            .filter(Draw.draw_status == "Pending Approval")
            .order_by(Draw.requested_at.asc())
            .all()
        )

        return {"total": len(draws), "data": [_draw_dict(d) for d in draws]}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/draws")
async def list_all_draws(db: Session = Depends(get_db),current_user: dict = Depends(get_current_user)):
    try:
        draws = (
            db.query(Draw)
            .order_by(Draw.requested_at.desc())
            .all()
        )

        return {
            "total": len(draws),
            "data": [_draw_dict(d) for d in draws],
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Full draw history for ONE account (all statuses)
@router.get("/accounts/{account_id}/draws")
async def list_account_draws(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    try:
        account = (
            db.query(PeakTracAccount)
            .filter(PeakTracAccount.id == account_id)
            .first()
        )

        if not account:
            raise HTTPException(
                status_code=404,
                detail="Account not found."
            )

        draws = (
            db.query(Draw)
            .filter(Draw.account_id == account_id)
            .order_by(Draw.requested_at.desc())
            .all()
        )

        return {
            "total": len(draws),
            "data": [_draw_dict(d) for d in draws],
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/draws/{draw_id}/approve", dependencies=[Depends(finance)])
async def approve_draw(
    draw_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    try:
        approved_by = body.get("approved_by")
        if not approved_by:
            raise HTTPException(status_code=400, detail="approved_by is required.")

        draw = db.query(Draw).filter(Draw.id == draw_id).first()
        if not draw:
            raise HTTPException(status_code=404, detail="Draw not found.")

        if draw.draw_status != "Pending Approval":
            raise HTTPException(
                status_code=400,
                detail="Only Pending Approval draws can be approved.",
            )

        if (draw.requested_by or "").strip().casefold() == approved_by.strip().casefold():
            raise HTTPException(
                status_code=403,
                detail="Dual authorization required: requester cannot approve the same draw.",
            )

        account = (
            db.query(PeakTracAccount)
            .filter(PeakTracAccount.id == draw.account_id)
            .with_for_update()
            .first()
        )
        if not account:
            raise HTTPException(status_code=404, detail="Associated account not found.")

        if draw.amount > (account.available_credit or 0):
            raise HTTPException(
                status_code=400,
                detail="Available credit has changed and no longer covers this draw.",
            )

        now = datetime.utcnow()
        draw.draw_status = "Funded"
        draw.approved_by = approved_by
        draw.approved_at = now
        draw.funded_at = now  # funds same-day, per walkthrough

        account.available_credit = (account.available_credit or 0) - draw.amount
        account.principal = (account.principal or 0) + draw.amount
        account.total_draws = (account.total_draws or 0) + 1
        account.utilization_percent = (
            round((account.principal / account.credit_limit) * 100, 2)
            if account.credit_limit
            else 0
        )

        db.commit()
        db.refresh(draw)
        db.refresh(account)

        return {
            "success": True,
            "message": "Draw approved and funded successfully.",
            "data": _draw_dict(draw),
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/draws/{draw_id}/deny", dependencies=[Depends(finance)])
async def deny_draw(
    draw_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    try:
        denied_by = body.get("denied_by")
        reason = body.get("reason", "")

        if not denied_by:
            raise HTTPException(
                status_code=400,
                detail="denied_by is required."
            )

        draw = (
            db.query(Draw)
            .filter(Draw.id == draw_id)
            .first()
        )

        if not draw:
            raise HTTPException(
                status_code=404,
                detail="Draw not found."
            )

        if draw.draw_status != "Pending Approval":
            raise HTTPException(
                status_code=400,
                detail="Only Pending Approval draws can be denied."
            )

        draw.draw_status = "Denied"
        draw.denied_reason = reason

        db.commit()
        db.refresh(draw)

        return {
            "success": True,
            "message": "Draw denied. Returned to requester with memo intact.",
            "data": _draw_dict(draw),
        }

    except HTTPException:
        raise

    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


# LEDGER & PAYOFF
@router.get("/accounts/{account_id}/ledger")
async def get_ledger(account_id: int, db: Session = Depends(get_db),current_user: dict = Depends(get_current_user)):
    try:
        account = (
            db.query(PeakTracAccount)
            .filter(PeakTracAccount.id == account_id)
            .first()
        )

        if not account:
            raise HTTPException(
                status_code=404,
                detail="Account not found."
            )

        draws = (
            db.query(Draw)
            .filter(
                Draw.account_id == account_id,
                Draw.draw_status == "Funded"
            )
            .order_by(Draw.funded_at.asc())
            .all()
        )

        rate = account.interest_rate or current_case_rate_pct()

        ledger = []

        total_principal = Decimal("0")
        total_interest = Decimal("0")

        for d in draws:

            interest = accrued_interest(
                d.amount,
                rate,
                d.funded_at
            )

            interest = Decimal(str(interest))

            total_principal += d.amount
            total_interest += interest

            ledger.append(
                {
                    "draw_id": d.id,
                    "payee": d.payee,
                    "category": d.category,
                    "memo": d.memo,
                    "amount": float(d.amount),
                    "funded_at": (
                        d.funded_at.isoformat()
                        if d.funded_at
                        else None
                    ),
                    "interest_accrued": float(interest),
                }
            )

        total_balance = total_principal + total_interest

        return {
            "account_id": account_id,
            "client_name": account.client_name,
            "rate_pct": float(rate),
            "total_draws": len(draws),
            "principal": float(total_principal),
            "accrued_interest": float(total_interest),
            "balance": float(total_balance),
            "ledger": ledger,
        }

    except HTTPException:
        raise

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@router.get("/accounts/{account_id}/payoff")
async def get_payoff_quote(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    try:
        account = (
            db.query(PeakTracAccount)
            .filter(PeakTracAccount.id == account_id)
            .first()
        )

        if not account:
            raise HTTPException(
                status_code=404,
                detail="Account not found."
            )

        draws = (
            db.query(Draw)
            .filter(
                Draw.account_id == account_id,
                Draw.draw_status == "Funded"
            )
            .all()
        )

        rate = float(
            account.interest_rate or current_case_rate_pct()
        )

        funded_draws = [
            {
                "amount": float(d.amount or 0),
                "funded_at": d.funded_at,
            }
            for d in draws
        ]

        quote = payoff_quote(
            funded_draws,
            rate
        )

        quote["account_id"] = account_id
        quote["client_name"] = account.client_name
        quote["case_number"] = account.case_number

        return {
            "success": True,
            "data": quote
        }

    except HTTPException:
        raise

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


# FIRM LINE OF CREDIT
@router.get("/firm/line-of-credit")
async def firm_line_of_credit(db: Session = Depends(get_db),current_user: dict = Depends(get_current_user)):
    try:
        facility_limit = FIRM_LOC_CONFIG["facility_limit"]

        active_accounts = (
            db.query(PeakTracAccount)
            .filter(
                PeakTracAccount.account_status == "Active"
            )
            .all()
        )

        client_drawn = sum(
            float(a.principal or 0)
            for a in active_accounts
        )

        open_cases = len(active_accounts)

        firm_draws = (
            db.query(FirmOperatingDraw)
            .filter(
                FirmOperatingDraw.status == "Funded"
            )
            .all()
        )

        firm_drawn = sum(
            float(d.amount or 0)
            for d in firm_draws
        )

        total_drawn = round(
            client_drawn + firm_drawn,
            2
        )

        utilization_pct = (
            round(
                (total_drawn / facility_limit) * 100,
                2
            )
            if facility_limit
            else 0
        )

        headroom = round(
            facility_limit - total_drawn,
            2
        )

        funded_case_draws = (
            db.query(Draw)
            .filter(Draw.draw_status == "Funded", Draw.funded_at.isnot(None))
            .all()
        )
        now = datetime.utcnow()
        wal_denominator = sum(float(d.amount or 0) for d in funded_case_draws)
        weighted_days = sum(
            float(d.amount or 0) * max((now - d.funded_at).days, 0)
            for d in funded_case_draws
        )
        wal_days = round(weighted_days / wal_denominator, 1) if wal_denominator else 0.0

        return {
            "facility_limit": facility_limit,
            "total_drawn": total_drawn,
            "client_drawn": round(client_drawn, 2),
            "firm_operating_drawn": round(firm_drawn, 2),
            "utilization_pct": utilization_pct,
            "headroom": headroom,
            "weighted_average_life_days": wal_days,
            "weighted_average_life_years": round(wal_days / 365, 2),
            "covenant_status": "In Good Standing" if headroom >= 0 else "Over Facility Limit",
            "open_cases": open_cases,
            "case_rate_pct": current_case_rate_pct(),
            "firm_operating_rate_pct": current_firm_operating_rate_pct(),
            "facility_maturity": FIRM_LOC_CONFIG_EXTRA.get(
                "facility_maturity"
            ),  # null until confirmed — was fabricated
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@router.post("/firm/draws")
async def request_firm_draw(
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    try:
        payee = body.get("payee")
        try:
            amount = Decimal(str(body.get("amount")))
        except (InvalidOperation, TypeError, ValueError):
            raise HTTPException(status_code=422, detail="amount must be a valid number.")
        memo = body.get("memo", "")
        requested_by = body.get("requested_by")

        if not all([payee, amount, requested_by]):
            raise HTTPException(
                status_code=400,
                detail="payee, amount, and requested_by are required.",
            )

        if amount <= 0:
            raise HTTPException(
                status_code=400,
                detail="amount must be positive."
            )

        draw = FirmOperatingDraw(
            description=f"{payee} - {memo}" if memo else payee,
            amount=amount,
            requested_by=requested_by,
            status="Pending Approval",
        )

        db.add(draw)
        db.commit()
        db.refresh(draw)

        return {
            "success": True,
            "message": "Firm operating draw requested. Awaiting approval.",
            "data": _firm_draw_dict(draw),
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/firm/draws/pending")
async def list_pending_firm_draws(db: Session = Depends(get_db),current_user: dict = Depends(get_current_user)):
    try:
        draws = (
            db.query(FirmOperatingDraw)
            .filter(FirmOperatingDraw.status == "Pending Approval")
            .all()
        )
        return {"total": len(draws), "data": [_firm_draw_dict(d) for d in draws]}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/firm/draws/{draw_id}/approve", dependencies=[Depends(finance)])
async def approve_firm_draw(
    draw_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    try:
        approved_by = body.get("approved_by") 
        if not approved_by:
            raise HTTPException(status_code=400, detail="approved_by is required.")

        draw = db.query(FirmOperatingDraw).filter(FirmOperatingDraw.id == draw_id).first()
        if not draw:
            raise HTTPException(status_code=404, detail="Firm draw not found.")

        if draw.status != "Pending Approval":
            raise HTTPException(
                status_code=400,
                detail="Only Pending Approval firm draws can be approved.",
            )

        if (draw.requested_by or "").strip().casefold() == approved_by.strip().casefold():
            raise HTTPException(
                status_code=403,
                detail="Dual authorization required: requester cannot approve the same draw.",
            )

        now = datetime.utcnow()
        draw.status = "Funded"
        draw.approved_by = approved_by
        draw.approved_at = now
        draw.funded_at = now

        db.commit()
        db.refresh(draw)

        return {
            "success": True,
            "message": "Firm operating draw approved and funded successfully.",
            "data": _firm_draw_dict(draw),
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/firm/draws/{draw_id}/deny", dependencies=[Depends(finance)])
async def deny_firm_draw(
    draw_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    try:
        denied_by = body.get("denied_by")
        reason = body.get("reason", "")

        if not denied_by:
            raise HTTPException(status_code=400, detail="denied_by is required.")

        draw = db.query(FirmOperatingDraw).filter(FirmOperatingDraw.id == draw_id).first()
        if not draw:
            raise HTTPException(status_code=404, detail="Firm draw not found.")

        if draw.status != "Pending Approval":
            raise HTTPException(
                status_code=400,
                detail="Only Pending Approval firm draws can be denied.",
            )

        draw.status = "Denied"
        draw.denied_reason = reason

        db.commit()
        db.refresh(draw)

        return {
            "success": True,
            "message": "Firm operating draw denied.",
            "data": _firm_draw_dict(draw),
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/firm/draws")
async def list_all_firm_draws(db: Session = Depends(get_db),current_user: dict = Depends(get_current_user)):
    try:
        draws = (
            db.query(FirmOperatingDraw).all()
        )
        return {"total": len(draws), "data": [_firm_draw_dict(d) for d in draws]}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/analytics/summary")
async def analytics_summary(db: Session = Depends(get_db),current_user: dict = Depends(get_current_user)):
    try:
        active_accounts = (
            db.query(PeakTracAccount)
            .filter(PeakTracAccount.account_status == "Active")
            .all()
        )

        ZERO = Decimal("0")

        account_calcs = []

        for a in active_accounts:
            interest, payoff, funded = _compute_live_interest(a, db)

            # Make sure everything is Decimal
            interest = Decimal(str(interest))
            payoff = Decimal(str(payoff))
            funded = Decimal(str(funded))

            account_calcs.append(
                (a, interest, payoff, funded)
            )

        open_accounts = len(active_accounts)

        total_balance = sum(
            (payoff for _, _, payoff, _ in account_calcs),
            ZERO,
        )

        avg_account_balance = (
            round(total_balance / Decimal(open_accounts), 2)
            if open_accounts
            else ZERO
        )

        total_principal = sum(
            (
                Decimal(str(a.principal))
                if a.principal is not None
                else ZERO
            )
            for a in active_accounts
        )

        total_interest = sum(
            (interest for _, interest, _, _ in account_calcs),
            ZERO,
        )

        interest_to_principal_pct = (
            round(
                (total_interest / total_principal) * Decimal("100"),
                2,
            )
            if total_principal
            else ZERO
        )

        oldest_draw = (
            db.query(Draw)
            .filter(
                Draw.draw_status == "Funded",
                Draw.funded_at.isnot(None),
            )
            .order_by(Draw.funded_at.asc())
            .first()
        )

        now = datetime.utcnow()

        oldest_open_draw_days = (
            (now - oldest_draw.funded_at).days
            if oldest_draw
            else None
        )

        # TODO:
        recovered_at_settlement_ytd = None

        case_type_buckets = defaultdict(
            lambda: {
                "principal": ZERO,
                "interest": ZERO,
                "cases": 0,
            }
        )

        for a, interest, _, _ in account_calcs:
            key = a.case_type or "Uncategorized"

            principal = (
                Decimal(str(a.principal))
                if a.principal is not None
                else ZERO
            )

            case_type_buckets[key]["principal"] += principal
            case_type_buckets[key]["interest"] += interest
            case_type_buckets[key]["cases"] += 1

        exposure_by_case_type = [
            {
                "case_type": k,
                "cases": v["cases"],
                "principal": round(v["principal"], 2),
                "accrued_interest": round(v["interest"], 2),
                "total_exposure": round(
                    v["principal"] + v["interest"],
                    2,
                ),
            }
            for k, v in sorted(
                case_type_buckets.items(),
                key=lambda kv: -(
                    kv[1]["principal"] + kv[1]["interest"]
                ),
            )
        ]

        aging_buckets = {
            "0-90": ZERO,
            "91-180": ZERO,
            "181-365": ZERO,
            "365+": ZERO,
        }

        funded_draws_all = (
            db.query(Draw)
            .filter(Draw.draw_status == "Funded")
            .all()
        )

        default_rate = Decimal(str(current_case_rate_pct()))

        account_rate_by_id = {
            a.id: (
                Decimal(str(a.interest_rate))
                if a.interest_rate is not None
                else default_rate
            )
            for a in active_accounts
        }

        for d in funded_draws_all:

            if (
                not d.funded_at
                or d.account_id not in account_rate_by_id
            ):
                continue

            days = (now - d.funded_at).days

            rate = account_rate_by_id[d.account_id]

            amount = (
                Decimal(str(d.amount))
                if d.amount is not None
                else ZERO
            )

            interest = Decimal(
                str(
                    accrued_interest(
                        amount,
                        rate,
                        d.funded_at,
                    )
                )
            )

            balance = amount + interest

            if days <= 90:
                aging_buckets["0-90"] += balance

            elif days <= 180:
                aging_buckets["91-180"] += balance

            elif days <= 365:
                aging_buckets["181-365"] += balance

            else:
                aging_buckets["365+"] += balance

        portfolio_aging = [
            {
                "bucket": k,
                "balance": round(v, 2),
            }
            for k, v in aging_buckets.items()
        ]

        attorney_buckets = defaultdict(
            lambda: {
                "principal": ZERO,
                "interest": ZERO,
                "cases": 0,
            }
        )

        for a, interest, _, _ in account_calcs:

            key = a.attorney_name or "Unassigned"

            principal = (
                Decimal(str(a.principal))
                if a.principal is not None
                else ZERO
            )

            attorney_buckets[key]["principal"] += principal
            attorney_buckets[key]["interest"] += interest
            attorney_buckets[key]["cases"] += 1

        total_portfolio_principal = sum(
            (
                v["principal"]
                for v in attorney_buckets.values()
            ),
            ZERO,
        )

        exposure_by_attorney = [
            {
                "attorney": k,
                "open_accounts": v["cases"],
                "outstanding_principal": round(
                    v["principal"],
                    2,
                ),
                "accrued_interest": round(
                    v["interest"],
                    2,
                ),
                "share_of_portfolio_pct": (
                    round(
                        (
                            v["principal"]
                            / total_portfolio_principal
                        )
                        * Decimal("100"),
                        1,
                    )
                    if total_portfolio_principal
                    else ZERO
                ),
            }
            for k, v in sorted(
                attorney_buckets.items(),
                key=lambda kv: -kv[1]["principal"],
            )
        ]

        return {
            "success": True,
            "summary": {
                "avg_account_balance": avg_account_balance,
                "open_accounts": open_accounts,
                "interest_to_principal_pct": interest_to_principal_pct,
                "oldest_open_draw_days": oldest_open_draw_days,
                "recovered_at_settlement_ytd": recovered_at_settlement_ytd,
            },
            "exposure_by_case_type": exposure_by_case_type,
            "portfolio_aging": portfolio_aging,
            "exposure_by_attorney": exposure_by_attorney,
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


@router.get("/analytics/export")
async def export_analytics_csv(db: Session = Depends(get_db),current_user: dict = Depends(get_current_user)):
    """Generate the Analytics report on the server from current persisted data."""
    report = await analytics_summary(db=db)
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["PeakTrac Analytics Report"])
    writer.writerow(["Generated At (UTC)", datetime.utcnow().isoformat() + "Z"])
    writer.writerow([])
    writer.writerow(["Summary Metric", "Value"])
    for key, value in report.get("summary", {}).items():
        writer.writerow([key, value if value is not None else "Not available"])

    sections = (
        ("Exposure by Case Type", "exposure_by_case_type"),
        ("Portfolio Aging", "portfolio_aging"),
        ("Exposure by Attorney", "exposure_by_attorney"),
    )
    for title, key in sections:
        rows = report.get(key, [])
        writer.writerow([])
        writer.writerow([title])
        if rows:
            writer.writerow(rows[0].keys())
            for row in rows:
                writer.writerow(row.values())

    payload = output.getvalue().encode("utf-8-sig")
    filename = f"peaktrac-analytics-{datetime.utcnow().date().isoformat()}.csv"
    return StreamingResponse(
        iter([payload]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

def _compute_annual_benefit(
    *,
    annual_fees: float,
    case_count: int,
    fee_percentage: float,
    expense_ratio_pct: float,
    avg_draw_life_days: float,
    case_rate_pct: float,
    opportunity_cost_pct: float,
    win_rate_pct: float,
    admin_time_savings_per_case: float,
    admin_hourly_rate: float,
    facility_size: float,
    facility_usable_ceiling_pct: float,
) -> dict:
    implied_annual_recoveries = (
        annual_fees / (fee_percentage / 100)
        if fee_percentage > 0
        else 0
    )
    annual_case_expenses = (
        implied_annual_recoveries * (expense_ratio_pct / 100)
    )
    avg_draw_life_years = avg_draw_life_days / 365
    avg_capital_demand = (
        annual_case_expenses * avg_draw_life_years
    )
    usable_facility_ceiling = (
        facility_size * (facility_usable_ceiling_pct / 100)
        if facility_size > 0
        else 0
    )
    capital_deployed = min(
        avg_capital_demand,
        usable_facility_ceiling,
    )

    deployment_ratio_pct = (
        (capital_deployed / avg_capital_demand) * 100
        if avg_capital_demand > 0
        else 0
    )

    interest_recovered = (
        capital_deployed * (case_rate_pct / 100)
    )
    capital_opportunity_cost = (
        capital_deployed * (opportunity_cost_pct / 100)
    )

    float_cash_flow_value = (
        capital_opportunity_cost * (win_rate_pct / 100)
    )
    admin_minutes_saved_total = (
        case_count * admin_time_savings_per_case
    )

    admin_time_saved_value = (
        (admin_minutes_saved_total / 60)
        * admin_hourly_rate
    )
    total_annual_benefit = (
        interest_recovered
        + float_cash_flow_value
        + admin_time_saved_value
    )

    return {
        "implied_annual_recoveries": round(
            implied_annual_recoveries, 2
        ),
        "annual_case_expenses": round(
            annual_case_expenses, 2
        ),
        "avg_capital_demand": round(
            avg_capital_demand, 2
        ),
        "usable_facility_ceiling": round(
            usable_facility_ceiling, 2
        ),
        "capital_deployed": round(
            capital_deployed, 2
        ),
        "deployment_ratio_pct": round(
            deployment_ratio_pct,
            1,
        ),
        "components": {
            "interest_recovered": round(
                interest_recovered,
                2,
            ),
            "capital_opportunity_cost": round(
                capital_opportunity_cost,
                2,
            ),
            "float_cash_flow_value": round(
                float_cash_flow_value,
                2,
            ),
            "admin_time_savings": round(
                admin_time_saved_value,
                2,
            ),
        },
        "total_annual_benefit": round(
            total_annual_benefit,
            2,
        ),
    }


@router.get("/projections/savings-report")
@router.get("/projections/summary")
async def projections_summary(
    request: Request,
    current_user: dict = Depends(get_current_user),
    annual_fees: float = Query(
        33_478_027.99,
        gt=0,
        description="Illustrative annual fees",
    ),

    case_count: int = Query(
        1762,
        gt=0,
        description="Illustrative annual case count",
    ),

    fee_percentage: float = Query(
        35.0,
        gt=0,
        le=100,
        description="Fee percentage",
    ),

    expense_ratio_pct: float = Query(
        6.0,
        ge=0,
        le=100,
        description="Case expense ratio",
    ),

    avg_draw_life_days: float = Query(
        180.0,
        gt=0,
        description="Average draw life in days",
    ),

    prime_rate_pct: float = Query(
        6.75,
        ge=0,
        le=100,
        description="Prime rate",
    ),

    spread_pct: float = Query(
        1.00,
        ge=0,
        le=100,
        description="PeakTrac spread over Prime",
    ),
    facility_size: float = Query(
        2_000_000,
        gt=0,
        description="Facility size",
    ),

    facility_usable_ceiling_pct: float = Query(
        85.0,
        ge=0,
        le=100,
        description="Usable facility percentage",
    ),

    opportunity_cost_pct: float = Query(
        15.0,
        ge=0,
        le=100,
        description="Opportunity cost percentage",
    ),

    win_rate_pct: float = Query(
        92.0,
        ge=0,
        le=100,
        description="Win rate",
    ),

    admin_time_savings_per_case: float = Query(
        30.0,
        ge=0,
        description="Admin minutes saved per case",
    ),

    admin_hourly_rate: float = Query(
        35.0,
        ge=0,
        description="Administrative hourly value",
    ),
    case_volume_growth_pct: float = Query(
        8.0,
        ge=-100,
        description="Annual case volume growth",
    ),

    db: Session = Depends(get_db),
    snapshot_id: int | None = Query(None),
    case_type: str | None = Query(None),
    projection_years: int = Query(
        5,
        ge=1,
        le=20,
        description="Number of projection years",
    ),
):

    try:
        from db import ProjectionInput
        snapshot = (db.get(ProjectionInput, snapshot_id) if snapshot_id is not None else
                    db.query(ProjectionInput).order_by(ProjectionInput.id.desc()).first())
        if snapshot is None:
            raise HTTPException(409, "Compute a closed-case projection snapshot first.")
        selected = [r for r in snapshot.data if not case_type or r['case_type'] == case_type]
        if not selected:
            raise HTTPException(404, "Case type is not present in this snapshot.")
        n = sum(r['n_cases'] for r in selected)
        life_n = sum(r['n_draw_life_cases'] for r in selected)
        # Explicit user slider values override snapshot defaults.
        if life_n and 'avg_draw_life_days' not in request.query_params:
            avg_draw_life_days = sum((r['average_draw_life_days'] or 0) * r['n_draw_life_cases'] for r in selected) / life_n
        if 'win_rate_pct' not in request.query_params:
            win_rate_pct = sum(r['win_rate_pct'] * r['n_cases'] for r in selected) / n
        if 'expense_ratio_pct' not in request.query_params:
            average_expense = sum(r['average_case_expense'] * r['n_cases'] for r in selected) / n
            expense_ratio_pct = 100 * average_expense * case_count / (annual_fees / (fee_percentage / 100))
        case_rate_pct = (
            prime_rate_pct + spread_pct
        )
        base_kwargs = dict(
            annual_fees=annual_fees,
            case_count=case_count,
            fee_percentage=fee_percentage,
            expense_ratio_pct=expense_ratio_pct,
            avg_draw_life_days=avg_draw_life_days,
            case_rate_pct=case_rate_pct,
            opportunity_cost_pct=opportunity_cost_pct,
            win_rate_pct=win_rate_pct,
            admin_time_savings_per_case=admin_time_savings_per_case,
            admin_hourly_rate=admin_hourly_rate,
            facility_usable_ceiling_pct=facility_usable_ceiling_pct,
        )
        today = _compute_annual_benefit(
            facility_size=facility_size,
            **base_kwargs,
        )
        demand_matched_facility_size = (
            today["avg_capital_demand"]
            / (facility_usable_ceiling_pct / 100)
            if facility_usable_ceiling_pct > 0
            else 0
        )

        addressable = _compute_annual_benefit(
            facility_size=demand_matched_facility_size,
            **base_kwargs,
        )
        benefit_today = round(
            today["total_annual_benefit"],
            2,
        )

        benefit_addressable = round(
            addressable["total_annual_benefit"],
            2,
        )

        remaining_opportunity = round(
            max(
                benefit_addressable - benefit_today,
                0,
            ),
            2,
        )

        captured_today_pct = round(
            (
                benefit_today
                / benefit_addressable
                * 100
            )
            if benefit_addressable > 0
            else 0,
            1,
        )
        facility_is_binding = (
            today["avg_capital_demand"]
            > today["usable_facility_ceiling"]
        )

        facility_constraint = {
            "is_binding": facility_is_binding,
            "modeled_demand": round(
                today["avg_capital_demand"],
                2,
            ),
            "usable_facility_today": round(
                today["usable_facility_ceiling"],
                2,
            ),
            "required_facility": round(
                demand_matched_facility_size,
                2,
            ),
            "remaining_annual_benefit_if_upsized": (
                remaining_opportunity
                if facility_is_binding
                else 0
            ),
        }

        five_year = []

        for i in range(projection_years):
            year_number = i + 1

            year_cases = (
                case_count
                * (
                    1
                    + case_volume_growth_pct / 100
                )
                ** year_number
            )

            year_fees = (
                annual_fees
                * (
                    year_cases / case_count
                )
            )

            year_result = _compute_annual_benefit(
                annual_fees=year_fees,
                case_count=round(year_cases),
                fee_percentage=fee_percentage,
                expense_ratio_pct=expense_ratio_pct,
                avg_draw_life_days=avg_draw_life_days,
                case_rate_pct=case_rate_pct,
                opportunity_cost_pct=opportunity_cost_pct,
                win_rate_pct=win_rate_pct,
                admin_time_savings_per_case=(
                    admin_time_savings_per_case
                ),
                admin_hourly_rate=admin_hourly_rate,
                facility_size=facility_size,
                facility_usable_ceiling_pct=(
                    facility_usable_ceiling_pct
                ),
            )

            five_year.append(
                {
                    "year": year_number,
                    "case_count": round(year_cases),
                    "annual_fees": round(
                        year_fees,
                        2,
                    ),
                    "facility_size": round(
                        facility_size,
                        2,
                    ),
                    "expense_volume": (
                        year_result[
                            "annual_case_expenses"
                        ]
                    ),
                    "capital_deployed": (
                        year_result[
                            "capital_deployed"
                        ]
                    ),
                    "annual_benefit": (
                        year_result[
                            "total_annual_benefit"
                        ]
                    ),
                }
            )

        sensitivity = []

        start_rate = prime_rate_pct - 2.0

        for step in range(9):
            rate = round(
                start_rate + (step * 0.5),
                2,
            )

            sensitivity_case_rate = (
                rate + spread_pct
            )

            sensitivity_result = (
                _compute_annual_benefit(
                    annual_fees=annual_fees,
                    case_count=case_count,
                    fee_percentage=fee_percentage,
                    expense_ratio_pct=expense_ratio_pct,
                    avg_draw_life_days=avg_draw_life_days,
                    case_rate_pct=sensitivity_case_rate,
                    opportunity_cost_pct=opportunity_cost_pct,
                    win_rate_pct=win_rate_pct,
                    admin_time_savings_per_case=(
                        admin_time_savings_per_case
                    ),
                    admin_hourly_rate=admin_hourly_rate,
                    facility_size=facility_size,
                    facility_usable_ceiling_pct=(
                        facility_usable_ceiling_pct
                    ),
                )
            )

            sensitivity.append(
                {
                    "prime_rate_pct": rate,
                    "case_rate_pct": round(
                        sensitivity_case_rate,
                        2,
                    ),
                    "total_annual_benefit": (
                        sensitivity_result[
                            "total_annual_benefit"
                        ]
                    ),
                }
            )
        return {
            "input_snapshot": {"snapshot_id": snapshot.id, "computed_at": snapshot.computed_at.isoformat() + "Z", "n_cases": n},
            "success": True,
            "assumptions": {
                "annual_fees": annual_fees,
                "case_count": case_count,
                "fee_percentage": fee_percentage,
                "expense_ratio_pct": expense_ratio_pct,
                "avg_draw_life_days": avg_draw_life_days,
                "prime_rate_pct": prime_rate_pct,
                "spread_pct": spread_pct,
                "case_rate_pct": case_rate_pct,
                "facility_size": facility_size,
                "facility_usable_ceiling_pct": (
                    facility_usable_ceiling_pct
                ),
                "opportunity_cost_pct": (
                    opportunity_cost_pct
                ),
                "win_rate_pct": win_rate_pct,
                "admin_time_savings_per_case": (
                    admin_time_savings_per_case
                ),
                "admin_hourly_rate": admin_hourly_rate,
                "case_volume_growth_pct": (
                    case_volume_growth_pct
                ),
                "projection_years": projection_years,
            },
            "cards": {
                "benefit_today": benefit_today,

                "benefit_addressable": (
                    benefit_addressable
                ),

                "captured_today_pct": (
                    captured_today_pct
                ),

                "remaining_opportunity": (
                    remaining_opportunity
                ),

                "annual_fees": annual_fees,

                "case_count": case_count,

                "annual_case_expenses": (
                    today["annual_case_expenses"]
                ),

                "avg_capital_demand": (
                    today["avg_capital_demand"]
                ),

                "capital_deployed": (
                    today["capital_deployed"]
                ),

                "facility_size": facility_size,

                "usable_facility": (
                    today["usable_facility_ceiling"]
                ),

                "case_rate_pct": case_rate_pct,
            },
            "legacy_cards": {
                "est_gross_recovery": (
                    today["implied_annual_recoveries"]
                ),
                "annual_expense_volume": (
                    today["annual_case_expenses"]
                ),
                "avg_capital_outstanding": (
                    today["avg_capital_demand"]
                ),
                "benefit_today": benefit_today,
                "benefit_addressable": (
                    benefit_addressable
                ),
                "captured_today_pct": (
                    captured_today_pct
                ),
                "remaining_addressable_benefit": (
                    remaining_opportunity
                ),
            },
            "annual_benefit_components": {
                "today": today["components"],
                "addressable": addressable[
                    "components"
                ],
            },
            "facility_constraint": facility_constraint,
            "methodology": {
                "implied_annual_recoveries": (
                    "Annual Fees / Fee Percentage"
                ),

                "annual_case_expenses": (
                    "Implied Annual Recoveries "
                    "× Expense Ratio"
                ),

                "avg_capital_demand": (
                    "Annual Case Expenses "
                    "× Average Draw Life / 365"
                ),

                "case_rate": (
                    "Prime + PeakTrac Spread"
                ),

                "capital_deployed": (
                    "Minimum of Capital Demand "
                    "and Usable Facility"
                ),

                "interest_recovered": (
                    "Capital Deployed × Case Rate"
                ),

                "float_cash_flow_value": (
                    "Capital Opportunity Cost "
                    "× Win Rate"
                ),

                "admin_time_savings": (
                    "Case Count "
                    "× Minutes Saved per Case "
                    "÷ 60 "
                    "× Hourly Rate"
                ),

                "benefit_today": (
                    "Interest Recovered "
                    "+ Float Cash Flow Value "
                    "+ Administrative Time Savings"
                ),

                "benefit_addressable": (
                    "Benefit using a facility large "
                    "enough to satisfy modeled "
                    "capital demand"
                ),

                "remaining_opportunity": (
                    "Benefit Addressable "
                    "- Benefit Today"
                ),

                "captured_today": (
                    "Benefit Today "
                    "÷ Benefit Addressable × 100"
                ),
            },
            "five_year_projection": {
                "years": five_year,
                "projection_years": projection_years,
            },
            "prime_rate_sensitivity": sensitivity,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e),
        )

def _draw_dict(draw: Draw) -> dict:
    return {
        "neos_code": getattr(draw, "neos_code", None),
        "id": draw.id,
        "account_id": draw.account_id,
        "payee": draw.payee,
        "category": draw.category,
        "expense_category": draw.expense_category,
        "amount": float(draw.amount) if draw.amount is not None else 0,
        "memo": draw.memo,
        "status": draw.draw_status,
        "requested_by": draw.requested_by,
        "requested_at": (
            draw.requested_at.isoformat()
            if draw.requested_at
            else None
        ),
        "approved_by": draw.approved_by,
        "approved_at": (
            draw.approved_at.isoformat()
            if draw.approved_at
            else None
        ),
        "denied_reason": draw.denied_reason,
        "funded_at": (
            draw.funded_at.isoformat()
            if draw.funded_at
            else None
        ),
        "created_at": (
            draw.created_at.isoformat()
            if draw.created_at
            else None
        ),
        "updated_at": (
            draw.updated_at.isoformat()
            if draw.updated_at
            else None
        ),
    }


def _firm_draw_dict(draw: FirmOperatingDraw) -> dict:
    return {
        "id": draw.id,
        "description": draw.description,
        "amount": float(draw.amount) if draw.amount is not None else 0,
        "status": draw.status,
        "requested_by": draw.requested_by,
        "approved_by": getattr(draw, "approved_by", None),
        "funded_at": (
            draw.funded_at.isoformat()
            if getattr(draw, "funded_at", None)
            else None
        ),
        "created_at": (
            draw.created_at.isoformat()
            if getattr(draw, "created_at", None)
            else None
        ),
    }


def _compute_live_interest(account: PeakTracAccount, db: Session) -> tuple[float, float, float]:

    funded_draws = (
        db.query(Draw)
        .filter(
            Draw.account_id == account.id,
            Draw.draw_status == "Funded",
        )
        .all()
    )

    rate = float(account.interest_rate or 0)

    total_accrued_interest = 0.0
    for draw in funded_draws:
        if draw.funded_at:
            total_accrued_interest += accrued_interest(
                float(draw.amount or 0),
                rate,
                draw.funded_at,
            )

    total_accrued_interest = round(total_accrued_interest, 2)
    principal = float(account.principal or 0)
    total_payoff = round(principal + total_accrued_interest, 2)
    total_funded_amount = round(
        sum(float(d.amount or 0) for d in funded_draws), 2
    )

    return total_accrued_interest, total_payoff, total_funded_amount


def _account_summary_dict(account: PeakTracAccount, db: Session) -> dict:
    """Return all account fields with dynamically calculated interest."""


    total_accrued_interest, total_payoff, total_funded_amount = (
        _compute_live_interest(account, db)
    )


    return {
        # Primary identifiers
        "id": account.id,
        "neos_case_id": account.neos_case_id,
        "case_number": account.case_number,


        # Client and case information
        "client_name": account.client_name,
        "phone_number": account.phone_number,
        "attorney_name": account.attorney_name,
        "attorney_id": account.attorney_id,
        "primary_staff_code": account.primary_staff_code,
        "case_type": account.case_type,
        "case_type_id": account.case_type_id,
        "case_status": account.case_status,
        "case_phase": account.case_phase,
        "practice_area": account.practice_area,
        "first_party_is_our_client": account.first_party_is_our_client,


        # Case dates
        "date_of_incident": (
            account.date_of_incident.isoformat()
            if account.date_of_incident else None
        ),
        "date_opened": (
            account.date_opened.isoformat()
            if account.date_opened else None
        ),
        "sol_date": (
            account.sol_date.isoformat()
            if account.sol_date else None
        ),


        # Neos synchronization details
        "neos_last_updated_at": (
            account.neos_last_updated_at.isoformat()
            if account.neos_last_updated_at else None
        ),
        "neos_synced_at": (
            account.neos_synced_at.isoformat()
            if account.neos_synced_at else None
        ),
        "settlement_phase": bool(getattr(account, "settlement_phase", False)),
        "closed_unrecovered": bool(getattr(account, "closed_unrecovered", False)),
        "neos_settlement_amount": float(account.neos_settlement_amount) if getattr(account, "neos_settlement_amount", None) is not None else None,
        "neos_due_to_firm": float(account.neos_due_to_firm) if getattr(account, "neos_due_to_firm", None) is not None else None,
        "neos_case_expenses_on_statement": float(account.neos_case_expenses_on_statement) if getattr(account, "neos_case_expenses_on_statement", None) is not None else None,
        "neos_settlement_checked_at": account.neos_settlement_checked_at.isoformat() if getattr(account, "neos_settlement_checked_at", None) else None,


        # Additional case details
        "office": account.office,
        "policy_limit_max": (
            float(account.policy_limit_max)
            if account.policy_limit_max is not None else None
        ),
        "limit_above_policy": account.limit_above_policy,
        "rpc_1_5_disclosure": account.rpc_1_5_disclosure,


        # Approval information
        "approved_by": account.approved_by,
        "approved_at": (
            account.approved_at.isoformat()
            if account.approved_at else None
        ),


        # Account financial information
        "credit_limit": float(account.credit_limit or 0),
        "available_credit": float(account.available_credit or 0),
        "principal": float(account.principal or 0),
        "interest_rate": float(account.interest_rate or 0),
        "accrued_interest": float(total_accrued_interest or 0),
        "total_payoff": float(total_payoff or 0),
        "payoff_amount": float(account.payoff_amount or 0),
        "total_payroll_amount": float(total_funded_amount or 0),
        "utilization_percent": float(account.utilization_percent or 0),
        "total_draws": account.total_draws,


        # Account status
        "account_status": account.account_status,


        # Audit timestamps
        "created_at": (
            account.created_at.isoformat()
            if account.created_at else None
        ),
        "updated_at": (
            account.updated_at.isoformat()
            if account.updated_at else None
        ),
    }


def _account_detail_dict(account: PeakTracAccount, db: Session) -> dict:
    total_accrued_interest, total_payoff, _ = _compute_live_interest(account, db)

    return {
        "id": account.id,
        "neos_case_id": account.neos_case_id,
        "case_number": account.case_number,
        "client_name": account.client_name,
        "phone_number": account.phone_number,
        "attorney_name": account.attorney_name,
        "attorney_id": account.attorney_id,
        "case_type": account.case_type,
        "case_type_id": account.case_type_id,
        "case_status": account.case_status,
        "case_phase": account.case_phase,
        "practice_area": account.practice_area,
        "primary_staff_code": account.primary_staff_code,
        "first_party_is_our_client": account.first_party_is_our_client,
        "date_of_incident": account.date_of_incident.isoformat() if account.date_of_incident else None,
        "date_opened": account.date_opened.isoformat() if account.date_opened else None,
        "neos_last_updated_at": account.neos_last_updated_at.isoformat() if account.neos_last_updated_at else None,
        "office": account.office,
        "sol_date": account.sol_date.isoformat() if account.sol_date else None,
        "policy_limit_max": float(account.policy_limit_max) if account.policy_limit_max is not None else None,
        "limit_above_policy": bool(account.limit_above_policy),
        "neos_synced_at": account.neos_synced_at.isoformat() if account.neos_synced_at else None,
        "settlement_phase": bool(getattr(account, "settlement_phase", False)),
        "closed_unrecovered": bool(getattr(account, "closed_unrecovered", False)),
        "neos_settlement_amount": float(account.neos_settlement_amount) if getattr(account, "neos_settlement_amount", None) is not None else None,
        "neos_due_to_firm": float(account.neos_due_to_firm) if getattr(account, "neos_due_to_firm", None) is not None else None,
        "neos_case_expenses_on_statement": float(account.neos_case_expenses_on_statement) if getattr(account, "neos_case_expenses_on_statement", None) is not None else None,
        "neos_settlement_checked_at": account.neos_settlement_checked_at.isoformat() if getattr(account, "neos_settlement_checked_at", None) else None,
        "credit_limit": float(account.credit_limit or 0),
        "available_credit": float(account.available_credit or 0),
        "principal": float(account.principal or 0),
        "accrued_interest": total_accrued_interest,
        "payoff_amount": total_payoff,
        "utilization_percent": account.utilization_percent,
        "rpc_1_5_disclosure": account.rpc_1_5_disclosure,
        "interest_rate": float(account.interest_rate or 0),
        "total_draws": account.total_draws,
        "account_status": account.account_status,
        "approved_by": getattr(account, "approved_by", None),
        "approved_at": (
            account.approved_at.isoformat()
            if getattr(account, "approved_at", None)
            else None
        ),
    }

from PeakTrac.neos_api import router as neos_extension_router
router.include_router(neos_extension_router) 

log = logging.getLogger("peaktrac.help")

RATE_LIMIT = 20
RATE_WINDOW_SECONDS = 300

_hits: dict[str, deque] = defaultdict(deque)


class ChatMessage(BaseModel):
    role: str
    content: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    conversation_id: str = Field(default="", max_length=64)
    # Backward compatible with the current frontend payload.
    messages: list[ChatMessage] = Field(default_factory=list)
    # New clients may send only the latest user message.
    message: str | None = Field(default=None, max_length=4000)


def rate_limit(key: str) -> None:
    now = time.monotonic()
    queue = _hits[key]

    while queue and now - queue[0] > RATE_WINDOW_SECONDS:
        queue.popleft()

    if len(queue) >= RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=(
                "You're sending messages quickly. "
                "Please wait a moment and try again."
            ),
        )

    queue.append(now)


@router.post("/chat")
async def help_chat(
    body: ChatRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    rate_limit(user_email(user))

    if body.message is not None and body.message.strip():
        submitted = [{"role": "user", "content": body.message}]
    else:
        submitted = [
            {"role": message.role, "content": message.content}
            for message in body.messages
        ]

    history, flags = process_history(submitted)
    if not history or history[-1]["role"] != "user":
        raise HTTPException(status_code=422, detail="Please type a message.")

    conversation_id = re.sub(r"[^A-Za-z0-9_-]", "", body.conversation_id)[:64] or "none"
    return await run_chat_turn(db, user, conversation_id, history, flags)


@router.get("/reports")
def list_reports(
    limit: int = Query(100, ge=1, le=500),
    status: str | None = Query(None, max_length=16),
    db: Session = Depends(get_db),
    _who: str = Depends(finance),
):

    query = db.query(PeakTracHelpReport)

    if status:
        query = query.filter(
            PeakTracHelpReport.status == status
        )

    rows = (
        query.order_by(PeakTracHelpReport.id.desc())
        .limit(limit)
        .all()
    )

    return [row.record for row in rows]