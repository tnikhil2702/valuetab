from fastapi import APIRouter, Depends, HTTPException, Request, Query, Body, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql import exists
from db import get_db, ActiveSession, Session, Whitelist, AnalyticsReport, AnalyticsExecutiveSummary, AnalyticsWeeklyPerformance
from auth_utils import authenticate_user, create_access_token, ACCESS_TOKEN_EXPIRE_MINUTES, get_current_user,  reset_password, generate_otp, verify_otp, SessionLocal, create_password
from sqlalchemy import func
from fastapi.security import OAuth2PasswordRequestForm
from datetime import datetime, timezone
from fastapi.templating import Jinja2Templates
from intake_manager.analytics import weekly_performance, executive_summary
from datetime import date
import shutil
import tempfile
import os 
from datetime import timedelta
from typing import Optional, List

PROJECT_NAME = "Intake_Manager"
router = APIRouter()

jinja_templates = Jinja2Templates(directory="intake_manager/templates")

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

#  ------------ analytics report generator ----------------- 
def row_to_dict(obj):
    return {
        c.name: getattr(obj, c.name)
        for c in obj.__table__.columns
    }


@router.post("/executive-summary/import")
def upload_executive_summary(
    month: int = Form(...),
    year: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)):

    with tempfile.NamedTemporaryFile(
        delete=False,
        suffix=".xlsx",
    ) as tmp:

        shutil.copyfileobj(file.file, tmp)

        tmp_path = tmp.name

    try:

        report = executive_summary(
            db,
            tmp_path,
            month,
            year,
        )

    finally:
        os.remove(tmp_path)

    return row_to_dict(report)


@router.post("/weekly-performance/import")
def upload_weekly_performance(
    month: int = Form(...),
    year: int = Form(...),
    week_number: int = Form(...),
    week_start: Optional[date] = Form(None),
    week_end: Optional[date] = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):

    with tempfile.NamedTemporaryFile(
        delete=False,
        suffix=".xlsx",
    ) as tmp:

        shutil.copyfileobj(file.file, tmp)

        tmp_path = tmp.name

    try:

        report = weekly_performance(
            db,
            tmp_path,
            month,
            year,
            week_number,
            week_start,
            week_end,
        )

    finally:
        os.remove(tmp_path)

    return row_to_dict(report) 


def _get_report(db: Session, month: int, year: int) -> AnalyticsReport | None:
    return (
        db.query(AnalyticsReport)
        .filter(
            AnalyticsReport.month == month,
            AnalyticsReport.year == year
        )
        .order_by(AnalyticsReport.id.desc())
        .first()
    )
 
 
def _summary_rows(db: Session, report_id: int):
    return (
        db.query(AnalyticsExecutiveSummary)
        .filter(AnalyticsExecutiveSummary.report_id == report_id)
        .all()
    )
 
 
def _weekly_rows(db: Session, report_id: int, week_number: int | None = None):
    q = db.query(AnalyticsWeeklyPerformance).filter(
        AnalyticsWeeklyPerformance.report_id == report_id
    )
    if week_number is not None:
        q = q.filter(AnalyticsWeeklyPerformance.week_number == week_number)
    return q.all()
 

def _employee_rows(rows, section):
    """
    Get employee rows under a section.
    """

    headers = [
        "Live",
        "Online",
        "Reception",
        "AVI",
        "MED",
        "WCC",
        "Grand Total"
    ]

    found = False
    result = []

    for row in rows:

        if row.team == section:
            found = True
            continue

        if found:

            if row.team in headers:
                break

            result.append(row)

    return result

# ── chart builders ────────────────────────────────────────────────────────────
 
def _build_live_charts(rows):

    live_rows = _employee_rows(rows, "Live")

    return {

        "answered_calls": [
            {
                "team": r.team,
                "total_answered": r.total_calls or 0,
                "answered_rate": r.overall_rate or 0
            }
            for r in live_rows
        ],

        "missed_calls": [
            {
                "team": r.team,
                "total_missed":
                    (r.mva22_total or 0) - (r.mva22_signup or 0),

                "missed_rate":
                    round(
                        1 - r.mva22_rate,
                        6
                    )
                    if r.mva22_rate
                    else 0
            }
            for r in live_rows
        ],


        "outbound_calls": [
            {
                "team": r.team,
                "total": r.total_calls or 0
            }
            for r in live_rows
        ],


        "mva_signup": [
            {
                "team": r.team,
                "total": r.mva22_signup or 0
            }
            for r in live_rows
        ],


        "mva_rate": [
            {
                "team": r.team,
                "total": r.mva22_rate or 0
            }
            for r in live_rows
        ]
    }
 
def _build_online_charts(rows):

    online_rows = _employee_rows(rows, "Online")


    return {

        "outbound_calls": [
            {
                "team": r.team,
                "total": r.total_calls or 0
            }
            for r in online_rows
        ],


        "mva_signup": [
            {
                "team": r.team,
                "total": r.mva22_signup or 0
            }
            for r in online_rows
        ],


        "mva_rate": [
            {
                "team": r.team,
                "total": r.mva22_rate or 0
            }
            for r in online_rows
        ]
    }
 
# ── API: monthly dashboard ────────────────────────────────────────────────────
 
@router.get("/dashboard/monthly")
def monthly(month: int, year: int, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    report = _get_report(db, month, year)
    if not report:
        return {"error": f"No report found for {month}/{year}"}
 
    rows = _summary_rows(db, report.id)
 
    return {
        "report_id": report.id,
        "month": report.month,
        "year": report.year,
        "live": _build_live_charts(rows),
        "online": _build_online_charts(rows)
    }
 
 
# ── API: weekly dashboard ─────────────────────────────────────────────────────
 
@router.get("/dashboard/weekly")
def weekly(
    week_start: date = Query(...),
    week_end: date = Query(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    rows = (
        db.query(AnalyticsWeeklyPerformance)
        .filter(
            AnalyticsWeeklyPerformance.week_start == week_start,
            AnalyticsWeeklyPerformance.week_end == week_end,
        )
        .all()
    )

    if not rows:
        return {
            "error": "No weekly data found for the given date range."
        }

    report = (
        db.query(AnalyticsReport)
        .filter(
            AnalyticsReport.id == rows[0].report_id
        )
        .first()
    )

    return {
        "report_id": report.id,
        "month": report.month,
        "year": report.year,
        "week": {
            "week_number": rows[0].week_number,
            "week_start": rows[0].week_start,
            "week_end": rows[0].week_end,
        },
        "live": _build_live_charts(rows),
        "online": _build_online_charts(rows)
    }