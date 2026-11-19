# executive_dashboard/router.py
from datetime import datetime, timedelta, date as date_cls
from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Body
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.templating import Jinja2Templates
from attorney_tracker.utils import (get_uploads_any,DEFAULT_THRESHOLD,generate_analysis,_format_currency)
from auth_utils import ACCESS_TOKEN_EXPIRE_MINUTES, authenticate_user, create_access_token, get_current_user,reset_password, generate_otp, create_password, verify_otp
from db import (Session, Whitelist, get_db, LeadIntake, LeadReferral, DashboardRole, QuickbookTransaction, ActiveSession)
try:
    from db import MarketingSpend
except ImportError:
    MarketingSpend = None
from sqlalchemy.sql import exists, or_
from sqlalchemy.orm import Session
from sqlalchemy import func
from dateutil.relativedelta import relativedelta
from collections import defaultdict
from admin.router import admin_router
from admin.auth import get_current_admin
import pandas as pd
from MVA_Platform.router import normalize_source, get_date_range

router = APIRouter()
jinja_templates = Jinja2Templates(directory="executive_dashboard/templates") 
PROJECT_NAME = "Executive_Dashboard"

RANGE_TYPES = [
    "today",
    "weekly",
    "monthly",
    "yearly",
    "last_30_days",
    "last_6_months",
    "this_quarter",
    "last_quarter",
    "this_year",
    "last_year",
    "all_time",
    "custom",
] 

RangeType = Literal[
    "today", "weekly", "monthly", "yearly",
    "last_30_days", "last_6_months",
    "this_quarter", "last_quarter",
    "this_year", "last_year",
    "all_time", "custom",
]

