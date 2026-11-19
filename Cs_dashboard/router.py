# router.py — CSDashboard  (all routes, UI-aligned responses)
import calendar
import csv
import io
from datetime import date
from typing import List, Literal, Optional
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from db import ExitInterview, get_db, ActiveSession, Whitelist , CustomServiceFile
from Cs_dashboard.services.graph_sync import get_last_sync, sync_from_graph
from Cs_dashboard.services.xlsx_ingest import ingest_xlsx
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from auth_utils import authenticate_user, create_access_token, ACCESS_TOKEN_EXPIRE_MINUTES, get_current_user, reset_password, generate_otp, verify_otp, SessionLocal, create_password
from fastapi.security import OAuth2PasswordRequestForm
from datetime import timedelta
from datetime import datetime
from sqlalchemy import or_


router = APIRouter()
PROJECT_NAME = "Cs_dashboard"

# ─── helpers ──────────────────────────────────────────────────────────────────

MONTH_ORDER = {
    "January": 1, "Jan": 1,
    "February": 2, "Feb": 2,
    "March": 3, "Mar": 3,
    "April": 4, "Apr": 4,
    "May": 5,
    "June": 6, "Jun": 6,
    "July": 7, "Jul": 7,
    "August": 8, "Aug": 8,
    "September": 9, "Sep": 9,
    "October": 10, "Oct": 10,
    "November": 11, "Nov": 11,
    "December": 12, "Dec": 12,
}

def _apply_file_filter(q, file_id: Optional[int]):
    if file_id:
        q = q.filter(ExitInterview.file_id == file_id)
    return q


def _period_label(year: str, month: str) -> str:
    """Build a short display label like 'Jan 2025'."""
    try:
        m_num = MONTH_ORDER.get(month, 0)
        abbr  = calendar.month_abbr[m_num] if m_num else month[:3]
        return f"{abbr} {year}"
    except Exception:
        return f"{month} {year}"


def _filter_by_period(query, period: str):
    today = date.today()

    if period == "weekly":
        week_start = today - timedelta(days=7)
        query = query.filter(
            func.date(ExitInterview.submitted_at) >= week_start
        )

    elif period == "monthly":
        # last 30 days (better for trends than "current month only")
        month_start = today - timedelta(days=30)
        return query.filter(func.date(ExitInterview.submitted_at) >= month_start)

    elif period == "yearly":
        query = query.filter(
            ExitInterview.year == str(today.year)
        )

    return query


def _apply_filters(q, year, month, attorney_code, paralegal_code):
    if year:          q = q.filter(ExitInterview.year          == year)
    if month:         q = q.filter(ExitInterview.month         == month)
    if attorney_code: q = q.filter(ExitInterview.attorney_code == attorney_code)
    if paralegal_code:q = q.filter(ExitInterview.paralegal_code== paralegal_code)
    return q


def _badge(avg: Optional[float]) -> str:
    if avg is None: return "yellow"
    return "green" if avg >= 4.5 else ("yellow" if avg >= 3.5 else "red")


def _severity(alert_type: str) -> str:
    return "critical" if alert_type == "critical_rating" else "warning"


jinja_templates = Jinja2Templates(directory="Cs_dashboard/templates")

@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
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

