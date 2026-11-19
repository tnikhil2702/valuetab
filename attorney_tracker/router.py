# router.py
from datetime import datetime, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func

from attorney_tracker.utils import (
    DEFAULT_THRESHOLD,
    FORMULA_VERSION,
    KNOWN_DIVISIONS,
    MONTHLY_METRIC_ALIASES,
    THRESHOLD_OPTIONS,
    approve_upload,
    create_manual_record,
    delete_upload_any,
    generate_analysis,
    get_invalid_records_for_upload,
    get_uploads_any,
    lock_upload,
    process_monthly_metrics,
    process_quickbook_upload,
    process_raw_settlements,
    process_upload_auto,
    update_record_any,
)
from auth_utils import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    SessionLocal,
    authenticate_user,
    create_access_token,
    create_password,
    generate_otp,
    get_current_user,
    reset_password,
    verify_otp,
)
from db import (
    ActiveSession,
    AttorneyTrackerRecord,
    Session,
    Whitelist,
    get_db,
)

router = APIRouter()
PROJECT_NAME = "Attorney_tracker"
jinja_templates = Jinja2Templates(directory="attorney_tracker/templates")


# UI
@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return jinja_templates.get_template("index.html").render({"request": request})


# Auth
@router.post("/login")
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = await authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    access_token = await create_access_token(
        data={"sub": user["email"], "role": user["role"]},
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
    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/verify-email")
async def verify_email(email: str):
    db = SessionLocal()
    try:
        user = db.query(Whitelist).filter(Whitelist.email == email.lower()).first()
        if not user:
            raise HTTPException(status_code=404, detail="Email not authorized")
        return {"valid": True, "password_created": bool(user.password_hash), "role": user.role}
    finally:
        db.close()


@router.post("/send/otp")
async def send_otp_api(email: str):
    return await generate_otp(email)


@router.post("/verify/otp")
async def verify_otp_api(email: str, otp: str):
    await verify_otp(email, otp)
    return {"message": "OTP verified successfully"}


@router.post("/create/password")
async def create_password_api(email: str, password: str):
    return await create_password(email, password)


@router.post("/forgot/password")
async def forgot_password(email: str):
    return await generate_otp(email)


@router.post("/reset/password")
async def reset_password_api(email: str, otp: str, new_password: str):
    return await reset_password(email, otp, new_password)


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


# Meta
@router.get("/threshold-options")
async def get_threshold_options(current_user: dict = Depends(get_current_user)):
    return {
        "default_threshold": DEFAULT_THRESHOLD,
        "options": THRESHOLD_OPTIONS,
        "formula_version": FORMULA_VERSION,
    }

@router.get("/divisions")
async def get_divisions(current_user: dict = Depends(get_current_user)):
    return {"divisions": sorted(KNOWN_DIVISIONS)}

# Upload routes
@router.post("/upload")
async def upload_excel(
    file: UploadFile = File(...),
    threshold: float = Query(
        DEFAULT_THRESHOLD,
        description="Very-large-case threshold to apply during upload parsing.",
    ),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    contents = await file.read()
    return await run_in_threadpool(
        process_upload_auto,
        contents=contents,
        filename=file.filename,
        db=db,
        threshold=threshold,
    )

@router.post("/upload/monthly-metrics")
async def upload_monthly_metrics(
    file: UploadFile = File(...),
    threshold: float = Query(
        DEFAULT_THRESHOLD,
        description="Very-large-case threshold to apply during upload parsing.",
    ),
    db: Session = Depends(get_db),
    # current_user: dict = Depends(get_current_user),
):

    contents = await file.read()
    return await run_in_threadpool(
        process_monthly_metrics,
        contents=contents,
        filename=file.filename,
        db=db,
        threshold=threshold,
    )

@router.post("/upload/case-level")
async def upload_case_level(
    file: UploadFile = File(...),
    threshold: float = Query(
        DEFAULT_THRESHOLD,
        description="Very-large-case threshold to apply during upload parsing.",
    ),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
   
    contents = await file.read()
    return await run_in_threadpool(
        process_raw_settlements,
        contents=contents,
        filename=file.filename,
        db=db,
        threshold=threshold,
    )

@router.post("/upload/quickbook")
async def upload_quickbook(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    contents = await file.read()
    return await run_in_threadpool(
        process_quickbook_upload,
        contents=contents,
        filename=file.filename,
        db=db,
    )


# Uploads listing / delete — shared across attorney-tracker + QuickBooks

@router.get("/uploads")
async def get_uploads(
    upload_id: Optional[int] = None,
    source: Optional[Literal["tracker", "quickbook"]] = Query(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
   
    return await run_in_threadpool(get_uploads_any, db=db, upload_id=upload_id, source=source)


@router.delete("/uploads/{upload_id}")
async def delete_upload(
    upload_id: int,
    source: Optional[Literal["tracker", "quickbook"]] = Query(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return await run_in_threadpool(delete_upload_any, upload_id=upload_id, db=db, source=source)

# Approve / Lock — shared across attorney-tracker + QuickBooks

@router.post("/uploads/{upload_id}/approve")
async def approve_uploaded_file(
    upload_id: int,
    source: Optional[Literal["tracker", "quickbook"]] = Query(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return await run_in_threadpool(approve_upload, upload_id=upload_id, db=db, source=source)


@router.post("/uploads/{upload_id}/lock")
async def lock_uploaded_file(
    upload_id: int,
    source: Optional[Literal["tracker", "quickbook"]] = Query(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return await run_in_threadpool(lock_upload, upload_id=upload_id, db=db, source=source)

# Analysis — shared across attorney-tracker + QuickBooks
@router.get("/uploads/{upload_id}/analysis")
async def get_analysis(
    upload_id: int,
    threshold: float = Query(DEFAULT_THRESHOLD),
    status_filter: Literal["approved", "pending", "locked", "all"] = Query("approved"),
    source: Optional[Literal["tracker", "quickbook"]] = Query(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
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

# Invalid records 
@router.get("/uploads/{upload_id}/invalid-records")
async def get_invalid_records(
    upload_id: int,
    source: Optional[Literal["tracker", "quickbook"]] = Query(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):

    return await run_in_threadpool(
        get_invalid_records_for_upload,
        upload_id=upload_id,
        db=db,
        source=source,
    )


@router.delete("/invalid-records/{record_id}")
async def delete_invalid_record(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    record = (
        db.query(AttorneyTrackerRecord)
        .filter(
            AttorneyTrackerRecord.id == record_id,
            AttorneyTrackerRecord.is_valid == False,
        )
        .first()
    )

    if not record:
        raise HTTPException(
            status_code=404,
            detail="Record not found"
        )

    # Optional safety check
    if record.is_valid:
        raise HTTPException(
            status_code=400,
            detail="Only invalid records can be deleted"
        )

    db.delete(record)
    db.commit()

    return {
        "success": True,
        "message": f"Invalid record {record_id} deleted successfully"
    } 

# Record update
class RecordUpdatePayload(BaseModel):
    # Shared / identity
    attorney_name: Optional[str] = None
    division_name: Optional[str] = None
    reporting_month: Optional[str] = None
    notes: Optional[str] = None

    # Case-level fields
    case_id: Optional[str] = None
    settlement_date: Optional[str] = None
    settlement_value: Optional[float] = None
    legal_fee_value: Optional[float] = None    # NEW v2
    case_type: Optional[str] = None            # NEW v2

    # Monthly metric fields
    year: Optional[int] = None                 # NEW v2
    month_number: Optional[int] = None         # NEW v2
    legal_fees_collected: Optional[float] = None
    cases_distributed: Optional[int] = None
    monthly_fee_reduction: Optional[float] = None
    active_caseload_snapshot: Optional[int] = None
    total_settlement_value: Optional[float] = None
    adjusted_value_excluding_large_cases: Optional[float] = None
    large_case_count: Optional[int] = None
    large_case_value: Optional[float] = None

    # QuickBooks transaction fields (record_id resolved against
    attorney: Optional[str] = None
    office: Optional[str] = None
    class_: Optional[str] = None
    case_type_description: Optional[str] = None
    date: Optional[str] = None       # YYYY-MM-DD
    credit: Optional[float] = None
    debit: Optional[float] = None
    memo: Optional[str] = None
    case_number: Optional[str] = None

    model_config = {"extra": "ignore"}


@router.patch("/records/{record_id}")
async def patch_record(
    record_id: int,
    payload: RecordUpdatePayload,
    source: Optional[Literal["tracker", "quickbook"]] = Query(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return await run_in_threadpool(
        update_record_any,
        db=db,
        record_id=record_id,
        payload=payload,
        source=source,
    )

# Manual
class ManualEntryPayload(BaseModel):
    case_id: str
    settlement_date: str       # YYYY-MM-DD
    reporting_month: str       # YYYY-MM
    attorney_name: str
    division_name: str
    settlement_value: float
    notes: Optional[str] = None


@router.post("/records/manual-entry")
async def manual_entry(
    payload: ManualEntryPayload,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return await run_in_threadpool(create_manual_record, db=db, payload=payload)