def resolve_date_range(
    range_type: str,
    end_date: datetime,
    start_date: Optional[datetime] = None,
):
    if range_type in ("today", "weekly", "monthly", "yearly"):
        return get_date_range(range_type, end_date)

    now = end_date or datetime.utcnow()

    if range_type == "last_30_days":
        return now - timedelta(days=30), now

    if range_type == "this_quarter":
        q_start_month = 3 * ((now.month - 1) // 3) + 1
        start = now.replace(month=q_start_month, day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, now

    if range_type == "last_quarter":
        q_start_month = 3 * ((now.month - 1) // 3) + 1
        this_q_start = now.replace(month=q_start_month, day=1, hour=0, minute=0, second=0, microsecond=0)
        last_q_end = this_q_start - timedelta(seconds=1)
        last_q_start_month = 3 * ((last_q_end.month - 1) // 3) + 1
        last_q_start = last_q_end.replace(month=last_q_start_month, day=1, hour=0, minute=0, second=0, microsecond=0)
        return last_q_start, last_q_end

    if range_type == "this_year":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0), now

    if range_type == "last_year":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0) - relativedelta(years=1)
        end = start + relativedelta(years=1) - timedelta(seconds=1)
        return start, end

    if range_type == "all_time":
        return datetime(2000, 1, 1), now

    if range_type == "custom":
        if not start_date:
            raise HTTPException(status_code=422, detail="start_date is required when range_type=custom")
        return start_date, now
    
    if range_type == "last_6_months":
        start = (
            now
            - relativedelta(months=5)
        ).replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

        return start, now
    raise HTTPException(status_code=422, detail=f"Unsupported range_type: {range_type}")


PLACEHOLDER_MONTHLY_MARKETING_SPEND = 50000.0

def get_marketing_spend(
    db: Session,
    start_date: datetime,
    end_date: datetime
) -> float:
    
    if MarketingSpend is not None:
        total = (
            db.query(func.sum(MarketingSpend.amount))
            .filter(
                MarketingSpend.date >= start_date,
                MarketingSpend.date <= end_date,
            )
            .scalar()
        )

        if total is not None:
            return float(total)

    months = (
        (end_date.year - start_date.year) * 12
        + (end_date.month - start_date.month)
        + 1
    )

    return PLACEHOLDER_MONTHLY_MARKETING_SPEND * months


@admin_router.get("/exceutive_router/whitelist")
async def list_whitelist_users(
    db: Session = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    users = (
        db.query(Whitelist)
        .filter(Whitelist.role == "user")
        .order_by(Whitelist.email)
        .all()
    )

    existing_roles = {r.email: r for r in db.query(DashboardRole).all()}

    return [
        {
            "email": u.email,
            "dashboard_role": u.email in existing_roles,
            "revenue_access": existing_roles[u.email].revenue_access if u.email in existing_roles else None,
            "attorney_full_access": existing_roles[u.email].attorney_full_access if u.email in existing_roles else None,
        }
        for u in users
    ]

@admin_router.post("/exceutive_router/roles")
async def assign_dashboard_role(
    email: str = Body(...),
    revenue_access: bool = Body(False),
    attorney_full_access: bool = Body(False),
    db: Session = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    email = email.lower().strip()

    whitelisted = db.query(Whitelist).filter(
        Whitelist.email == email,
        Whitelist.role == "user"
    ).first()

    if not whitelisted:
        raise HTTPException(
            status_code=404,
            detail="Email is not whitelisted — cannot assign a dashboard role"
        )

    role = db.query(DashboardRole).filter(DashboardRole.email == email).first()

    if role:
        role.revenue_access = revenue_access
        role.attorney_full_access = attorney_full_access
    else:
        role = DashboardRole(
            email=email,
            revenue_access=revenue_access,
            attorney_full_access=attorney_full_access,
        )
        db.add(role)

    db.commit()
    db.refresh(role)

    return {
        "message": "Role assigned successfully",
        "id": role.id,
        "email": role.email,
        "revenue_access": role.revenue_access,
        "attorney_full_access": role.attorney_full_access,
        "created_at": role.created_at,
    }

@admin_router.patch("/exceutive_router/roles/{role_id}")
async def patch_role(
    role_id: str,
    revenue_access: bool = None,
    attorney_full_access: bool = None,
    db: Session = Depends(get_db)
):
    role = (
        db.query(DashboardRole)
        .filter(DashboardRole.id == role_id)
        .first()
    )

    if not role:
        raise HTTPException(
            status_code=404,
            detail="Role not found"
        )

    if revenue_access is not None:
        role.revenue_access = revenue_access

    if attorney_full_access is not None:
        role.attorney_full_access = attorney_full_access

    db.commit()
    db.refresh(role)

    return role

@admin_router.delete("/exceutive_router/roles/{role_id}")
async def delete_role(
    role_id: str,
    db: Session = Depends(get_db)
):

    role = (
        db.query(DashboardRole)
        .filter(DashboardRole.id == role_id)
        .first()
    )

    if not role:
        raise HTTPException(
            status_code=404,
            detail="Role not found"
        )

    db.delete(role)
    db.commit()

    return {
        "message": "Role deleted successfully"
    }

@admin_router.get("/roles")
async def get_roles(
    db: Session = Depends(get_db)
):
    return (
        db.query(DashboardRole)
        .order_by(DashboardRole.email)
        .all()
    )

@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return jinja_templates.get_template("index.html").render({"request": request})

@router.get("/verify-email")
async def verify_email(
    email: str = Query(...),
    db: Session = Depends(get_db),
):
    email = email.lower().strip()

    user = db.query(Whitelist).filter(
        Whitelist.email == email,
        Whitelist.role == "user"
    ).first()

    if not user:
        raise HTTPException(status_code=404, detail="Email not authorized")

    role = db.query(DashboardRole).filter(DashboardRole.email == email).first()

    if not role:
        raise HTTPException(status_code=403, detail="No dashboard role assigned for this email")

    return {
        "valid": True,
        "password_created": bool(user.password_hash),
        "revenue_access": role.revenue_access,
        "attorney_full_access": role.attorney_full_access,
    }

@router.get("/active-today")
async def active_today(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    today = datetime.utcnow().date()
    active = db.query(ActiveSession.user_email, ActiveSession.last_activity_timestamp).filter(
        ActiveSession.project_name == PROJECT_NAME,
        func.date(ActiveSession.last_activity_timestamp) == today,
    ).all()
    return [{"email": email, "last_active": ts.isoformat()} for email, ts in active]

@router.post("/login")
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    email = form_data.username.lower().strip()

    role = db.query(DashboardRole).filter(DashboardRole.email == email).first()
    if not role:
        raise HTTPException(status_code=403, detail="No dashboard access assigned for this account")

    user = await authenticate_user(email, form_data.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    access_token = await create_access_token(
        data={
            "sub": user["email"],
            "permissions": {
                "revenue_access": role.revenue_access,
                "attorney_full_access": role.attorney_full_access,
            },
        },
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )

    active = db.query(ActiveSession).filter(
            ActiveSession.project_name == PROJECT_NAME,
            ActiveSession.user_email == user["email"],
        ).first()
    if active:
        active.last_activity_timestamp = datetime.utcnow()
    else:
        db.add(ActiveSession(
            project_name=PROJECT_NAME,
            user_email=user["email"],
            last_activity_timestamp=datetime.utcnow(),
        ))
    db.commit()

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "permissions": {
            "revenue_access": role.revenue_access,
            "attorney_full_access": role.attorney_full_access,
        },
    }

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

@router.get("/check-access")
async def check_access(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    email = current_user.get("email")

    role = db.query(DashboardRole).filter(DashboardRole.email == email).first()
    if not role:
        raise HTTPException(status_code=403, detail="No dashboard role assigned")

    return {
        "email": email,
        "can_view_revenue": role.revenue_access,
        "can_view_attorney_details": role.attorney_full_access,
    }

@router.get("/roles")
async def get_roles(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    return (
        db.query(DashboardRole)
        .order_by(DashboardRole.email)
        .all()
    )

@router.put("/roles/{role_id}")
async def update_role(
    role_id: str,
    revenue_access: bool = None,
    attorney_full_access: bool = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    role = (
        db.query(DashboardRole)
        .filter(DashboardRole.id == role_id)
        .first()
    )

    if not role:
        raise HTTPException(
            status_code=404,
            detail="Role not found"
        )

    if revenue_access is not None:
        role.revenue_access = revenue_access

    if attorney_full_access is not None:
        role.attorney_full_access = attorney_full_access

    db.commit()
    db.refresh(role)

    return role

@router.get("/uploads")
async def get_uploads(
    upload_id: Optional[int] = None,
    source: Optional[Literal["tracker", "quickbook"]] = Query(None),
    db: Session = Depends(get_db),
    # current_user: dict = Depends(get_current_user),
    ):
   
    return await run_in_threadpool(get_uploads_any, db=db, upload_id=upload_id, source=source)


@router.get("/uploads/{upload_id}/analysis")
async def get_analysis(
    upload_id: int,
    threshold: float = Query(DEFAULT_THRESHOLD),
    status_filter: Literal["approved", "pending", "locked", "all"] = Query("approved"),
    source: Optional[Literal["tracker", "quickbook"]] = Query(None),
    db: Session = Depends(get_db),
    # current_user: dict = Depends(get_current_user),
):
    effective_threshold = threshold if threshold != 0 else None
    return await run_in_threadpool(
        generate_analysis,
        upload_id=upload_id,
        db=db,
        threshold=effective_threshold,
        status_filter=status_filter,
        source=source,
    )

@router.get("/leads")
async def get_leads(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    fourteen_days_ago = datetime.utcnow() - timedelta(days=14) # fetch last 14 days leads from today
    query = db.query(LeadIntake, exists().where(LeadReferral.lead_id == LeadIntake.id).label("is_referred"))
    query = query.filter(LeadIntake.received_at >= fourteen_days_ago)

    # order latest to oldest
    leads = query.order_by(LeadIntake.received_at.desc()).all()

    result = []
    for lead, is_referred in leads:
        result.append({
            "id": lead.id,
            "email_id": lead.email_id,
            "first_name": lead.first_name,
            "last_name": lead.last_name,
            "email": lead.email,
            "phone": lead.phone,
            "state_of_incident": lead.state_of_incident,
            "case_type": lead.case_type,
            "accident_date": lead.accident_date,
            "fault": lead.fault,
            "injury_severity": lead.injury_severity,
            "medical_treatment": lead.medical_treatment,
            "insurance": lead.insurance,
            "source": lead.source,
            "case_description": lead.case_description,
            "language": lead.language,
            "nafi_tool_recommendation": lead.nafi_tool_recommendation,
            "nafi_tool_reason": lead.nafi_tool_reason,
            "status": lead.status,
            "decline_type": lead.decline_type,
            "reviewer_note": lead.reviewer_note,
            "reviewed_by": lead.reviewed_by,
            "reviewed_at": lead.reviewed_at,
            "email_sent": lead.email_sent,
            "received_at": lead.received_at,
            "practice_area": lead.practice_area,
            "assigned_group": lead.assigned_group,
            "assigned_users": lead.assigned_users,
            "lead_accepted":  lead.lead_accepted,
            "lead_accepted_by": lead.lead_accepted_by,
            "is_referral_linked": is_referred
        })
    return result

@router.get("/pipeline_leads")
async def pipeline_leads(
    range_type: RangeType = Query("weekly"),
    end_date: datetime = Query(...),
    start_date: Optional[datetime] = Query(None, description="Required when range_type=custom"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    start_date, end_date = resolve_date_range(range_type, end_date, start_date)

    leads = db.query(LeadIntake).filter(
        LeadIntake.received_at >= start_date,
        LeadIntake.received_at <= end_date
    ).all() 

    # Total Active (all pending leads)

    total_active = sum(
        1 for lead in leads
        if lead.status not in ["proceed", "declined"]
    )

    # Last 48 Hours Burst

    last_48_hours = end_date - timedelta(hours=48)

    burst_count = sum(
        1 for lead in leads
        if lead.received_at and lead.received_at >= last_48_hours
    )

    # Lead -> Case Conversion

    qualified_leads = len([
        l for l in leads
        if l.status == "proceed"
    ])

    signed_cases = len([
        l for l in leads
        if l.lead_accepted is True
    ])

    conversion_rate = round(
        (signed_cases / qualified_leads) * 100,
        1
    ) if qualified_leads else 0

    # Top State

    state_counts = {}

    for lead in leads:

        state = (
            lead.state_of_incident
            or "Unknown"
        )

        state_counts[state] = (
            state_counts.get(state, 0) + 1
        )

    top_state = None
    top_state_pct = 0

    if state_counts:

        top_state = max(
            state_counts,
            key=state_counts.get
        )

        top_state_pct = round(
            (
                state_counts[top_state]
                / len(leads)
            ) * 100,
            1
        )

    if range_type == "today":
        weekdays = [f"{hour:02d}:00" for hour in range(24)]
    else:
        all_weekdays = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]

        start_day = start_date.strftime("%a")
        start_idx = all_weekdays.index(start_day)

        weekdays = (
            all_weekdays[start_idx:]
            + all_weekdays[:start_idx]
        )

    # INTAKE BY WEEKDAY
    intake = {d:0 for d in weekdays}

    for l in leads:
        if not l.received_at:
            continue
        if range_type == "today":
            bucket = f"{l.received_at.hour:02d}:00"
        else:
            bucket = l.received_at.strftime("%a")
        intake[bucket] += 1

    # SOURCE MIX
    source_raw = {}
    for l in leads:
        key = normalize_source(l.source)
        source_raw[key] = source_raw.get(key, 0) + 1

    total_source = sum(source_raw.values()) or 1
    source_mix  = dict(
        sorted(
            (
                (k, {"count": v, "pct": round(v / total_source * 100, 1)})
                for k, v in source_raw.items()
                if v > 0
            ),
            key=lambda x: x[1]["count"],
            reverse=True
        )[:10]
    )

    # CASE TYPE
    case_type = {}
    for l in leads:
        key = l.case_type or "Other"
        case_type[key] = case_type.get(key, 0) + 1

    # DISPOSITION
    disposition = {d:{} for d in weekdays}

    for l in leads:
        if not l.received_at:
            continue

        if range_type == "today":
            wd = f"{l.received_at.hour:02d}:00"
        else:
            wd = l.received_at.strftime("%a")

        if l.status == "proceed":
            bucket = "Proceed"
        elif l.status == "declined":
            bucket = "Declined"
        elif l.status == "escalated":
            bucket = "Escalated"
        else:
            bucket = "Pending"

        disposition[wd][bucket] = disposition[wd].get(bucket, 0) + 1

    return {
    "summary": {
        "total_active": {
            "value": total_active,
            "message": "All pending and active leads."
        },

        "lead_burst_48hr": {
            "value": burst_count,
            "message": "New leads received during the last 48 hours."
        },

        "lead_to_case_conversion": {
            "value": conversion_rate,
            "message": "Qualified leads converted into signed cases."
        },

        "top_state": {
            "state": top_state,
            "percentage": top_state_pct,
            "message": "State generating the highest lead volume."
        }
    },

    "intake_by_weekday": {
        "data": intake,
        "message": (
            "Shows lead volume by weekday based on lead received dates."
        )
    },

    "sources": {
        "data": source_mix,
        "message": (
            "Shows where leads are coming from based on lead sources."
        )
    },

    "case_types": {
        "data": case_type,
        "message": (
            "Shows the most common case types based on submitted lead information."
        )
    },

    "disposition": {
        "data": disposition,
        "message": (
            "Shows lead outcomes by weekday based on lead review statuses."
        )
    },
    }


def _monthly_trend(
    db: Session,
    start_date: datetime,
    end_date: datetime,
    compute_fn,
):
    """
    Generate monthly chart data between the selected start_date and end_date.

    Example:
        start_date = 2026-07-13
        end_date   = 2026-08-13

    Result:
        [
            {"month": "Jul 2026", "value": ...},
            {"month": "Aug 2026", "value": ...}
        ]
    """

    # Start from the first day of the start month
    first_month = start_date.replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    series = []

    current_month = first_month

    while current_month <= end_date:

        next_month = current_month + relativedelta(months=1)

        # Full month boundaries
        month_start = current_month
        month_end = next_month - timedelta(seconds=1)

        # Respect the selected date range
        effective_start = max(month_start, start_date)
        effective_end = min(month_end, end_date)

        if effective_start <= effective_end:

            month_leads = (
                db.query(LeadIntake)
                .filter(
                    LeadIntake.received_at >= effective_start,
                    LeadIntake.received_at <= effective_end,
                )
                .all()
            )

            value = compute_fn(
                month_leads,
                effective_start,
                effective_end,
                db,
            )

            series.append({
                "month": current_month.strftime("%b %Y"),
                "value": value,
            })

        current_month = next_month

    return series

def _get_month_transactions(
    db: Session,
    m_start: datetime,
    m_end: datetime,
):
    return (
        db.query(QuickbookTransaction)
        .filter(
            QuickbookTransaction.is_valid == True,
            QuickbookTransaction.date >= m_start,
            QuickbookTransaction.date <= m_end,
        )
        .all()
    )


def _average_case_value_month(
    month_leads,
    m_start,
    m_end,
    db,
):
    transactions = _get_month_transactions(
        db,
        m_start,
        m_end,
    )

    values = [
        float(t.credit)
        for t in transactions
        if t.credit
    ]

    if not values:
        return None

    return round(
        sum(values) / len(values),
        2,
    )


def _total_firm_settlements_month(
    month_leads,
    m_start,
    m_end,
    db,
):
    transactions = _get_month_transactions(
        db,
        m_start,
        m_end,
    )

    return sum(
        1
        for t in transactions
        if t.credit
    )


def _avg_settlement_firm_month(
    month_leads,
    m_start,
    m_end,
    db,
):
    return _average_case_value_month(
        month_leads,
        m_start,
        m_end,
        db,
    )

def _qualified_leads_month(
    month_leads,
    *_,
):
    return sum(
        1
        for l in month_leads
        if l.status == "proceed"
    )


def _qualified_rate_month(
    month_leads,
    *_,
):
    if not month_leads:
        return None

    qualified = sum(
        1
        for l in month_leads
        if l.status == "proceed"
    )

    return round(
        qualified / len(month_leads) * 100,
        1,
    )


def _rejected_rate_month(
    month_leads,
    *_,
):
    if not month_leads:
        return None

    rejected = sum(
        1
        for l in month_leads
        if l.status == "declined"
    )

    return round(
        rejected / len(month_leads) * 100,
        1,
    )


def _signed_clients_month(
    month_leads,
    *_,
):
    return sum(
        1
        for l in month_leads
        if l.lead_accepted is True
    )


def _cost_per_qualified_lead_month(
    month_leads,
    m_start,
    m_end,
    db,
):
    spend = get_marketing_spend(
        db,
        m_start,
        m_end,
    )

    qualified = sum(
        1
        for l in month_leads
        if l.status == "proceed"
    )

    if not spend or not qualified:
        return None

    return round(
        spend / qualified,
        2,
    )


def _case_acquisition_cost_month(
    month_leads,
    m_start,
    m_end,
    db,
):
    spend = get_marketing_spend(
        db,
        m_start,
        m_end,
    )

    signed = sum(
        1
        for l in month_leads
        if l.lead_accepted is True
    )

    if not spend or not signed:
        return None

    return round(
        spend / signed,
        2,
    )


def _client_referral_rate_month(
    month_leads,
    *_,
):
    signed = [
        l
        for l in month_leads
        if l.lead_accepted is True
    ]

    if not signed:
        return None

    referrals = [
        l
        for l in signed
        if isinstance(l.source, str)
        and "referral" in l.source.lower()
    ]

    return round(
        len(referrals) / len(signed) * 100,
        1,
    )

@router.get("/executive-overview")
async def get_executive_overview(
    range_type: RangeType = Query("last_6_months"),
    end_date: datetime = Query(default_factory=datetime.utcnow),
    start_date: Optional[datetime] = Query(None, description="Required when range_type=custom"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    start_date, end_date = resolve_date_range(range_type, end_date, start_date)

    leads = db.query(LeadIntake).filter(
        LeadIntake.received_at >= start_date,
        LeadIntake.received_at <= end_date,
    ).all()

    total_leads = len(leads)
    signed_leads = [l for l in leads if l.lead_accepted is True]
    qualified_leads = [l for l in leads if l.status == "proceed"]
    rejected_leads = [l for l in leads if l.status == "declined"]
    signed_count = len(signed_leads)
    qualified_count = len(qualified_leads)
    rejected_count = len(rejected_leads)

    qualified_lead_rate = round(qualified_count / total_leads * 100, 1) if total_leads else None
    rejected_lead_rate = round(rejected_count / total_leads * 100, 1) if total_leads else None

    referral_signed = [
        l for l in signed_leads
        if isinstance(l.source, str) and "referral" in l.source.lower()
    ]
    client_referral_rate = round(len(referral_signed) / signed_count * 100, 1) if signed_count else None

    source_counts = {}
    for l in signed_leads:
        key = normalize_source(l.source)
        source_counts[key] = source_counts.get(key, 0) + 1
    signed_cases_by_source = [
        {"source": k, "signed_cases": v}
        for k, v in sorted(source_counts.items(), key=lambda x: x[1], reverse=True)
    ]

    marketing_spend = get_marketing_spend(db, start_date, end_date)

    cost_per_qualified_lead = (
        round(marketing_spend / qualified_count, 2)
        if marketing_spend is not None and qualified_count else None
    )
    case_acquisition_cost = (
        round(marketing_spend / signed_count, 2)
        if marketing_spend is not None and signed_count else None
    )
    txns = db.query(QuickbookTransaction).filter(
        QuickbookTransaction.is_valid == True,
    ).all()
    settled_values = [float(t.credit) for t in txns if t.credit]
    avg_case_value = round(sum(settled_values) / len(settled_values), 2) if settled_values else None
    median_case_value = round(pd.Series(settled_values).median(), 2) if settled_values else None
    total_firm_settlements = len(settled_values)
    avg_settlement_firm = avg_case_value  # same figure, distinct KPI slot per the layout spec

    active_pipeline_leads = [l for l in leads if l.status not in ("declined",) and l.lead_accepted is not True]
    estimated_revenue = (
        round(len(active_pipeline_leads) * avg_case_value + sum(settled_values), 2)
        if avg_case_value is not None else None
    )

    marketing_roi_ratio = (
        round(estimated_revenue / marketing_spend, 1)
        if marketing_spend and estimated_revenue is not None else None
    ) 

    # NOTE: kept as a local closure (not module-level) because it needs
    # `avg_case_value`, which is only known once leads/txns for this
    # request's date range have been computed above.
    def _marketing_roi_month(month_leads, m_start, m_end, db):
        spend = get_marketing_spend(db, m_start, m_end)

        if not spend or avg_case_value is None:
            return None

        active = [
            l for l in month_leads
            if l.status != "declined"
            and l.lead_accepted is not True
        ]

        signed = [
            l for l in month_leads
            if l.lead_accepted is True
        ]

        pipeline_revenue = len(active) * avg_case_value
        signed_revenue = len(signed) * avg_case_value

        estimated_revenue_month = (
            pipeline_revenue + signed_revenue
        )

        return round(
            estimated_revenue_month / spend,
            1
        )

    qualified_leads_trend = _monthly_trend(
        db,
        start_date,
        end_date,
        _qualified_leads_month
    )

    qualified_lead_rate_trend = _monthly_trend(
        db,
        start_date,
        end_date,
        _qualified_rate_month
    )

    rejected_lead_rate_trend = _monthly_trend(
        db,
        start_date,
        end_date,
        _rejected_rate_month
    )

    signed_clients_trend = _monthly_trend(
        db,
        start_date,
        end_date,
        _signed_clients_month
    )

    cost_per_qualified_lead_trend = _monthly_trend(
        db,
        start_date,
        end_date,
        _cost_per_qualified_lead_month
    )

    cac_trend = _monthly_trend(
        db,
        start_date,
        end_date,
        _case_acquisition_cost_month
    )

    marketing_roi_trend = _monthly_trend(
        db,
        start_date,
        end_date,
        _marketing_roi_month
    )

    client_referral_rate_trend = _monthly_trend(
        db,
        start_date,
        end_date,
        _client_referral_rate_month
    )

    payload = {
        "range_type": range_type,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),

        "kpis": [
            {
                "id": "qualified_leads",
                "label": "Qualified Leads",
                "value": qualified_count
            },
            {
                "id": "qualified_lead_rate",
                "label": "Qualified Lead Rate",
                "value": qualified_lead_rate
            },
            {
                "id": "rejected_lead_rate",
                "label": "Rejected Lead Rate",
                "value": rejected_lead_rate
            },
            {
                "id": "signed_clients",
                "label": "Signed Clients",
                "value": signed_count
            },
            {
                "id": "cost_per_qualified_lead",
                "label": "Cost Per Qualified Lead",
                "value": cost_per_qualified_lead
            },
            {
                "id": "case_acquisition_cost",
                "label": "Case Acquisition Cost",
                "value": case_acquisition_cost,
                "display": (
                    _format_currency(case_acquisition_cost)
                    if case_acquisition_cost is not None
                    else None
                ),
                "tooltip": (
                    "Total marketing spend divided by signed clients."
                ),
            },
            {
                "id": "marketing_roi",
                "label": "Marketing ROI",
                "value": marketing_roi_ratio,
                "display": (
                    f"{marketing_roi_ratio} : 1"
                    if marketing_roi_ratio is not None
                    else None
                ),
                "tooltip": (
                    "Projected marketing ROI. For every $1 spent on marketing, "
                    f"we generate ${marketing_roi_ratio} in estimated pipeline revenue. "
                    "Note: This reflects potential pipeline value generated, "
                    "not realized settlement cash."
                    if marketing_roi_ratio is not None
                    else (
                        "Projected marketing ROI based on estimated pipeline revenue "
                        "and marketing spend."
                    )
                ),
            },
            {
                "id": "estimated_revenue",
                "label": "Estimated Revenue",
                "value": estimated_revenue,
                "display": _format_currency(estimated_revenue) if estimated_revenue is not None else None
            },
            {
                "id": "average_case_value",
                "label": "Average Case Value",
                "value": avg_case_value,
                "median": median_case_value
            },
            {
                "id": "total_firm_settlements",
                "label": "Total Firm Settlements",
                "value": total_firm_settlements
            },
            {
                "id": "avg_settlement_firm",
                "label": "Avg Settlement (Firm)",
                "value": avg_settlement_firm,
                "median": median_case_value
            },
            {
                "id": "client_referral_rate",
                "label": "Client Referral Rate",
                "value": client_referral_rate
            },
        ],

        "charts": {
            "qualified_leads": {
                "type": "line",
                "title": "Qualified Leads",
                "unit": "count",
                "data": qualified_leads_trend,
            },

            "qualified_lead_rate": {
                "type": "line",
                "title": "Qualified Lead Rate",
                "unit": "percentage",
                "data": qualified_lead_rate_trend,
            },

            "rejected_lead_rate": {
                "type": "line",
                "title": "Rejected Lead Rate",
                "unit": "percentage",
                "data": rejected_lead_rate_trend,
            },

            "signed_clients": {
                "type": "line",
                "title": "Signed Clients",
                "unit": "count",
                "data": signed_clients_trend,
            },

            "cost_per_qualified_lead": {
                "type": "line",
                "title": "Cost Per Qualified Lead",
                "unit": "currency",
                "data": cost_per_qualified_lead_trend,
            },

            "case_acquisition_cost": {
                "type": "line",
                "title": "Case Acquisition Cost",
                "unit": "currency",
                "data": cac_trend,
            },

            "marketing_roi": {
                "type": "line",
                "title": "Marketing ROI",
                "unit": "ratio",
                "data": marketing_roi_trend,
            },

            "client_referral_rate": {
                "type": "line",
                "title": "Client Referral Rate",
                "unit": "percentage",
                "data": client_referral_rate_trend,
            },

            "signed_cases_by_source": {
                "type": "bar",
                "title": "Signed Cases by Source",
                "unit": "count",
                "data": signed_cases_by_source,
            },
        },

        "marketing_spend_available": marketing_spend is not None,
    }

    return payload

@router.get("/quarterly-review")
async def get_quarterly_review(
    year: int = Query(datetime.utcnow().year, description="Year to fetch quarterly data"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    now = datetime.utcnow()
    current_year = now.year
    current_quarter = ((now.month - 1) // 3) + 1

    quarter_stats = []

    for quarter in range(1, 5):
        # Skip future quarters for the current year
        if year == current_year and quarter > current_quarter:
            break

        start_month = (quarter - 1) * 3 + 1

        q_start = datetime(year, start_month, 1)

        if quarter == 4:
            q_end = datetime(year + 1, 1, 1) - timedelta(seconds=1)
        else:
            q_end = datetime(year, start_month + 3, 1) - timedelta(seconds=1)

        # If current quarter, use today's date as the end
        effective_end = min(q_end, now) if year == current_year else q_end

        q_leads = (
            db.query(LeadIntake)
            .filter(
                LeadIntake.received_at >= q_start,
                LeadIntake.received_at <= effective_end,
            )
            .all()
        )

        signed = sum(1 for l in q_leads if l.lead_accepted is True)
        qualified = sum(1 for l in q_leads if l.status == "proceed")

        quarter_stats.append({
            "quarter": f"Q{quarter} {year}",
            "start_date": q_start.isoformat(),
            "end_date": effective_end.isoformat(),
            "signed_clients": signed,
            "qualified_leads": qualified,
        })

    latest = quarter_stats[-1] if quarter_stats else None
    prior = quarter_stats[-2] if len(quarter_stats) > 1 else None

    def pct_change(current, previous):
        if previous in (None, 0):
            return None
        return round(((current - previous) / previous) * 100, 1)

    scorecard = None
    if latest:
        scorecard = {
            "quarter": latest["quarter"],
            "signed_clients": {
                "value": latest["signed_clients"],
                "change_pct": pct_change(
                    latest["signed_clients"],
                    prior["signed_clients"] if prior else None,
                ),
            },
            "qualified_leads": {
                "value": latest["qualified_leads"],
                "change_pct": pct_change(
                    latest["qualified_leads"],
                    prior["qualified_leads"] if prior else None,
                ),
            },
        }

    return {
        "selected_year": year,
        "quarterly_trend": quarter_stats,
        "scorecard": scorecard,
    }


@router.get("/practice-areas/{upload_id}")
async def get_practice_areas(
    upload_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    # Fetch transactions
    transactions = (
        db.query(QuickbookTransaction)
        .filter(
            QuickbookTransaction.upload_id == upload_id,
            QuickbookTransaction.is_valid == True,
        )
        .all()
    )

    if not transactions:
        raise HTTPException(
            status_code=404,
            detail="No QuickBooks transactions found."
        )

    df = pd.DataFrame([
        {
            "case_number": t.case_number,
            "case_type": t.case_type_merged or t.case_type_description,
            "credit": float(t.credit or 0),
        }
        for t in transactions
    ])

    df = df.dropna(subset=["case_type"])

    if df.empty:
        raise HTTPException(
            status_code=404,
            detail="No valid case types found."
        )

    total_revenue = df["credit"].sum()

    # Revenue by Case Type
    case_type_group = (
        df.groupby("case_type")["credit"]
        .sum()
        .sort_values(ascending=False)
    )

    case_type_breakdown = [
        {
            "case_type": case_type,
            "revenue": float(revenue),
            "revenue_display": _format_currency(revenue),
            "pct_of_total": round(
                (revenue / total_revenue) * 100,
                1
            ) if total_revenue else 0.0,
        }
        for case_type, revenue in case_type_group.items()
    ]

    # Highest Revenue Area
    highest_case_type = case_type_group.idxmax()
    highest_revenue = case_type_group.max()

    highest_revenue_area = {
        "title": "Highest Revenue Area",
        "case_type": highest_case_type,
        "value": float(highest_revenue),
        "display": _format_currency(highest_revenue),
    }
    # Highest Average Case Value
    if df["case_number"].notna().any():

        case_totals = (
            df.groupby(["case_type", "case_number"])["credit"]
            .sum()
            .reset_index()
        )

        avg_case = (
            case_totals.groupby("case_type")["credit"]
            .mean()
            .sort_values(ascending=False)
        )

    else:
        avg_case = (
            df.groupby("case_type")["credit"]
            .mean()
            .sort_values(ascending=False)
        )

    highest_avg_type = avg_case.idxmax()
    highest_avg = avg_case.max()

    highest_avg_case_value = {
        "title": "Highest Avg Case Value",
        "case_type": highest_avg_type,
        "value": float(highest_avg),
        "display": _format_currency(highest_avg),
    }
    return {
        "summary": {
            "highest_revenue_area": highest_revenue_area,
            "highest_avg_case_value": highest_avg_case_value,
        },
        "total_revenue": float(total_revenue),
        "total_revenue_display": _format_currency(total_revenue),
        "case_type_breakdown": case_type_breakdown,
    }