@router.get("/active-today")
async def active_today(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    today = datetime.utcnow().date()
    active = db.query(ActiveSession.user_email, ActiveSession.last_activity_timestamp).filter(
        ActiveSession.project_name == PROJECT_NAME,
        func.date(ActiveSession.last_activity_timestamp) == today
    ).all()
    return [{"email": email, "last_active": ts.isoformat()} for email, ts in active]


@router.post("/upload-xlsx")
async def upload_xlsx(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    if not file.filename.endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Only .xlsx files are accepted.")
    contents = await file.read()
    log = ingest_xlsx(contents, file.filename, db, triggered_by="admin_upload")
    return {
        "status":        log.status,
        "rows_fetched":  log.rows_fetched,
        "rows_inserted": log.rows_inserted,
        "rows_skipped":  log.rows_skipped,
        "error":         log.error_message,
        "synced_at":     log.synced_at,
    }

@router.get("/dashboard/kpis")
def get_kpis(
    period: Literal["weekly", "monthly", "yearly", "total"] = "total",
    file_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    q = db.query(ExitInterview)

    q = _apply_file_filter(q, file_id)

    q = _filter_by_period(q, period)

    rows = q.all()

    total = len(rows)

    def avg_col(col):
        vals = [getattr(r, col) for r in rows if getattr(r, col) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    return {
        "total_reviews"            : total,
        "avg_attorney_rating"      : avg_col("rating_attorney_comm"),
        "avg_paralegal_rating"     : avg_col("rating_paralegal_comm"),
        "avg_overall_rating"       : avg_col("rating_overall"),
        "avg_case_outcome_rating"  : avg_col("rating_case_outcome"),
        "referral_rate_pct"        : round(sum(1 for r in rows if r.referral_flag) / total * 100, 1) if total else 0,
        "review_link_sent_pct"     : round(sum(1 for r in rows if r.review_sent)   / total * 100, 1) if total else 0,
        "period"                   : period,    
        }


@router.get("/dashboard/charts/ratings-trend")
def ratings_trend(
    period: Literal["weekly", "monthly", "yearly", "total"] = "total",
    file_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
): 
    q = db.query(
    ExitInterview.year,
    ExitInterview.month,
    func.avg(ExitInterview.rating_attorney_comm).label("avg_attorney"),
    func.avg(ExitInterview.rating_paralegal_comm).label("avg_paralegal"),
    func.avg(ExitInterview.rating_overall).label("avg_overall"),
    func.avg(ExitInterview.rating_case_outcome).label("avg_case_outcome"),
    func.count(ExitInterview.id).label("survey_count"),
    )

    q = _apply_file_filter(q, file_id)

    q = _filter_by_period(q, period)

    q = q.group_by(
        ExitInterview.year,
        ExitInterview.month
    )

    rows = q.all()
    rows.sort(key=lambda r: (r.year or "", MONTH_ORDER.get(r.month or "", 0)))

    return [
        {
            "year": r.year or "",
            "month": r.month or "",
            "period_label": _period_label(r.year or "", r.month or ""),
            "avg_attorney": round(r.avg_attorney, 2) if r.avg_attorney else None,
            "avg_paralegal": round(r.avg_paralegal, 2) if r.avg_paralegal else None,
            "avg_overall": round(r.avg_overall, 2) if r.avg_overall else None,
            "avg_case_outcome": round(r.avg_case_outcome, 2) if r.avg_case_outcome else None,
            "survey_count": r.survey_count,
        }
        for r in rows
    ]


@router.get("/dashboard/charts/monthly-volume")
def monthly_volume(
    period: str = Query("total"),
    file_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):  
    q = db.query(
    ExitInterview.year,
    ExitInterview.month,
    func.count(ExitInterview.id).label("count"),
    )

    q = _apply_file_filter(q, file_id)

    q = _filter_by_period(q, period)

    q = q.group_by(
        ExitInterview.year,
        ExitInterview.month
    )

    rows = q.all()
    rows.sort(key=lambda r: (r.year or "", MONTH_ORDER.get(r.month or "", 0)))

    return [
        {
            "year" : r.year or "",
            "month"        : r.month or "",
            "period_label" : _period_label(r.year or "", r.month or ""),
            "count"        : r.count,
        }
        for r in rows
    ]


@router.get("/dashboard/charts/referral-breakdown")
def referral_breakdown(
    period: str = Query("total"),
    file_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
): 
    q = db.query(
    ExitInterview.willing_to_refer,
    func.count(ExitInterview.id).label("count"),
    )

    q = _apply_file_filter(q, file_id)

    q = _filter_by_period(q, period)

    q = q.group_by(
        ExitInterview.willing_to_refer
    )

    rows  = q.all()
    total = sum(r.count for r in rows) or 1

    return [
        {
            "label" : r.willing_to_refer or "Unknown",
            "count" : r.count,
            "pct"   : round(r.count / total * 100, 1),
        }
        for r in rows
    ]


@router.get("/dashboard/charts/google-review-status")
def dashboard_google_review_status(
    period: Literal["weekly", "monthly", "yearly", "total"] = "total",
    file_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):  
    q = db.query(
    ExitInterview.google_review_status,
    func.count(ExitInterview.id).label("count"),
    )

    q = _apply_file_filter(q, file_id)

    q = _filter_by_period(q, period)

    q = q.group_by(
        ExitInterview.google_review_status
    )

    rows  = q.all()
    total = sum(r.count for r in rows) or 1

    return [
        {
            "label" : r.google_review_status or "Unknown",
            "count" : r.count,
            "pct"   : round(r.count / total * 100, 1),
        }
        for r in rows
    ]


@router.get("/dashboard/recent-reviews")
def recent_reviews(
    file_id: Optional[int] = None,
    limit: int = Query(10, le=50),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
): 
    q = db.query(ExitInterview)

    q = _apply_file_filter(q, file_id)
    return (
        q
        .order_by(ExitInterview.submitted_at.desc())
        .limit(limit)
        .all()
    )


@router.get("/dashboard/sync-status")
def sync_status(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    last = get_last_sync(db)
    
    return {
        "last_synced_at" : last.synced_at,
        "status"         : last.status,
        "rows_inserted"  : last.rows_inserted,
        "rows_skipped"   : last.rows_skipped,
        "rows_fetched"   : last.rows_fetched,
        "error_message"  : last.error_message,
        "sync_type"      : last.sync_type
        }


@router.post("/dashboard/sync-now")
def sync_now(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    log = sync_from_graph(db, triggered_by="admin")
    return {
        "status":        log.status,
        "rows_fetched":  log.rows_fetched,
        "rows_inserted": log.rows_inserted,
        "rows_skipped":  log.rows_skipped,
        "error":         log.error_message,
        "synced_at":     log.synced_at,
    }

# ANALYTICS  

@router.get("/analytics/rating-breakdown")
def rating_breakdown(
    file_id: Optional[int] = None,
    year: Optional[str] = None,
    month: Optional[str] = None,
    attorney_code: Optional[str] = None,
    paralegal_code: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    q = db.query(
        func.avg(ExitInterview.rating_attorney_comm).label("avg_attorney_comm"),
        func.avg(ExitInterview.rating_paralegal_comm).label("avg_paralegal_comm"),
        func.avg(ExitInterview.rating_case_outcome).label("avg_case_outcome"),
            func.avg(ExitInterview.rating_overall).label("avg_overall"),
            func.count(ExitInterview.id).label("total"),
    )

    q = _apply_file_filter(q, file_id)

    q = _apply_filters(
        q,
        year,
        month,
        attorney_code,
        paralegal_code,
    )
    r = q.first()
    return {
        "avg_attorney_comm": round(r.avg_attorney_comm, 2) if r.avg_attorney_comm else None,
        "avg_paralegal_comm" : round(r.avg_paralegal_comm, 2) if r.avg_paralegal_comm else None,
        "avg_case_outcome"   : round(r.avg_case_outcome, 2)   if r.avg_case_outcome   else None,
        "avg_overall"     : round(r.avg_overall, 2)        if r.avg_overall        else None,
        "total"        : r.total,
    }


@router.get("/analytics/attorney-vs-paralegal")
def attorney_vs_paralegal(
    file_id: Optional[int] = None,
    year: Optional[str] = None,
    month: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    q = db.query(
        ExitInterview.year,
        ExitInterview.month,
        func.avg(ExitInterview.rating_attorney_comm).label("avg_attorney"),
        func.avg(ExitInterview.rating_paralegal_comm).label("avg_paralegal"),
        func.count(ExitInterview.id).label("count"),
    )
    q = _apply_file_filter(q, file_id)

    if year:  q = q.filter(ExitInterview.year  == year)
    if month: q = q.filter(ExitInterview.month == month)
    q = q.group_by(ExitInterview.year, ExitInterview.month)

    rows = q.all()
    rows.sort(key=lambda r: (r.year or "", MONTH_ORDER.get(r.month or "", 0)))

    return [
        {
            "year":         r.year,
            "month":        r.month,
            "period_label": _period_label(r.year or "", r.month or ""),
            "avg_attorney":  round(r.avg_attorney, 2)  if r.avg_attorney  else None,
            "avg_paralegal": round(r.avg_paralegal, 2) if r.avg_paralegal else None,
            "count":         r.count,
        }
        for r in rows
    ]


@router.get("/analytics/suggestions")
def suggestions(
    file_id: Optional[int] = None,
    year: Optional[str] = None,
    month: Optional[str] = None,
    attorney_code: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    q = _apply_filters(
        db.query(
            ExitInterview.id,
            ExitInterview.submitted_at,
            ExitInterview.case_name,
            ExitInterview.lead_attorney,
            ExitInterview.suggestions,
            ExitInterview.avg_rating,
        ).filter(ExitInterview.suggestions.isnot(None), ExitInterview.suggestions != ""),
        year, month, attorney_code, None,
    ).order_by(ExitInterview.submitted_at.desc()) 

    q = _apply_file_filter(q, file_id)

    return [
        {
            "id"         : r.id,
            "submitted_at" : r.submitted_at,
            "case_name"   : r.case_name,
            "lead_attorney" : r.lead_attorney,
            "suggestion "  : r.suggestions,
            "avg_rating"   : r.avg_rating,
        }
        for r in q.all()
    ]


@router.get("/analytics/filters/options")
def filter_options(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    years  = sorted([r[0] for r in db.query(ExitInterview.year).distinct().all()  if r[0]])
    months = [r[0] for r in db.query(ExitInterview.month).distinct().all() if r[0]]
    months.sort(key=lambda m: MONTH_ORDER.get(m, 99))

    attorneys = [
        {"code": r[0], "name": r[1]}
        for r in db.query(ExitInterview.attorney_code, ExitInterview.lead_attorney).distinct().all()
        if r[0]
    ]
    paralegals = [
        {"code": r[0], "name": r[1]}
        for r in db.query(ExitInterview.paralegal_code, ExitInterview.paralegal).distinct().all()
        if r[0]
    ]
    return {
        "years" : years, 
        "months" : months, 
        "attorneys" :attorneys, 
        "paralegals" : paralegals
    }


# SCORECARDS 

@router.get("/scorecards/attorneys")
def attorney_scorecards(
    file_id: Optional[int] = None,
    year: Optional[str] = Query(None),
    sort_by: str = Query("avg_overall"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    q = db.query(
        ExitInterview.attorney_code.label("code"),
        ExitInterview.lead_attorney.label("name"),
        func.count(ExitInterview.id).label("survey_count"),
        func.avg(ExitInterview.rating_attorney_comm).label("avg_comm"),
        func.avg(ExitInterview.rating_case_outcome).label("avg_outcome"),
        func.avg(ExitInterview.rating_overall).label("avg_overall"),
    ).filter(ExitInterview.attorney_code.isnot(None))
    if year: q = q.filter(ExitInterview.year == year)
    q = _apply_file_filter(q, file_id)
    q = q.group_by(ExitInterview.attorney_code, ExitInterview.lead_attorney)

    result = [
    {
        "name": r.name or r.code,
        "code": r.code,
        "survey_count": r.survey_count,
        "avg_comm": round(r.avg_comm, 2) if r.avg_comm else None,
        "avg_outcome": round(r.avg_outcome, 2) if r.avg_outcome else None,
        "avg_overall": round(r.avg_overall, 2) if r.avg_overall else None,
        "trend": None,
        "badge": _badge(r.avg_overall),
        }
        for r in q.all()
    ]

    total_surveys = sum(r["survey_count"] for r in result)

    if total_surveys:
        avg_comm = round(
            sum(r["avg_comm"] * r["survey_count"] for r in result)
            / total_surveys,
            2,
        )

        avg_outcome = round(
            sum(r["avg_outcome"] * r["survey_count"] for r in result)
            / total_surveys,
            2,
        )

        avg_overall = round(
            sum(r["avg_overall"] * r["survey_count"] for r in result)
            / total_surveys,
            2,
        )
    else:
        avg_comm = avg_outcome = avg_overall = 0

    valid_sort = {"avg_comm", "avg_outcome", 
    "avg_overall", "survey_count"}

    if sort_by in valid_sort:
        result.sort(
            key=lambda x: x.get(sort_by, 0) or 0,
            reverse=True
        )

    return {
        "rows": result,
        "totals": {
            "survey_count": total_surveys,
            "avg_comm": avg_comm,
            "avg_outcome": avg_outcome,
            "avg_overall": avg_overall,
        },
    }


@router.get("/scorecards/paralegals")
def paralegal_scorecards(
    file_id: Optional[int] = None,
    year: Optional[str] = Query(None),
    sort_by: str = Query("avg_overall"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    q = db.query(
        ExitInterview.paralegal_code.label("code"),
        ExitInterview.paralegal.label("name"),
        func.count(ExitInterview.id).label("survey_count"),
        func.avg(ExitInterview.rating_paralegal_comm).label("avg_comm"),
        func.avg(ExitInterview.rating_case_outcome).label("avg_outcome"),
        func.avg(ExitInterview.rating_overall).label("avg_overall"),
    ).filter(ExitInterview.paralegal_code.isnot(None))
    if year: q = q.filter(ExitInterview.year == year)
    q = _apply_file_filter(q, file_id)
    q = q.group_by(ExitInterview.paralegal_code, ExitInterview.paralegal)

    result = [
       {
            "name"         : r.name or r.code,
            "code"        : r.code,
            "survey_count" : r.survey_count,
            "avg_comm"     : round(r.avg_comm,    2) if r.avg_comm    else None,
            "avg_outcome": round(r.avg_outcome, 2) if r.avg_outcome else None,
            "avg_overall": round(r.avg_overall, 2) if r.avg_overall else None,
            "trend"       : None,
            "badge"        : _badge(r.avg_overall),
        }
        for r in q.all()
    ]

    total_surveys = sum(r["survey_count"] for r in result)

    if total_surveys:
        avg_comm = round(sum(r["avg_comm"] * r["survey_count"] for r in result)/ total_surveys,2,)
        avg_outcome = round(sum(r["avg_outcome"] * r["survey_count"] for r in result)/ total_surveys,2,)
        avg_overall = round(sum(r["avg_overall"] * r["survey_count"] for r in result)/ total_surveys,2,)
    else:
        avg_comm = avg_outcome = avg_overall = 0

    valid_sort = {"avg_comm","avg_outcome","avg_overall","survey_count"}

    if sort_by in valid_sort:
        result.sort(key=lambda x: x.get(sort_by, 0), reverse=True,)

    return {
        "rows": result,
        "totals": {
            "survey_count": total_surveys,
            "avg_comm": avg_comm,
            "avg_outcome": avg_outcome,
            "avg_overall": avg_overall,
        },
    }

@router.get("/google-review/survey-submission-summary")
def survey_submission_summary(
    year: str = Query(...),
    file_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    q = db.query(ExitInterview.google_review_status, ExitInterview.month, func.count(ExitInterview.id).label("count"))
    q = q.filter(ExitInterview.year == year)
    q = _apply_file_filter(q, file_id)
    q = q.group_by( ExitInterview.google_review_status, ExitInterview.month )
    rows = q.all()
    months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    statuses = [
        "Yes - Link sent",
        "No - Client Response (Link not sent)",
        "No - You determined not to send the link"
    ]
    data = {}
    for status in statuses:
        data[status] = {m: 0 for m in months}
        data[status]["Total"] = 0

    for row in rows:
        month = row.month[:3]
        if row.google_review_status in data:
            data[row.google_review_status][month] = row.count
            data[row.google_review_status]["Total"] += row.count

    month_totals = {}
    for m in months:
        month_totals[m] = sum(
            data[s][m]
            for s in statuses
        )

    month_totals["Total"] = sum(month_totals[m] for m in months)
    return {
        "year": year,
        "rows": [
            {
                "status": status,
                **data[status]
            }
            for status in statuses
        ],
        "totals": month_totals
    }

# REVIEWS  —  /reviews

def _build_review_query(
    db, date_from, date_to, attorney_code, paralegal_code,
    year, month, min_rating, max_rating, referral, google_review_status,file_id=None
):  
    q = db.query(ExitInterview)
    q = _apply_file_filter(q, file_id)
    if date_from:            q = q.filter(ExitInterview.submitted_at    >= date_from)
    if date_to:              q = q.filter(ExitInterview.submitted_at    <= date_to)
    if attorney_code:        q = q.filter(ExitInterview.attorney_code   == attorney_code)
    if paralegal_code:       q = q.filter(ExitInterview.paralegal_code  == paralegal_code)
    if year:                 q = q.filter(ExitInterview.year            == year)
    if month:                q = q.filter(ExitInterview.month           == month)
    if min_rating is not None: q = q.filter(ExitInterview.avg_rating   >= min_rating)
    if max_rating is not None: q = q.filter(ExitInterview.avg_rating   <= max_rating)
    if referral is not None: q = q.filter(ExitInterview.referral_flag  == referral)
    if google_review_status: q = q.filter(ExitInterview.google_review_status == google_review_status)
    return q


@router.get("/reviews/")
def list_reviews(
    file_id: Optional[int] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, le=200),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    attorney_code: Optional[str] = None,
    paralegal_code: Optional[str] = None,
    year: Optional[str] = None,
    month: Optional[str] = None,
    min_rating: Optional[float] = None,
    max_rating: Optional[float] = None,
    referral: Optional[bool] = None,
    google_review_status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    q     = _build_review_query(db, date_from, date_to, attorney_code, paralegal_code,
                                year, month, min_rating, max_rating, referral, google_review_status,file_id)
    total = q.count()
    rows  = q.order_by(ExitInterview.submitted_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total"     : total,
        "page"      : page,
        "page_size" : page_size,
        "pages"     : (total + page_size - 1) // page_size,
        "data"      : [rows for rows in rows],
    }


@router.get("/reviews/export-csv")
def export_csv(
    file_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    attorney_code: Optional[str] = None,
    paralegal_code: Optional[str] = None,
    year: Optional[str] = None,
    month: Optional[str] = None,
    referral: Optional[bool] = None,
    google_review_status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    q = _build_review_query(db, date_from, date_to, attorney_code, paralegal_code,
                            year, month, None, None, referral, google_review_status, file_id)
    rows = q.order_by(ExitInterview.submitted_at.desc()).all()

    fields = [
        "id", "submitted_at", "submitter_name", "case_name", "neos_id",
        "lead_attorney", "attorney_code", "paralegal", "paralegal_code",
        "rating_attorney_comm", "rating_paralegal_comm", "rating_case_outcome", "rating_overall",
        "avg_rating", "sentiment_flag", "willing_to_refer", "referral_flag",
        "google_review_status", "review_sent", "review_declined", "review_withheld",
        "suggestions", "month", "year"
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for r in rows:
        writer.writerow({f: getattr(r, f, "") for f in fields})
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=exit_interviews.csv"},
    )

# ALERTS  —  /alerts

@router.get("/alerts/")
def get_alerts(
    file_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    alerts = []
    # 1. Critical ratings (any score <= 2)
    critical_query = db.query(ExitInterview)

    critical_query = _apply_file_filter(
        critical_query,
        file_id
    )

    critical_reviews = (
        critical_query
        .filter(
            or_(
                ExitInterview.rating_attorney_comm <= 2,
                ExitInterview.rating_paralegal_comm <= 2,
                ExitInterview.rating_case_outcome <= 2,
                ExitInterview.rating_overall <= 2,
            )
        )
        .order_by(ExitInterview.submitted_at.desc())
        .all()
    )

    for r in critical_reviews:
        low_scores = []

        if r.rating_attorney_comm is not None and r.rating_attorney_comm <= 2:
            low_scores.append(
                f"Attorney Comm={r.rating_attorney_comm}"
            )

        if r.rating_paralegal_comm is not None and r.rating_paralegal_comm <= 2:
            low_scores.append(
                f"Paralegal Comm={r.rating_paralegal_comm}"
            )

        if r.rating_case_outcome is not None and r.rating_case_outcome <= 2:
            low_scores.append(
                f"Case Outcome={r.rating_case_outcome}"
            )

        if r.rating_overall is not None and r.rating_overall <= 2:
            low_scores.append(
                f"Overall={r.rating_overall}"
            )

        alerts.append({
            "id": r.id,
            "case_name": r.case_name,
            "submitted_at": r.submitted_at,
            "alert_type": "critical_rating",
            "severity": "critical",
            "detail": f"Critical score(s): {', '.join(low_scores)}",
            "avg_rating": r.avg_rating,
            "lead_attorney": r.lead_attorney,
            "paralegal": r.paralegal
        })

    seen_ids = {a["id"] for a in alerts}

    # 2. Low average ratings (< 3.5)
    low_avg_reviews = db.query(ExitInterview)

    low_avg_reviews = _apply_file_filter(
        low_avg_reviews,
        file_id
    )

    low_avg_reviews = (
        low_avg_reviews
        .filter(
            ExitInterview.avg_rating.isnot(None),
            ExitInterview.avg_rating < 3.5
        )
        .order_by(ExitInterview.submitted_at.desc())
        .all()
    )

    for r in low_avg_reviews:

        if r.id not in seen_ids:

            alerts.append({
                "id": r.id,
                "case_name": r.case_name,
                "submitted_at": r.submitted_at,
                "alert_type": "low_avg_rating",
                "severity": "warning",
                "detail": f"Avg rating {r.avg_rating} is below 3.5 threshold.",
                "avg_rating": r.avg_rating,
                "lead_attorney": r.lead_attorney,
                "paralegal": r.paralegal
            })

            seen_ids.add(r.id)

    # 3. Client declined referral and no review link sent
    declined_reviews = db.query(ExitInterview)

    declined_reviews = _apply_file_filter(
        declined_reviews,
        file_id
    )

    declined_reviews = (
        declined_reviews
        .filter(
            ExitInterview.referral_flag == False,
            ExitInterview.review_sent == False,
            ExitInterview.willing_to_refer.isnot(None)
        )
        .order_by(ExitInterview.submitted_at.desc())
        .all()
    )

    for r in declined_reviews:

        if r.id not in seen_ids:

            alerts.append({
                "id": r.id,
                "case_name": r.case_name,
                "submitted_at": r.submitted_at,
                "alert_type": "declined_no_link",
                "severity": "warning",
                "detail": "Client declined referral and no review link was sent.",
                "avg_rating": r.avg_rating,
                "lead_attorney": r.lead_attorney,
                "paralegal": r.paralegal
            })

            seen_ids.add(r.id)

    # Sort Critical First, then newest
    alerts.sort(
        key=lambda a: (
            a["severity"] == "critical",
            a["submitted_at"] or ""
        ),
        reverse=True
    )

    return alerts

# GOOGLE REVIEW PIPELINE  —  /google-review
@router.get("/analytics/survey-count-by-submitter")
def survey_count_by_submitter(
    year: str = Query(...),
    file_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    q = db.query(ExitInterview.submitter_name,ExitInterview.month,func.count(ExitInterview.id).label("count"))
    q = q.filter(ExitInterview.year == year)
    q = _apply_file_filter(q, file_id)
    q = q.group_by(ExitInterview.submitter_name, ExitInterview.month)
    rows = q.all()
    month_order = ["Jan", "Feb", "Mar", "Apr","May", "Jun", "Jul", "Aug","Sep", "Oct", "Nov", "Dec"]
    # Get all submitters dynamically
    submitters = sorted(list(set(r.submitter_name for r in rows
                if r.submitter_name)))

    table = {}
    for month in month_order:
        table[month] = {s: 0 for s in submitters}
        table[month]["Total"] = 0

    for row in rows:
        month = row.month[:3]
        if month in table:
            table[month][row.submitter_name] = row.count
            table[month]["Total"] += row.count

    # Column totals
    column_totals = {}
    for submitter in submitters:
        column_totals[submitter] = sum(table[m][submitter] for m in month_order)

    column_totals["Total"] = sum(column_totals[s] for s in submitters)

    response_rows = []

    for month in month_order:
        if table[month]["Total"] == 0:
            continue
        row = {"month": month}
        for submitter in submitters:
            row[submitter] = table[month][submitter]
        row["Total"] = table[month]["Total"]
        response_rows.append(row)

    return {
        "year": year,
        "submitters": submitters,
        "rows": response_rows,
        "totals": column_totals
    }

@router.get("/google-review/pipeline-cards")
def pipeline_cards(
    file_id: Optional[int] = None,
    year: Optional[str] = Query(None),
    month: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    q = db.query(ExitInterview)

    q = _apply_file_filter(q, file_id)
    if year:  q = q.filter(ExitInterview.year  == year)
    if month: q = q.filter(ExitInterview.month == month)
    rows = q.all()

    total       = len(rows)
    links_sent  = sum(1 for r in rows if r.review_sent)

    return {
        "links_sent"         : links_sent,
        "pending_followup"   : links_sent,               # same as sent — follow-up not yet tracked
        "client_declined"    : sum(1 for r in rows if r.review_declined),
        "withheld"           : sum(1 for r in rows if r.review_withheld),
        "total"              : total,
        "conversion_rate_pct" : round(links_sent / total * 100, 1) if total else None,
    }

@router.get("/google-review/link-sent-cases")
def link_sent_cases(
    file_id: Optional[int] = None,
    year: Optional[str] = Query(None),
    month: Optional[str] = Query(None),
    attorney_code: Optional[str] = Query(None),
    paralegal_code: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    q = db.query(ExitInterview).filter(
    ExitInterview.review_sent == True
    )

    q = _apply_file_filter(q, file_id)

    if year:
        q = q.filter(ExitInterview.year == year)

    if month:
        q = q.filter(ExitInterview.month == month)

    if attorney_code:
        q = q.filter(ExitInterview.attorney_code == attorney_code)

    if paralegal_code:
        q = q.filter(ExitInterview.paralegal_code == paralegal_code)

    return q.order_by(ExitInterview.submitted_at.desc()).all() 

@router.get("/uploaded-files")
def get_uploaded_files(
    db: Session = Depends(get_db)
):
    files = (
        db.query(CustomServiceFile)
        .order_by(CustomServiceFile.uploaded_at.desc())
        .all()
    )

    return [
        {
            "file_id": f.id,
            "file_name": f.file_name,
            "rows_fetched": f.rows_fetched,
            "uploaded_by": f.uploaded_by,
            "uploaded_at": f.uploaded_at,
        }
        for f in files
    ] 