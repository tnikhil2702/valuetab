from fastapi import APIRouter,Header, Depends, HTTPException, Request, Query, Body, UploadFile, File, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql import exists
from db import get_db, LeadIntake, ActiveSession, ApiErrorLog, IntakeSpecialist, Session, Referral, LeadReferral, Whitelist,AgreementSignedUnmatched
from auth_utils import authenticate_user, create_access_token, ACCESS_TOKEN_EXPIRE_MINUTES, get_current_user, verify_lead_token, reset_password, generate_otp, create_lead_token, verify_otp, SessionLocal, create_password
from sqlalchemy import func, extract, and_, or_, not_, case
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.templating import Jinja2Templates
from datetime import timedelta, date, datetime, timezone
from MVA_Platform.outlook_utils import send_email, get_assigned_users, normalize_state_of_incident, process_lead_reviews, apply_agreement_sent, process_neos_update_for_leads, clean_lead_name
from MVA_Platform.lead_utils import derive_agree_disagree, record_reviewer_answer, apply_agent_status, agreement_summary, backfill_reviewer_decision
from MVA_Platform.email_templates import build_approved_email, build_declined_email, build_escalated_email, build_declined_with_referrals_email
from MVA_Platform.neos_service import get_lead_status
from MVA_Platform.neos_direct import ensure_lead_can_email, lead_emails_blocked, is_neos_direct, not_neos_direct_clause
from admin.router import admin_router
from pydantic import EmailStr, ValidationError, TypeAdapter, BaseModel, Field, ConfigDict
from typing import Optional, List
import pandas as pd
from dateutil.relativedelta import relativedelta
from collections import defaultdict
from openai import AsyncOpenAI
import json
import os
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from typing import Literal
from MVA_Platform.owl_service import (
    prepare_owl_run,
    start_owl_run_task,
    request_stop_owl_run,
    refresh_owl_runs,
    owl_status,
    owl_run_status,
    verify_edit_code,
    apply_owl_override,
    load_owl_reviews,
    owl_fields,
    denver_date_bounds_utc,
    user_email
)
import hmac
import secrets
from pathlib import Path


load_dotenv()
router = APIRouter()

PROJECT_NAME = "IntakeOps"
BASE_ACCEPT_URL = "https://lxintel.net/intakeops/accept/lead"
# BASE_ACCEPT_URL = "https://slimy-leonia-kyphotic.ngrok-free.dev/intakeops/accept/lead"

CASE_REVIEW_MAILBOX = "casereview@ramoslaw.com"
REFERRALS_MAILBOX = "referralsteam@ramoslaw.com"

jinja_templates = Jinja2Templates(directory="MVA_Platform/templates")

@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return jinja_templates.get_template("index.html").render({"request": request})


@router.get("/lang-engine.js", response_class=FileResponse)
async def lang_engine():
    script_path = Path(__file__).resolve().parent / "templates" / "lang-engine.js"
    return FileResponse(script_path, media_type="application/javascript")


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

@router.get("/leads")
async def get_leads(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    # leads = db.query(LeadIntake).all()
    fourteen_days_ago = datetime.utcnow() - timedelta(days=14) # fetch last 14 days leads from today
    query = db.query(LeadIntake, exists().where(LeadReferral.lead_id == LeadIntake.id).label("is_referred"))
    query = query.filter(LeadIntake.received_at >= fourteen_days_ago)
    leads = query.order_by(LeadIntake.received_at.desc()).all()
    owl_by_lead = load_owl_reviews(db, [l.id for l, _ in leads])
    # keila_by_lead = load_keila_reviews(
    #     db,
    #     [lead.id for lead, _ in leads]
    # )
 
    result = []
    for lead, is_referred in leads:
        owl = owl_by_lead.get(lead.id)        
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
            "is_referral_linked": is_referred,
            "neos_updated":lead.neos_updated,
            "neos_case_number":lead.neos_case_number,
            "neos_status": lead.neos_status,
            "neos_assigned_to": lead.assigned_to,
            "agreement_sent": lead.agreement_sent,
            "agreement_sent_at": lead.agreement_sent_at,
            "agreement_signed": lead.agreement_signed,
            "agreement_signed_at": lead.agreement_signed_at,
            "agreement_signed_neos_number": lead.agreement_signed_neos_number,
            "agreement_signed_needs_review": lead.agreement_signed_needs_review,
            "agree_disagree": derive_agree_disagree(lead),
            "reviewer_decision": lead.reviewer_decision,
            "reviewer_answer_at": lead.reviewer_answer_at,
            "agent_status_at_answer": lead.agent_status_at_answer,
            "agent_version_at_answer": lead.agent_version_at_answer,
            "intake_channel": lead.intake_channel or "intakeops",
            "neos_direct": is_neos_direct(lead),
            "neos_intake_id": lead.neos_intake_id,
            "scoring_status": lead.scoring_status,
            "send_emails": not lead_emails_blocked(lead),
            "write_neos_notes": bool(lead.write_neos_notes) and not is_neos_direct(lead),
            "link_review": bool(lead.link_review),
            "link_candidate_lead_ids": lead.link_candidate_lead_ids,
            "neos_intake_meta": lead.neos_intake_meta,
            **owl_fields(owl),
            # **keila_fields(keila_by_lead.get(lead.id)),
        })
    return result

@router.patch("/{lead_id}/status")
async def update_status(
    lead_id: int,
    status: str,
    reviewer_note: str = None,
    reviewer_name: str = None,
    escalator_email: str = None,
    decline_type: str = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    lead = db.query(LeadIntake).filter(LeadIntake.id == lead_id).first()

    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    if lead.email_sent:
        raise HTTPException(
            status_code=400,
            detail="Status cannot be changed after email has been sent."
        )

    valid_status = ["proceed", "declined", "escalated"]
    VALID_DECLINE_TYPES = ["declined_refer_out", "declined_no_referral"]
    if status not in valid_status:
        raise HTTPException(status_code=400, detail="Invalid status")

    if status == "declined":
        if not decline_type:
            raise HTTPException(
                status_code=400,
                detail="decline_type is required when status is declined"
            )

        if decline_type not in VALID_DECLINE_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid decline_type. Must be one of {VALID_DECLINE_TYPES}"
            )

    reviewer = reviewer_name or current_user["email"]

    if status == "escalated" and escalator_email:
        ensure_lead_can_email(lead)  # Neos Direct: no escalation email either
        if not validate_lead_email(escalator_email):
            raise HTTPException(status_code=400, detail="Escalator email is invalid")

        cleaned_escalator_email = escalator_email.strip().lower()
        subject = f"Lead Escalation - {lead.first_name or ''} {lead.last_name or ''}".strip()
        body = build_escalated_email({
            "first_name": lead.first_name,
            "last_name": lead.last_name,
            "email": lead.email,
            "phone": lead.phone,
            "case_type": lead.case_type,
            "state_of_incident": lead.state_of_incident,
            "accident_date": lead.accident_date,
            "case_description": lead.case_description
        }, reviewer_note or "", reviewer)

        # Send an escalation email only when an escalator email is provided.
        # Do not commit the escalated state if the email provider rejects delivery.
        try:
            send_email(
                CASE_REVIEW_MAILBOX,
                cleaned_escalator_email,
                subject,
                body
            )
        except Exception as exc:
            db.rollback()
            raise HTTPException(
                status_code=502,
                detail=f"Escalation email could not be sent: {str(exc)}"
            ) from exc

        lead.assigned_users = [cleaned_escalator_email]
        lead.email_sent = True
        lead.lead_email_sent_at = datetime.utcnow()

    apply_agent_status(
        db, lead, status, decline_type, reviewer_note, reviewer,
        prompt_version="manual",
    )
    await clean_lead_name(lead)

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "message": "Status updated",
        "lead_id": lead_id,
        "new_status": status,
        "reason": reviewer_note
    }


# helper to validate email format
def validate_lead_email(email: str) -> bool:
    if not email:
        return False
    try:
        email_adapter.validate_python(email.strip())
        return True
    except Exception:
        return False
    
@router.patch("/leads/{lead_id}/email")
async def update_lead_email(
    lead_id: int,
    email: str = Body(..., embed=True),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    lead = db.query(LeadIntake).filter(
        LeadIntake.id == lead_id
    ).first()

    if not lead:
        raise HTTPException(
            status_code=404,
            detail="Lead not found"
        )

    cleaned_email = email.strip().lower()

    # validate email
    if not validate_lead_email(cleaned_email):
        raise HTTPException(
            status_code=400,
            detail="Invalid email address"
        )

    old_email = lead.email

    lead.email = cleaned_email

    db.commit()
    db.refresh(lead)

    return {
        "message": "Lead email updated successfully",
        "lead_id": lead.id,
        "old_email": old_email,
        "new_email": lead.email,
        "updated_by": current_user["email"]
    }

@router.post("/{lead_id}/send-email")
async def send_lead_email(
    lead_id: int,
    practice_area: Optional[str] = Query(None),
    assigned_group: Optional[int] = Query(None),
    custom_emails: Optional[List[str]] = Query(None),
    custom_email_body: Optional[str] = Body(None, embed=True),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    lead = db.query(LeadIntake).filter(LeadIntake.id == lead_id).first()

    if not lead:
        raise HTTPException(404, "Lead not found")

    ensure_lead_can_email(lead)  # server-side block for Neos Direct (send_emails = false)

    if lead.status not in ["proceed", "declined"]:
        raise HTTPException(400, "Invalid status")

    if lead.email_sent:
        raise HTTPException(400, "Email already sent")

    if not lead.reviewed_by:
        raise HTTPException(400, "Lead must be reviewed first")

    lead_data = {
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "email": lead.email,
        "phone": lead.phone,
        "case_type": lead.case_type,
        "state_of_incident": lead.state_of_incident,
        "accident_date": lead.accident_date,
        "case_description": lead.case_description
    }

    reason = lead.reviewer_note
    reviewer_email = lead.reviewed_by

    try:
        if lead.status == "proceed":

            # Validate assignment OR custom emails
            has_assignment = practice_area and assigned_group
            has_custom_emails = (
                custom_emails and len(custom_emails) > 0
            )

            if not has_assignment and not has_custom_emails:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Provide either practice_area + assigned_group "
                        "or custom_emails"
                    )
                )

            if has_assignment:

                if assigned_group not in [1, 2]:
                    raise HTTPException(
                        status_code=400,
                        detail="assigned_group must be 1 or 2"
                    )

                assigned_users = await get_assigned_users(practice_area, assigned_group, db)

                if not assigned_users:
                    raise HTTPException(
                        status_code=400,
                        detail="No assigned users found"
                    )

                # Save assignment info
                lead.practice_area = practice_area
                lead.assigned_group = assigned_group
                lead.assigned_users = assigned_users

                recipient_emails = assigned_users

            else:

                # Remove duplicates + empty values
                recipient_emails = list({
                    email.strip()
                    for email in custom_emails
                    if email and email.strip()
                })

                if not recipient_emails:
                    raise HTTPException(
                        status_code=400,
                        detail="No valid custom emails provided"
                    )

                if len(recipient_emails) > 3:
                    raise HTTPException(
                        status_code=400,
                        detail="Maximum 3 custom emails allowed"
                    )
                
                lead.assigned_users = recipient_emails

            for user_email in recipient_emails:

                token = await create_lead_token(lead.id, user_email)
                accept_url = f"{BASE_ACCEPT_URL}?token={token}"

                subject = f"Approved Lead - {lead.first_name}"
                default_body = build_approved_email(lead_data, reason, reviewer_email, accept_url)

                body = (
                    custom_email_body
                    if custom_email_body
                    else default_body
                )

                # Inject accept button if missing
                if custom_email_body:
                    if accept_url not in body:

                        button_html = f"""
                        <br><br>
                        <a href="{accept_url}"style="background:#28a745; color:white; padding:14px 22px;
                            text-decoration:none;
                            border-radius:6px;
                            font-size:16px;
                            display:inline-block;
                        ">
                            Accept This Intake
                        </a>

                        """
                        # inject before </body>
                        if "</body>" in body:
                            body = body.replace(
                                "</body>",
                                f"{button_html}</body>"
                            )
                        else:
                            body += button_html

                send_email(CASE_REVIEW_MAILBOX, user_email, subject, body)

        elif lead.status == "declined":
            has_custom_emails = bool(custom_emails)
            if not has_custom_emails and not validate_lead_email(lead.email):
                raise HTTPException(
                    status_code=400,
                    detail="Lead email is missing or invalid. Update it or provide custom_emails."
                )

            recipient_emails = []
            if has_custom_emails:
                for raw_email in custom_emails:
                    cleaned = (raw_email or "").strip().lower()
                    if not validate_lead_email(cleaned):
                        raise HTTPException(status_code=400, detail=f"Invalid custom email address: {raw_email}")
                    if cleaned not in recipient_emails:
                        recipient_emails.append(cleaned)
                if len(recipient_emails) > 3:
                    raise HTTPException(status_code=400, detail="Maximum 3 custom emails allowed")
            else:
                recipient_emails = [lead.email.strip().lower()]

            subject = "Regarding Your Case Evaluation Request"
            if lead.decline_type == "declined_refer_out":
                referrals = db.query(LeadReferral).options(
                    joinedload(LeadReferral.referral)
                ).filter(
                    LeadReferral.lead_id == lead.id
                ).all()

                if not referrals:
                    raise HTTPException(400, "No referrals attached for this lead")

                body = custom_email_body if custom_email_body else build_declined_with_referrals_email(lead_data, referrals)
            else:
                body = custom_email_body if custom_email_body else build_declined_email(lead_data)
            from_email = (REFERRALS_MAILBOX if lead.decline_type == "declined_refer_out" else CASE_REVIEW_MAILBOX)
            for recipient_email in recipient_emails:
                send_email(from_email, recipient_email, subject, body)
            lead.assigned_users = recipient_emails if has_custom_emails else lead.email

        lead.email_sent = True
        lead.lead_email_sent_at = datetime.utcnow()
        db.commit()

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(502, f"Failed to send email: {str(e)}")

    return {"message": "Email sent successfully"}


@router.get("/accept/lead", response_class=HTMLResponse)
async def accept_lead(token: str, db: Session = Depends(get_db)):
    payload = await verify_lead_token(token)

    if not payload:
        return "<h2>Invalid or expired link</h2>"

    lead_id = payload.get("lead_id")
    user_email = payload.get("email")

    lead = db.query(LeadIntake).filter(LeadIntake.id == lead_id).first()

    if user_email not in (lead.assigned_users or []):
        return "<h2>You are not authorized to accept this lead</h2>"

    if not lead:
        return "<h2>Lead not found</h2>"

    if lead.lead_accepted:
        return f"<h2>Already accepted by {lead.lead_accepted_by}</h2>"

    # SAVE WHO ACCEPTED
    lead.lead_accepted = True
    lead.lead_accepted_by = user_email 
    lead.lead_accepted_at = datetime.utcnow()

    # CALCULATE RESPONSE TIME
    if lead.lead_email_sent_at:
        diff = datetime.utcnow() - lead.lead_email_sent_at
        lead.lead_accept_time_seconds = int(diff.total_seconds())

    db.commit()

    return f"<h2>Lead {lead_id} accepted by {user_email}</h2>"

@router.get("/intake_specialists")
async def get_intake_specialists(
    practice_area: str = Query(..., description="MVA, GPI, CONSUMER"),
    group: int = Query(..., description="Group number (1-6)"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    practice_area = practice_area.upper()
    if not (1 <= group <= 6):
        raise HTTPException(400, "Group must be between 1 and 6")

    start = ((group - 1) * 3) + 1
    end = group * 3

    users = db.query(IntakeSpecialist).filter(
        IntakeSpecialist.practice_area == practice_area,
        IntakeSpecialist.position.between(start, end)
    ).order_by(IntakeSpecialist.position).all()

    return {
        "practice_area": practice_area,
        "group": group,
        "users": [u.email for u in users]
    }

@router.get("/intake_practice_area")
async def get_intake_specialists(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    users = (
        db.query(IntakeSpecialist)
        .order_by(
            IntakeSpecialist.practice_area,
            IntakeSpecialist.position
        )
        .all()
    )

    grouped_data = []

    practice_area_map = {}

    for u in users:
        practice_area = u.practice_area
        group_number = ((u.position - 1) // 3) + 1
        group_key = f"group_{group_number}"

        if practice_area not in practice_area_map:
            practice_area_map[practice_area] = {
                "practice_area": practice_area
            }

        # create group only when first needed
        if group_key not in practice_area_map[practice_area]:
            practice_area_map[practice_area][group_key] = []

        practice_area_map[practice_area][group_key].append({
            "position": u.position,
            "email": u.email
        })

    grouped_data = list(practice_area_map.values())

    return {
        "total_practice_areas": len(grouped_data),
        "data": grouped_data
    }

@admin_router.post("/intakeops/intake_specialists")
async def set_intake_specialist(
    practice_area: str = Body(...),
    email: EmailStr = Body(...),
    position: int = Body(..., ge=1, le=18),
    db: Session = Depends(get_db)
):
    practice_area = practice_area.upper()

    existing = db.query(IntakeSpecialist).filter(
        IntakeSpecialist.practice_area == practice_area,
        IntakeSpecialist.position == position
    ).first()

    if existing:
        old_email = existing.email
        existing.email = email
    else:
        old_email = None
        new_entry = IntakeSpecialist(
            practice_area=practice_area,
            position=position,
            email=email
        )
        db.add(new_entry)

    db.commit()

    return {
        "message": "Email set successfully",
        "practice_area": practice_area,
        "position": position,
        "old_email": old_email,
        "new_email": email
    }
    
email_adapter = TypeAdapter(EmailStr)
@admin_router.post("/intakeops/intake_specialists/bulk")
async def bulk_set_intake_specialists(
    items: list = Body(...),
    db: Session = Depends(get_db)
):
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="Request body must be a list")

    results = []

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail=f"Item at index {idx} must be an object")

        practice_area = item.get("practice_area")
        email = item.get("email")
        position = item.get("position")

        # ---- Basic validation ----
        if not practice_area or not isinstance(practice_area, str):
            raise HTTPException(status_code=400, detail=f"Invalid practice_area at index {idx}")

        if not isinstance(position, int) or not (1 <= position <= 18):
            raise HTTPException(status_code=400, detail=f"Position must be int between 1-18 at index {idx}")

        try:
            email = email_adapter.validate_python(email)
        except (ValidationError, ValueError):
            raise HTTPException(status_code=400, detail=f"Invalid email at index {idx}")

        practice_area = practice_area.upper()

        existing = db.query(IntakeSpecialist).filter(
            IntakeSpecialist.practice_area == practice_area,
            IntakeSpecialist.position == position
        ).first()

        if existing:
            old_email = existing.email
            existing.email = email
            action = "updated"
        else:
            old_email = None
            db.add(IntakeSpecialist(
                practice_area=practice_area,
                position=position,
                email=email
            ))
            action = "created"

        results.append({
            "practice_area": practice_area,
            "position": position,
            "old_email": old_email,
            "new_email": email,
            "action": action
        })

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "message": "Bulk operation successful",
        "total_processed": len(items),
        "results": results
    }


@admin_router.get("/intakeops/intake_specialists")
async def get_intake_specialists(
    practice_area: Optional[str] = Query(None),
    group: Optional[int] = Query(None),
    db: Session = Depends(get_db)
):
    query = db.query(IntakeSpecialist)

    # Optional filter: practice_area
    if practice_area:
        query = query.filter(
            IntakeSpecialist.practice_area == practice_area.upper()
        )

    # Optional filter: group
    if group:
        if not (1 <= group <= 6):
            raise HTTPException(400, "Group must be between 1 and 6")

        start = ((group - 1) * 3) + 1
        end = group * 3

        query = query.filter(
            IntakeSpecialist.position.between(start, end)
        )

    users = query.order_by(
        IntakeSpecialist.practice_area,
        IntakeSpecialist.position
    ).all()

    return {
        "total": len(users),
        "data": [
            {
                "practice_area": u.practice_area,
                "position": u.position,
                "email": u.email
            }
            for u in users
        ]
    }

@admin_router.get("/intakeops/active-today")
async def admin_active_today(db: Session = Depends(get_db)):
    today = datetime.utcnow().date()

    rows = db.query(
        ActiveSession.user_email,
        ActiveSession.last_activity_timestamp
    ).filter(
        ActiveSession.project_name == PROJECT_NAME,
        func.date(ActiveSession.last_activity_timestamp) == today
    ).all()

    return [
        {"email": email, "last_active": ts.isoformat()}
        for email, ts in rows
    ] 

@admin_router.get("/intakeops/error-logs")
async def get_error_logs(
    category: Optional[str] = None,
    status_code: Optional[int] = None,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    query = db.query(ApiErrorLog).filter(
        ApiErrorLog.tool == PROJECT_NAME
    )

    if category:
        query = query.filter(ApiErrorLog.category == category)

    if status_code:
        query = query.filter(ApiErrorLog.status_code == status_code)

    logs = (
        query
        .order_by(ApiErrorLog.created_at.desc())
        .limit(limit)
        .all()
    )

    if not logs:
        raise HTTPException(status_code=404, detail="No error logs found")

    return [
        {
            "id": log.id,
            "api": log.api,
            "error_type": log.error_type,
            "category": log.category,
            "message": log.message,
            "status_code": log.status_code,
            "request_id": log.request_id,
            "created_at": log.created_at.isoformat()
        }
        for log in logs
    ]


@router.post("/referrals/import")
async def import_referrals(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    def clean_value(value):
        return None if pd.isna(value) else value

    try:
        if file.filename.endswith(".csv"):
            try:
                df = pd.read_csv(file.file, encoding="utf-8")
            except UnicodeDecodeError:
                file.file.seek(0)
                df = pd.read_csv(file.file, encoding="latin-1")
        else:
            df = pd.read_excel(file.file)

        df.columns = [
            col.replace("\xa0", " ").strip().lower()
            for col in df.columns
        ]

        print("Columns in file:", df.columns.tolist())

        created = 0

        for _, row in df.iterrows():
            law_firm = clean_value(row.get("law firm"))

            if not law_firm:
                continue

            referral = Referral(
                practice_area=clean_value(row.get("case type")),
                organization_name=law_firm,
                contact_name=clean_value(row.get("referred to:")),
                phone=(
                    str(row.get("phone number"))
                    if clean_value(row.get("phone number")) is not None
                    else None
                ),
                email=clean_value(row.get("email")),
                is_active=True
            )

            db.add(referral)
            created += 1

        db.commit()

        return {
            "message": "Import successful",
            "records_created": created
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Import failed: {str(e)}")
    
@router.get("/referrals/search")
async def search_referrals(
    practice_area: str = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    query = db.query(Referral).filter(Referral.is_active == True)

    if practice_area:
        query = query.filter(
            Referral.practice_area.ilike(f"%{practice_area.strip()}%")
        )

    subquery = (
        db.query(func.min(Referral.id).label("id"))
        .filter(Referral.is_active == True)
    )
    if practice_area:
        subquery = subquery.filter(
            Referral.practice_area.ilike(f"%{practice_area.strip()}%")
        )
    subquery = subquery.group_by(Referral.email).subquery()
    # Join back to get full row
    query = db.query(Referral).join(subquery, Referral.id == subquery.c.id)

    results = query.all()

    return [
        {
            "id": r.id,
            "organization_name": r.organization_name,
            "phone": r.phone,
            "email": r.email,
            "practice_area": r.practice_area,
            "contact_name": r.contact_name
        }
        for r in results
    ]

@router.get("/referrals/practice-areas")
async def get_practice_areas(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    query = db.query(
        Referral.practice_area
    ).filter(
        Referral.is_active == True,
        Referral.practice_area.isnot(None)
    )

    results = query.distinct().all()

    return [pa for (pa,) in results]

@router.post("/leads/{lead_id}/referrals")
async def attach_referrals(
    lead_id: int,
    referral_ids: list[int] = Query(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    lead = db.query(LeadIntake).filter(LeadIntake.id == lead_id).first()

    if not lead:
        raise HTTPException(404, "Lead not found")

    if lead.status != "declined":
        raise HTTPException(400, "Referrals can only be added to declined leads")
    
    existing = db.query(LeadReferral).filter(
        LeadReferral.lead_id == lead_id
    ).first()

    if existing:
        raise HTTPException(
            status_code=400,
            detail="Referrals already linked to this lead. Cannot add more."
        )

    try:
        for rid in referral_ids:
            db.add(LeadReferral(
                lead_id=lead_id,
                referral_id=rid,
                selected_by=current_user["email"]
            ))

        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(400, "One or more referrals already attached")

    return {"message": "Referrals attached successfully"}


@router.get("/leads/{lead_id}/referrals")
async def get_lead_referrals(
    lead_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    results = db.query(LeadReferral).options(
        joinedload(LeadReferral.referral)
    ).filter(
        LeadReferral.lead_id == lead_id
    ).order_by(LeadReferral.selected_at.desc()).all()
    
    if not results:
        return {
            "lead_id": lead_id,
            "message": "No referrals linked to this lead",
            "data": []
        }

    return [
        {
            "id": r.referral.id,
            "organization_name": r.referral.organization_name,
            "phone": r.referral.phone,
            "email": r.referral.email,
            "practice_area": r.referral.practice_area,
            "contact_name": r.referral.contact_name
        }
        for r in results if r.referral
    ]

@router.get("/referrals/analysis")
async def referral_analytics(
    month: int = Query(None, ge=1, le=12),
    year: int = Query(None),
    selected_by: str = Query(None, description="Filter by user email who referred"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    query = db.query(LeadReferral).options(
        joinedload(LeadReferral.referral),
        joinedload(LeadReferral.lead)
    )

    # ---- FILTER: selected_by ----
    if selected_by:
        query = query.filter(LeadReferral.selected_by.ilike(f"%{selected_by}%"))

    # ---- FILTER: month/year ----
    if month:
        query = query.filter(
            LeadReferral.lead.has(
                extract('month', LeadIntake.lead_email_sent_at) == month
            )
        )

    if year:
        query = query.filter(
            LeadReferral.lead.has(
                extract('year', LeadIntake.lead_email_sent_at) == year
            )
        )

    results = query.order_by(LeadReferral.selected_at.desc()).all()

    grouped = {}

    for r in results:
        lead = r.lead
        referral = r.referral

        if not lead or not referral:
            continue

        key = f"{lead.id}_{r.selected_by}"

        if key not in grouped:
            grouped[key] = {
                "lead_id": lead.id,
                "lead_name": f"{lead.first_name} {lead.last_name}",
                "lead_email": lead.email,
                "lead_status": lead.status,
                "decline_type": lead.decline_type,

                "referred_by": r.selected_by,
                "referred_at": r.selected_at.isoformat(),

                "email_sent": lead.email_sent,
                "email_sent_at": (
                    lead.lead_email_sent_at.isoformat()
                    if lead.lead_email_sent_at else None
                ),
                "month": (
                    lead.lead_email_sent_at.strftime("%B")
                    if lead.lead_email_sent_at else None
                ),
                "year": (
                    lead.lead_email_sent_at.year
                    if lead.lead_email_sent_at else None
                ),

                "referrals": []
            }

        grouped[key]["referrals"].append({
            "referral_id": referral.id,
            "referred_organization_name": referral.organization_name,
            "referred_contact_name": referral.contact_name,
            "referral_email": referral.email,
            "referral_phone": referral.phone,
            "practice_area": referral.practice_area
        })

    if not grouped:
        return {
            "total_groups": 0,
            "message": "No referral records found for given filters",
            "data": []
        }

    return {
        "total_groups": len(grouped),
        "data": list(grouped.values())
    }

@router.get("/email-templates")
async def get_email_templates(
    lead_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    import MVA_Platform.email_templates as templates_module

    lead = db.query(LeadIntake).filter(LeadIntake.id == lead_id).first()

    if not lead:
        raise HTTPException(404, "Lead not found")

    if lead.status == "pending":
        return {
            "lead_id": lead.id,
            "status": lead.status,
            "message": "Lead is still pending. Please review and update status before previewing email templates.",
            "templates": []
        }

    if not lead.reviewed_by:
        return {
            "lead_id": lead.id,
            "status": lead.status,
            "message": "Lead must be reviewed before previewing templates.",
            "templates": []
        }

    lead_data = {
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "email": lead.email,
        "phone": lead.phone,
        "case_type": lead.case_type,
        "state_of_incident": lead.state_of_incident,
        "accident_date": lead.accident_date,
        "case_description": lead.case_description
    }

    reason = lead.reviewer_note or ""
    reviewer_email = lead.reviewed_by or ""

    templates = []

    referrals = db.query(LeadReferral).options(
        joinedload(LeadReferral.referral)
    ).filter(LeadReferral.lead_id == lead.id).all()

    function_names = []

    if lead.status == "proceed":
        function_names = ["build_approved_email"]

    elif lead.status == "escalated":
        function_names = ["build_escalated_email"]

    elif lead.status == "declined":
        if lead.decline_type == "declined_refer_out":
            if not referrals:
                return {
                    "lead_id": lead.id,
                    "status": lead.status,
                    "message": "No referrals available for this lead, Add the referrals first to preview the email template.",
                    "templates": []
                }
            function_names = ["build_declined_with_referrals_email"]
        else:
            function_names = ["build_declined_email"]

    for func_name in function_names:
        func = getattr(templates_module, func_name)

        try:
            if func_name in ["build_approved_email"]:
                body = func(lead_data, reason, reviewer_email, "PREVIEW_LINK")
                subject = f"{'Approved'} - {lead.first_name}"

            elif func_name == "build_escalated_email":
                body = func(lead_data, reason, reviewer_email)
                subject = f"Lead Escalation - {lead.first_name} {lead.last_name}"

            elif func_name == "build_declined_with_referrals_email":
                body = func(lead_data, referrals)
                subject = "Regarding Your Case Evaluation Request"

            else:
                body = func(lead_data)
                subject = "Regarding Your Case Evaluation Request"

            templates.append({
                "template_type": func_name.replace("build_", "").replace("_email", ""),
                "subject": subject,
                "body": body,
                "editable": True
            })

        except Exception:
            continue

    if not templates:
        return {
            "lead_id": lead.id,
            "status": lead.status,
            "message": "No templates available",
            "templates": []
        }

    return {
        "lead_id": lead.id,
        "status": lead.status,
        "decline_type": lead.decline_type,
        "total_templates": len(templates),
        "templates": templates
    }

# util function for weekly or monthly range
MOUNTAIN_TZ = ZoneInfo("America/Denver")
UTC = timezone.utc

def get_date_range(range_type: str, end_date: datetime | None = None):
    now_utc = datetime.now(UTC)
    now_mt = now_utc.astimezone(MOUNTAIN_TZ)

    if end_date is None:
        end_date_mt = now_mt
    else:
    
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=UTC)
        else:
            end_date = end_date.astimezone(UTC)

        if end_date > now_utc:
            end_date = now_utc

        end_date_mt = end_date.astimezone(MOUNTAIN_TZ)

    if range_type == "today":
        start_date_mt = end_date_mt.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        )

    elif range_type == "weekly":
        start_date_mt = end_date_mt - timedelta(days=13)

    elif range_type == "monthly":
        start_date_mt = end_date_mt - relativedelta(months=1)

    elif range_type == "yearly":
        start_date_mt = end_date_mt - relativedelta(years=1)

    else:
        raise HTTPException(
            400,
            "range must be 'today','weekly', 'monthly', or 'yearly'"
        )

    start_date = (
        start_date_mt
        .astimezone(UTC)
        .replace(tzinfo=None)
    )

    end_date = (
        end_date_mt
        .astimezone(UTC)
        .replace(tzinfo=None)
    )

    return start_date, end_date

@router.get("/dashboard/summary")
async def dashboard_summary(
    range_type: Literal["today", "weekly", "monthly", "yearly"] = Query("weekly"),
    end_date: datetime = Query(...),
    include_neos_direct: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    start_date, end_date = get_date_range(
        range_type,
        end_date
    )
    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    yesterday_start = today_start - timedelta(days=1)

    # BASE DATA
    leads = db.query(LeadIntake).filter(
        LeadIntake.received_at >= start_date,
        LeadIntake.received_at <= end_date
    ).all()

    today_leads = [
    l for l in leads
    if today_start <= l.received_at <= end_date
    ]

    yesterday_leads = [
        l for l in leads
        if yesterday_start <= l.received_at < today_start
    ]

    leads_today = len(today_leads)
    leads_yesterday = len(yesterday_leads)

    # 2. OPEN QUEUE 
    status_counts = defaultdict(int)
    rows = db.query(
        LeadIntake.status,
        func.count(LeadIntake.id)
    ).filter(
        LeadIntake.received_at >= start_date,
        LeadIntake.received_at <= end_date
    ).group_by(LeadIntake.status).all()

    for status, count in rows:
        status_counts[status] = count

    pending = status_counts["pending"]
    escalated = status_counts["escalated"]

    open_queue = pending + escalated

    # 3. AVG TIME TO REVIEW
    review_times = [
        (l.reviewed_at - l.received_at).total_seconds() / 60
        for l in leads
        if l.reviewed_at and l.received_at
    ]
    avg_review = sum(review_times) / len(review_times) if review_times else 0

    SLA_TARGET = 15  # minutes

    # 4. NAFI RATE
    total_count = len(leads)

    # Proceed / NAFI rates leave Neos Direct leads out unless the toggle asks for them.
    rate_leads = leads if include_neos_direct else [l for l in leads if not is_neos_direct(l)]
    rate_lead_ids = {l.id for l in rate_leads}
    rate_total = len(rate_leads)

    nafi_count = sum(1 for l in rate_leads if l.nafi_tool_recommendation == "likely_nafi")

    # EMAIL COUNTS FOR SELECTED RANGE
    emails_sent_count = db.query(
        func.count(LeadIntake.id)
    ).filter(
        LeadIntake.email_sent == True,
        LeadIntake.lead_email_sent_at >= start_date,
        LeadIntake.lead_email_sent_at < end_date
    ).scalar()

    # EMAIL STATUS BREAKDOWN
    proceed_email_count = db.query(
        func.count(LeadIntake.id)
    ).filter(
        LeadIntake.email_sent == True,
        LeadIntake.status == "proceed",
        LeadIntake.lead_email_sent_at >= start_date,
        LeadIntake.lead_email_sent_at < end_date
    ).scalar()

    declined_email_count = db.query(
        func.count(LeadIntake.id)
    ).filter(
        LeadIntake.email_sent == True,
        LeadIntake.status == "declined",
        LeadIntake.lead_email_sent_at >= start_date,
        LeadIntake.lead_email_sent_at < end_date
    ).scalar()

    escalated_email_count = db.query(
        func.count(LeadIntake.id)
    ).filter(
        LeadIntake.email_sent == True,
        LeadIntake.status == "escalated",
        LeadIntake.lead_email_sent_at >= start_date,
        LeadIntake.lead_email_sent_at < end_date
    ).scalar()

    # TURNOVER %
    turnover = (
        (total_count / emails_sent_count) * 100
        if emails_sent_count else 0
    )

# 5. PROCEED RATE (+ VALIDATED)
    proceed_count = sum(1 for l in rate_leads if l.status == "proceed")

    nafi_rate = (nafi_count / rate_total * 100) if rate_total else 0
    proceed_rate = (proceed_count / rate_total * 100) if rate_total else 0

    # Owl reviews for the leads in range (effective verdict, so overrides count)
    # Owl reviews for the leads in range
    owl_by_lead = load_owl_reviews(db, [l.id for l in leads])

    validated_count = 0
    agree_count = 0
    disagree_count = 0

    for l in leads:

        human_decision = derive_agree_disagree(l)

        owl_verdict = (
            owl_fields(owl_by_lead.get(l.id))["owl_verdict"]
        )

        # HIL: Use the human validation when available.
        # Otherwise, use Owl's prescreen result.
        if human_decision in ("Agree", "Disagree"):
            hil_decision = human_decision

        elif owl_verdict == "agree":
            hil_decision = "Agree"

        elif owl_verdict == "disagree":
            hil_decision = "Disagree"

        else:
            hil_decision = None

        # Count each lead only once.
        if hil_decision == "Agree":
            agree_count += 1

        elif hil_decision == "Disagree":
            disagree_count += 1

        human_agrees = human_decision == "Agree"
        owl_agrees = owl_verdict == "agree"

        # Validated = proceed call AND (human agrees OR Owl agrees)
        if l.id in rate_lead_ids and l.status == "proceed" and (human_agrees or owl_agrees):
            validated_count += 1


    validated_proceed_rate = (
        validated_count / rate_total * 100
        if rate_total else 0
    )

    # HIL AGREEMENT
    # Formula: HIL Agree / Total Leads * 100

    ai_agreement = (
        agree_count / total_count * 100
        if total_count else 0
    )


    # 7. LEAD VELOCITY
    hours_passed = max((end_date - start_date).total_seconds() / 3600, 1)
    lead_velocity = len(leads) / hours_passed

    # 8. LEADERBOARD 
    leaderboard_query = (
        db.query(
            LeadIntake.lead_accepted_by,
            func.count(LeadIntake.id)
        )
        .filter(
            LeadIntake.lead_accepted == True,
            LeadIntake.lead_accepted_by.isnot(None),
            LeadIntake.received_at >= start_date,
            LeadIntake.received_at <= end_date
        )
        .group_by(LeadIntake.lead_accepted_by)
        .order_by(func.count(LeadIntake.id).desc())
        .limit(3)
        .all()
    )

    leaderboard = [
        {
            "rank": i + 1,
            "name": row[0],
            "intakes": row[1]
        }
        for i, row in enumerate(leaderboard_query)
    ]

    while len(leaderboard) < 3:
        leaderboard.append({
            "rank": len(leaderboard) + 1,
            "name": "—",
            "intakes": 0
        }) 

    cutoff_date = datetime.utcnow() - timedelta(days=1)

    assigned_cases = (
        db.query(
            LeadIntake.assigned_to,
            func.count(LeadIntake.id).label("total_cases")
        )
        .filter(
            LeadIntake.received_at >= start_date,
            LeadIntake.received_at <= end_date,
            LeadIntake.received_at < cutoff_date,
            LeadIntake.assigned_to.isnot(None),
            LeadIntake.assigned_to != "",
            LeadIntake.assigned_to != "Daniel P. Filbert"
        )
        .group_by(LeadIntake.assigned_to)
        .order_by(func.count(LeadIntake.id).desc())
        .all()
    )

    neos_assigned_to = [
        {
            "name": row.assigned_to,
            "cases": row.total_cases
        }
        for row in assigned_cases
    ]

    return {
        "range": {
            "type": range_type,
            "start_date": start_date,
            "end_date": end_date
        },
        "instrument_panel": {
            "new_leads_today": {
                "data":{
                "value": leads_today,
                "vs_yesterday": leads_yesterday,
                "today": leads_today
                },
                "message": (
                    "Shows how many new leads were received today based on today's lead count."
                )
            },

            "turnover": {
                "data": {
                    "value": round(turnover, 1),
                    "total_intakes": total_count,
                    "emails_sent": emails_sent_count,

                    "status_breakdown": {
                        "proceed": proceed_email_count,
                        "declined": declined_email_count,
                        "escalated": escalated_email_count
                    },

                    "range_type": range_type
                },

                "message": (
                    f"Shows the Turnover percentage based on total intakes received versus total emails sent for proceed, declined, and escalated leads."
                )
            },
            "open_queue": {
                "data": {
                "total": open_queue,
                "breakdown": {
                    "pending": pending,
                    "escalated": escalated
                }},
                "message": (
                    "Shows leads still waiting for action based on pending, need info, and escalated leads."
                )
            },

            "avg_time_to_review": {
                "data":{
                "value": round(avg_review, 1),
                "sla_target": SLA_TARGET},
                "message": (
                    "Shows the average time taken to review leads based on reviewed and received timestamps."
                )
            },

            "nafi_rate": {
                "data": round(nafi_rate, 1),
                "total_leads": rate_total,
                "includes_neos_direct": include_neos_direct,
                "message": (
                    "Shows the percentage of leads marked as likely NAFI based on NAFI tool recommendations."
                )
            },

            "proceed_rate": {
                "data": {
                    "value": round(proceed_rate, 1),
                    "validated_value": round(validated_proceed_rate, 1),
                    "proceed_count": proceed_count,
                    "validated_count": validated_count,
                    "total_leads": rate_total,
                    "includes_neos_direct": include_neos_direct,
                },
                "message": (
                    "Shows the percentage of leads marked proceed, and the validated "
                    "percentage where proceed was the call and a human or Owl agreed."
                )
            },

            "ai_human_agreement": {
                "data": round(ai_agreement, 1),
                "agree_count": agree_count,
                "total_leads": total_count,
                "message": (
                    "Shows the percentage of all leads where the reviewer agreed "
                    "(Agree count / total leads x 100)."
                )
            },

            "lead_velocity": {
                "data":{
                "value": round(lead_velocity, 2),
                "unit": "per_hour"},
                "message": (
                    "Shows the speed of incoming leads based on leads received over time."
                )
            },

            "top_specialists": {
                "data": leaderboard,
                "message": (
                    "Shows the top intake specialists based on accepted lead count."
                )
            },

            "neos_assigned_to": {
                "data": neos_assigned_to,
                "message": (
                    "Shows the number of cases assigned to each NEOS user."
                )
            }
        }
    }

async def generate_analytics_report(
    intake,
    sources,
    case_types,
    disposition,
    nafi_rate_source,
    referral_areas,
    range_type
):

    insights = []

    # TOTAL LEADS
    # total_leads = sum(intake.values())
    if range_type == "weekly":
        total_leads = sum(intake["current_week"].values()) + sum(intake["previous_week"].values())
    else:
        total_leads = sum(intake.values())

    if total_leads >= 100:
        insights.append({
            "type": "positive",
            "title": "Strong Intake Volume",
            "message": f"{total_leads} total leads were received during this {range_type} reporting period."
        })

    elif total_leads < 20:
        insights.append({
            "type": "warning",
            "title": "Low Intake Volume",
            "message": f"Only {total_leads} leads were received during this {range_type} reporting period."
        })

    # BUSIEST DAY
    # busiest_day = max(intake, key=intake.get)
    if range_type == "weekly":
        combined = {d: intake["current_week"].get(d, 0) + intake["previous_week"].get(d, 0) for d in intake["current_week"]}
        busiest_day = max(combined, key=combined.get)
    else:
        busiest_day = max(intake, key=intake.get)

    insights.append({
        "type": "neutral",
        "title": ("Peak Intake Hour" if range_type == "today" else "Peak Intake Day"),
        "message": f"{busiest_day} recorded the highest intake activity."
    })

    # TOP SOURCE
    if sources:

        top_source = max(
            sources,
            key=lambda k: sources[k]["count"]
        )

        insights.append({
            "type": "positive",
            "title": "Top Lead Source",
            "message": f"{top_source} generated the highest lead volume."
        })

    # TOP CASE TYPE
    if case_types:

        top_case = max(
            case_types,
            key=case_types.get
        )

        insights.append({
            "type": "neutral",
            "title": "Dominant Case Type",
            "message": f"{top_case} was the most common case category."
        })

    # ESCALATION ANALYSIS
    total_escalated = 0

    for day_data in disposition.values():
        total_escalated += day_data.get("Escalated", 0)

    if total_escalated > 10:
        insights.append({
            "type": "warning",
            "title": "Escalation Risk",
            "message": f"{total_escalated} escalated leads were identified during this reporting period."
        })

    else:
        insights.append({
            "type": "positive",
            "title": "Escalation Stability",
            "message": "Escalation volume remained under control."
        })

    # NAFI SOURCE ANALYSIS
    high_nafi_sources = []

    for source_name, data in nafi_rate_source.items():

        if data["nafi_rate"] >= 60:
            high_nafi_sources.append(source_name)

    if high_nafi_sources:

        insights.append({
            "type": "warning",
            "title": "High NAFI Sources",
            "message": f"High NAFI rates detected from: {', '.join(high_nafi_sources)}."
        })

    # REFERRAL AREAS
    if referral_areas:

        top_referral = max(
            referral_areas,
            key=referral_areas.get
        )

        insights.append({
            "type": "neutral",
            "title": "Top Referral Practice Area",
            "message": f"{top_referral} generated the highest referral activity."
        })

    # PROCEED RATE
    proceed_total = 0

    for day_data in disposition.values():
        proceed_total += day_data.get("Proceed", 0)

    proceed_rate = round(
        (proceed_total / total_leads) * 100,
        1
    ) if total_leads else 0

    if proceed_rate >= 40:

        insights.append({
            "type": "positive",
            "title": "Healthy Conversion",
            "message": f"Proceed rate reached {proceed_rate}% across all leads."
        })

    else:

        insights.append({
            "type": "warning",
            "title": "Low Conversion",
            "message": f"Proceed rate closed at {proceed_rate}%."
        })

    return insights

def calculate_completion_score(
    lead,
    tracked_fields
):
    completed = sum(
        1
        for field in tracked_fields
        if getattr(lead, field, None)
    )

    return round(
        (completed / len(tracked_fields)) * 100,
        1
    )

MST = ZoneInfo("America/Denver")

def generate_data_quality_metrics(
    leads,
    start_date,
    end_date,
    range_type
):
    tracked_fields = [
        "accident_date",
        "injury_severity",
        "fault",
        "insurance",
        "medical_treatment",
        "case_description",
        "practice_area"
    ]

    quality_buckets = defaultdict(list)

    field_completion = {
        field: 0
        for field in tracked_fields
    }

    all_scores = []
    for lead in leads:
        score = calculate_completion_score(
            lead,
            tracked_fields
        )
        all_scores.append(score)

        if lead.received_at:

            received_at_mst = lead.received_at.replace(
                tzinfo=timezone.utc
            ).astimezone(MST)

            if range_type == "today":

                bucket = received_at_mst.strftime(
                    "%Y-%m-%d %H:00"
                )

            else:

                bucket = received_at_mst.date().isoformat()

            quality_buckets[bucket].append(score)

        for field in tracked_fields:
            if getattr(lead, field, None):
                field_completion[field] += 1

    quality_data = []

    if range_type == "today":

        current = start_date.replace(
            minute=0,
            second=0,
            microsecond=0
        )

        end_bucket = end_date.replace(
            minute=0,
            second=0,
            microsecond=0
        )

        while current <= end_bucket:

            bucket = current.strftime(
                "%Y-%m-%d %H:00"
            )

            scores = quality_buckets.get(
                bucket,
                []
            )

            quality_data.append({
                "hour": current.strftime("%H:00"),
                "lead_count": len(scores),
                "avg_completion_percent": round(
                    sum(scores) / len(scores),
                    1
                ) if scores else 0
            })

            current += timedelta(hours=1)

    else:

        current = start_date.date()

        while current <= end_date.date():

            bucket = current.isoformat()

            scores = quality_buckets.get(
                bucket,
                []
            )

            quality_data.append({
                "date": bucket,
                "lead_count": len(scores),
                "avg_completion_percent": round(
                    sum(scores) / len(scores),
                    1
                ) if scores else 0
            })

            current += timedelta(days=1)

    total_leads = len(leads)

    field_percentages = {
        field: round(
            (count / total_leads) * 100,
            1
        ) if total_leads else 0
        for field, count in field_completion.items()
    }

    current_quality_score = round(
        sum(all_scores) / len(all_scores),
        1
    ) if all_scores else 0

    return {
        "summary": {
            "current_score": current_quality_score,
            "best_completed_field": (
                max(
                    field_percentages,
                    key=field_percentages.get
                )
                if field_percentages else None
            ),
            "most_missing_field": (
                min(
                    field_percentages,
                    key=field_percentages.get
                )
                if field_percentages else None
            )
        },

        "fields_tracked": tracked_fields,
        "field_completion": field_percentages,
        "data": quality_data
    }

def generate_hourly_lead_volume(leads):
    hourly_counts = {
        f"{hour:02d}:00": 0
        for hour in range(24)
    }

    for lead in leads:
        if not lead.received_at:
            continue

        # Database stores received_at as a naive UTC datetime.
        # Attach UTC first, then convert to Mountain Time.
        received_at_utc = lead.received_at.replace(
            tzinfo=timezone.utc
        )

        received_at_mst = received_at_utc.astimezone(MST)

        # Count the lead in its Mountain Time hour.
        hour_key = received_at_mst.strftime("%H:00")
        hourly_counts[hour_key] += 1

    peak_hour = max(
        hourly_counts,
        key=hourly_counts.get
    )

    return {
        "summary": {
            "peak_hour": peak_hour,
            "peak_volume": hourly_counts[peak_hour],
            "timezone": "America/Denver"
        },
        "data": [
            {
                "hour": hour,
                "lead_count": count
            }
            for hour, count in hourly_counts.items()
        ]
    }

def normalize_source(src):
    if isinstance(src, dict):
        return src.get("name") or "Other"
    if isinstance(src, str) and src.strip():
        return src.strip()
    return "Other"

@router.get("/analytics")
async def analytics(
    range_type: Literal["today", "weekly", "monthly", "yearly"] = Query("weekly"),
    end_date: datetime = Query(...),
    include_neos_direct: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    start_date, end_date = get_date_range(range_type, end_date)

    leads = db.query(LeadIntake).filter(
        LeadIntake.received_at >= start_date,
        LeadIntake.received_at <= end_date
    ).all()

    # weekdays = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
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

    # current_week_start is the boundary between previous and current week
    current_week_start = end_date.date() - timedelta(days=6)

    # INTAKE BY WEEKDAY
    if range_type == "weekly":
        intake = {
            "current_week": {d: 0 for d in weekdays},
            "previous_week": {d: 0 for d in weekdays}
        }
        for l in leads:
            if not l.received_at:
                continue
            day = l.received_at.strftime("%a")
            if l.received_at.date() >= current_week_start:
                intake["current_week"][day] += 1
            else:
                intake["previous_week"][day] += 1
    elif range_type == "today":
        intake = {
            f"{hour:02d}:00": 0
            for hour in range(24)
        }

        for l in leads:
            if not l.received_at:
                continue

            received_at_mst = l.received_at.replace(
                tzinfo=timezone.utc
            ).astimezone(MST)

            hour_key = received_at_mst.strftime("%H:00")
            intake[hour_key] += 1
    else:
        intake = {d: 0 for d in weekdays}
        for l in leads:
            if not l.received_at:
                continue
            intake[l.received_at.strftime("%a")] += 1

    # SOURCE MIX
    if range_type == "weekly":
        source_raw_cw, source_raw_pw = {}, {}
        for l in leads:
            key = normalize_source(l.source)
            if l.received_at and l.received_at.date() >= current_week_start:
                source_raw_cw[key] = source_raw_cw.get(key, 0) + 1
            else:
                source_raw_pw[key] = source_raw_pw.get(key, 0) + 1
        def _source_mix(raw):
            total = sum(raw.values()) or 1
            return dict(sorted(((k, {"count": v, "pct": round(v/total*100,1)}) for k,v in raw.items() if v>0), key=lambda x: x[1]["count"], reverse=True)[:10])
        source_mix = {"current_week": _source_mix(source_raw_cw), "previous_week": _source_mix(source_raw_pw)}
    else:
        source_raw = {}
        for l in leads:
            key = normalize_source(l.source)
            source_raw[key] = source_raw.get(key, 0) + 1
        total_source = sum(source_raw.values()) or 1
        source_mix = dict(sorted(((k, {"count": v, "pct": round(v/total_source*100,1)}) for k,v in source_raw.items() if v>0), key=lambda x: x[1]["count"], reverse=True)[:10])

    # CASE TYPE
    if range_type == "weekly":
        case_type = {"current_week": {}, "previous_week": {}}
        for l in leads:
            key = l.case_type or "Other"
            if l.received_at and l.received_at.date() >= current_week_start:
                case_type["current_week"][key] = case_type["current_week"].get(key, 0) + 1
            else:
                case_type["previous_week"][key] = case_type["previous_week"].get(key, 0) + 1
    else:
        case_type = {}
        for l in leads:
            key = l.case_type or "Other"
            case_type[key] = case_type.get(key, 0) + 1

    # DISPOSITION
    if range_type == "weekly":
        disposition = {"current_week": {d: {} for d in weekdays}, "previous_week": {d: {} for d in weekdays}}
        for l in leads:
            if not l.received_at:
                continue
            wd = l.received_at.strftime("%a")
            bucket = "Proceed" if l.status == "proceed" else "Declined" if l.status == "declined" else "Escalated" if l.status == "escalated" else "Pending"
            week = "current_week" if l.received_at.date() >= current_week_start else "previous_week"
            disposition[week][wd][bucket] = disposition[week][wd].get(bucket, 0) + 1
    else:
        disposition = {d: {} for d in weekdays}
        for l in leads:
            if not l.received_at:
                continue
            wd = f"{l.received_at.hour:02d}:00" if range_type == "today" else l.received_at.strftime("%a")
            bucket = "Proceed" if l.status == "proceed" else "Declined" if l.status == "declined" else "Escalated" if l.status == "escalated" else "Pending"
            disposition[wd][bucket] = disposition[wd].get(bucket, 0) + 1

    # NAFI RATE BY SOURCE
    if range_type == "weekly":
        nafi_by_source = {"current_week": {}, "previous_week": {}}
        for l in leads:
            if not include_neos_direct and is_neos_direct(l):
                continue
            src = l.source or "Other"
            week = "current_week" if l.received_at and l.received_at.date() >= current_week_start else "previous_week"
            if src not in nafi_by_source[week]:
                nafi_by_source[week][src] = {"total": 0, "nafi": 0}
            nafi_by_source[week][src]["total"] += 1
            if l.nafi_tool_recommendation == "likely_nafi":
                nafi_by_source[week][src]["nafi"] += 1
        nafi_rate_source = {
            week: {k: {"total": v["total"], "nafi_count": v["nafi"], "nafi_rate": round(v["nafi"]/v["total"]*100,1) if v["total"] else 0} for k,v in data.items() if v["total"] >= 3}
            for week, data in nafi_by_source.items()
        }
    else:
        nafi_by_source = {}
        for l in leads:
            if not include_neos_direct and is_neos_direct(l):
                continue
            src = l.source or "Other"
            if src not in nafi_by_source:
                nafi_by_source[src] = {"total": 0, "nafi": 0}
            nafi_by_source[src]["total"] += 1
            if l.nafi_tool_recommendation == "likely_nafi":
                nafi_by_source[src]["nafi"] += 1
        nafi_rate_source = {k: {"total": v["total"], "nafi_count": v["nafi"], "nafi_rate": round(v["nafi"]/v["total"]*100,1) if v["total"] else 0} for k,v in nafi_by_source.items() if v["total"] >= 3}

    # DECLINE RATE BY SOURCE
    if range_type == "weekly":
        decline_source = {"current_week": {}, "previous_week": {}}
        for l in leads:
            src = l.source.get("name") if isinstance(l.source, dict) else str(l.source or "Other")
            week = "current_week" if l.received_at and l.received_at.date() >= current_week_start else "previous_week"
            if src not in decline_source[week]:
                decline_source[week][src] = {"proceed": 0, "declined": 0}
            if l.status == "proceed":
                decline_source[week][src]["proceed"] += 1
            elif l.status in ["declined_no_referral", "declined_refer_out"]:
                decline_source[week][src]["declined"] += 1
        decline_rate_by_source = {}
        for week, data in decline_source.items():
            decline_rate_by_source[week] = {}
            for src_name, d in data.items():
                reviewed_total = d["proceed"] + d["declined"]
                if reviewed_total < 3:
                    continue
                decline_rate_by_source[week][src_name] = {"reviewed_leads": reviewed_total, "proceed_count": d["proceed"], "declined_count": d["declined"], "decline_rate": round((d["declined"]/reviewed_total)*100,1)}
    else:
        decline_source = {}
        for l in leads:
            src = l.source.get("name") if isinstance(l.source, dict) else str(l.source or "Other")
            if src not in decline_source:
                decline_source[src] = {"proceed": 0, "declined": 0}
            if l.status == "proceed":
                decline_source[src]["proceed"] += 1
            elif l.status in ["declined_no_referral", "declined_refer_out"]:
                decline_source[src]["declined"] += 1
        decline_rate_by_source = {}
        for src_name, data in decline_source.items():
            reviewed_total = data["proceed"] + data["declined"]
            if reviewed_total < 3:
                continue
            decline_rate_by_source[src_name] = {"reviewed_leads": reviewed_total, "proceed_count": data["proceed"], "declined_count": data["declined"], "decline_rate": round((data["declined"]/reviewed_total)*100,1)}
    
    # Top 10 leads data 
    top_states = (
        db.query(
            LeadIntake.state_of_incident.label("state"),
            func.count(LeadIntake.id).label("lead_count")
        )
        .filter(
            LeadIntake.received_at >= start_date,
            LeadIntake.received_at <= end_date,
            LeadIntake.state_of_incident.isnot(None),
            LeadIntake.state_of_incident != ""
        )
        .group_by(LeadIntake.state_of_incident)
        .order_by(func.count(LeadIntake.id).desc())
            .limit(10)
            .all()
        ) 
    
    top_states_data = [
        {
            "state": row.state,
            "lead_count": row.lead_count
        }
        for row in top_states
    ]
    
    # REFERRAL AREAS
    referrals = db.query(LeadReferral).join(LeadIntake).options(
        joinedload(LeadReferral.referral)
    ).filter(
        LeadIntake.received_at >= start_date,
        LeadIntake.received_at <= end_date
    ).all()

    if range_type == "weekly":
        referral_area_mix = {"current_week": {}, "previous_week": {}}
        for r in referrals:
            if not r.referral:
                continue
            area = r.referral.practice_area or "Other"
            lead_date = r.lead.received_at.date() if r.lead and r.lead.received_at else None
            week = "current_week" if lead_date and lead_date >= current_week_start else "previous_week"
            referral_area_mix[week][area] = referral_area_mix[week].get(area, 0) + 1
    else:
        referral_area_mix = {}
        for r in referrals:
            if not r.referral:
                continue
            area = r.referral.practice_area or "Other"
            referral_area_mix[area] = referral_area_mix.get(area, 0) + 1 

    data_quality = generate_data_quality_metrics(leads, start_date, end_date, range_type)
    hourly_volume = generate_hourly_lead_volume(leads)

    # report uses current_week data for weekly (flat dict expected by generator)
    report_intake = intake
    report_sources = source_mix["current_week"] if range_type == "weekly" else source_mix
    report_case_types = case_type["current_week"] if range_type == "weekly" else case_type
    report_disposition = disposition["current_week"] if range_type == "weekly" else disposition
    report_nafi = nafi_rate_source["current_week"] if range_type == "weekly" else nafi_rate_source
    report_referral_areas = referral_area_mix["current_week"] if range_type == "weekly" else referral_area_mix

    report = await generate_analytics_report(
        intake=intake,
        sources=report_sources,
        case_types=report_case_types,
        disposition=report_disposition,
        nafi_rate_source=report_nafi,
        referral_areas=report_referral_areas,
        range_type=range_type
    )

    closed_case_reasons = (
        db.query(
            LeadIntake.neos_status.label("reason"),
            func.count(LeadIntake.id).label("count")
        )
        .filter(
            LeadIntake.received_at >= start_date,
            LeadIntake.received_at <= end_date,
            LeadIntake.neos_status.isnot(None),
            LeadIntake.neos_status != ""
        )
        .group_by(LeadIntake.neos_status)
        .order_by(func.count(LeadIntake.id).desc())
        .all()
    )

    closed_case_reasons_data = [
        {
            "reason": row.reason,
            "count": row.count
        }
        for row in closed_case_reasons
    ] 

    agreement_rates_data = build_agreement_rates(db, start_date, end_date)

    return {
    "range": {
        "type": range_type,
        "start_date": start_date,
        "end_date": end_date
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

    "nafi_rate_by_source": {
        "data": nafi_rate_source,
        "message": (
            "Shows likely NAFI percentages by source based on NAFI Tool recommendations."
        )
    },

    "decline_rate_by_source": {
    "data": decline_rate_by_source,
    "message": (
        "Shows decline rate by source based on declined leads divided by all reviewed leads (proceed + declined). Escalated leads are excluded."
    )
    },

    "referral_areas": {
        "data": referral_area_mix,
        "message": (
            "Shows referral practice area distribution based on attached referral records."
        )
    },

    "report": {
        "data": report,
        "message": (
            f"Rule-based operational insights generated from {range_type} analytics data."
        )
    },

    "data_quality_score": {
    "data": data_quality,
    "message": (
        "Shows average intake field completion percentage over time based on key intake fields."
    )
    },

    "lead_volume_by_hour": {
    "data": hourly_volume,
    "message": (
        "Shows lead arrival volume by hour of day "
        "for the selected reporting period."
    )
    },
    
    "top_10_states": {
        "top_states": top_states_data
    },
    "closed_case_reasons": {
        "data": closed_case_reasons_data,
        "message": (
            "Shows the count of each NEOS case status for the selected period."
        )
    },
    "agreement_rates": {
        "data": agreement_rates_data,
        "message": (
            "Shows Agreement Sent and Agreement Signed rates by agent decision, "
            "and the share of sent agreements the agent had marked Proceed."
        )
    }
    }


# helper llm function
client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
async def generate_daily_brief(data: dict):
    prompt = f"""
    You are a legal operations analytics assistant.

    Generate exactly 3 concise executive bullet points based ONLY on today's operational data.

    Requirements:
    - Each bullet must be 1 sentence
    - Professional tone
    - Focus on workload, queue health, review efficiency, staffing, and intake performance
    - Do NOT mention yesterday, weekly, monthly, or trends
    - Keep total response under 80 words

    Analytics Data:
    {json.dumps(data, indent=2)}

    Return ONLY valid JSON:

    {{
    "bullets": [
        "...",
        "...",
        "..."
    ]
    }}
    """

    response = await client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {
                "role": "system",
                "content": "You generate concise law firm operational summaries."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0.1
    )

    content = response.choices[0].message.content.strip()
    parsed = json.loads(content)
    return parsed["bullets"]

@router.get("/dashboard/daily-brief")
async def dashboard_daily_brief(
    include_neos_direct: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    now = datetime.utcnow()

    today_start = datetime(
        now.year,
        now.month,
        now.day
    )

    # TODAY LEADS ONLY
    today_leads = db.query(LeadIntake).filter(
        LeadIntake.received_at >= today_start
    ).all()

    today_count = len(today_leads)

    # STATUS COUNTS
    status_counts = defaultdict(int)

    for lead in today_leads:
        status_counts[lead.status] += 1

    pending = status_counts["pending"]
    escalated = status_counts["escalated"]
    proceed = status_counts["proceed"]
    declined = status_counts["declined"]

    # AVG REVIEW TIME
    review_times = [
        (l.reviewed_at - l.received_at).total_seconds() / 60
        for l in today_leads
        if l.reviewed_at and l.received_at
    ]

    avg_review = (
        sum(review_times) / len(review_times)
        if review_times else 0
    )

    # NAFI RATE (Neos Direct excluded unless the toggle asks for them)
    rate_leads = today_leads if include_neos_direct else [l for l in today_leads if not is_neos_direct(l)]
    rate_total = len(rate_leads)
    nafi_count = sum(
        1 for l in rate_leads
        if l.nafi_tool_recommendation == "likely_nafi"
    )

    nafi_rate = (
        (nafi_count / rate_total) * 100
        if rate_total else 0
    )

    # PROCEED RATE
    rate_proceed = sum(1 for l in rate_leads if l.status == "proceed")
    proceed_rate = (
        (rate_proceed / rate_total) * 100
        if rate_total else 0
    )

    # LEAD VELOCITY
    hours_passed = max(
        (now - today_start).total_seconds() / 3600,
        1
    )

    lead_velocity = today_count / hours_passed

    # TOP SPECIALISTS
    leaderboard_query = (
        db.query(
            LeadIntake.lead_accepted_by,
            func.count(LeadIntake.id)
        )
        .filter(
            LeadIntake.lead_accepted == True,
            LeadIntake.lead_accepted_by.isnot(None),
            LeadIntake.received_at >= today_start
        )
        .group_by(LeadIntake.lead_accepted_by)
        .order_by(func.count(LeadIntake.id).desc())
        .limit(3)
        .all()
    )

    top_specialists = [
        {
            "name": row[0],
            "intakes": row[1]
        }
        for row in leaderboard_query
    ]

    # LLM PAYLOAD
    analytics_payload = {
        "today_leads": today_count,
        "pending": pending,
        "escalated": escalated,
        "proceed": proceed,
        "declined": declined,
        "avg_review_minutes": round(avg_review, 1),
        "nafi_rate": round(nafi_rate, 1),
        "proceed_rate": round(proceed_rate, 1),
        "lead_velocity_per_hour": round(lead_velocity, 2),
        "top_specialists": top_specialists
    }

    # GENERATE DAILY BRIEF
    brief = await generate_daily_brief(analytics_payload)

    return {
        "date": today_start.date(),
        "daily_brief": brief
    }

@router.get("/email/history")
async def get_email_history(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    query = db.query(LeadIntake).filter(
        LeadIntake.email_sent == True
    )

    if start_date:
        query = query.filter(
            LeadIntake.lead_email_sent_at >= start_date
        )

    if end_date:
        query = query.filter(
            LeadIntake.lead_email_sent_at <= end_date
        )

    if status:
        query = query.filter(
            LeadIntake.status == status
        )

    leads = query.order_by(
        LeadIntake.lead_email_sent_at.desc()
    ).all()

    return {
        "total_emails_sent": len(leads),

        "breakdown": {
            "proceed": len([l for l in leads if l.status == "proceed"]),
            "declined": len([l for l in leads if l.status == "declined"]),
            "escalated": len([l for l in leads if l.status == "escalated"])
        },

        "history": [
            {
                "lead_id": l.id,
                "lead_name": f"{l.first_name} {l.last_name}",
                "status": l.status,
                "decline_type": l.decline_type,
                "reviewed_by": l.reviewed_by,
                "reviewed_at": l.reviewed_at,
                "email_sent_at": l.lead_email_sent_at,

                "recipient": (
                    l.assigned_users
                )
            }
            for l in leads
        ]
    }


@router.post("/normalize-state-of-incident")
async def normalize_state_of_incident_api(
    db: Session = Depends(get_db)
):
    try:

        leads = db.query(LeadIntake).all()

        updated = 0
        changes = []

        for lead in leads:

            old_value = lead.state_of_incident

            new_value = normalize_state_of_incident(
                old_value
            )

            if (
                new_value
                and old_value != new_value
            ):
                lead.state_of_incident = new_value

                changes.append({
                    "lead_id": lead.id,
                    "old_value": old_value,
                    "new_value": new_value
                })

                updated += 1

        db.commit()

        return {
            "success": True,
            "updated_count": updated,
            "changes": changes
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

@router.patch("/leads/{lead_id}/case-type")
async def update_case_type(lead_id: int,  case_type: str = Body(... , embed = True),
    db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):

    lead = db.query(LeadIntake).filter(LeadIntake.id == lead_id).first()

    if not lead:
        raise HTTPException(
            status_code=404,
            detail="Lead not found"
        )
    
    case_type = case_type.strip()

    if not case_type:
        raise HTTPException(
            status_code=404,
            detail="Lead not found"
        )
    
    old_case_type = lead.case_type

    lead.case_type = case_type 

    db.commit()
    db.refresh(lead) 

    return{
        "message": "Case type updated successfully",
        "lead_id": lead.id,
        "old_case_type": old_case_type,
        "new_case_type": lead.case_type
    }

@router.post("/neos/sync-status")
async def sync_neos_status(
    lead_ids: Optional[List[int]] = Body(default=None),
    current_user: dict = Depends(get_current_user)
):
    db: Session = SessionLocal()

    updated_count = 0
    unchanged_count = 0
    failed_count = 0
    errors = []

    try:
        query = db.query(LeadIntake).filter(
            LeadIntake.neos_updated.is_(True)
        )

        if lead_ids:
            query = query.filter(LeadIntake.id.in_(lead_ids))

        leads = query.order_by(LeadIntake.id.asc()).all()

        if not leads:
            raise HTTPException(
                status_code=404,
                detail="No matching leads found."
            )

        total_found = len(leads)

        print(
            f"[Neos Sync] Found {total_found} eligible leads"
        )

        for lead in leads:
            try:
                if not lead.first_name or not lead.last_name:
                    failed_count += 1
                    errors.append({
                        "lead_id": lead.id,
                        "error": "Missing first name or last name"
                    })
                    continue

                print(
                    f"[Neos Sync] Updating lead "
                    f"{lead.id}: {lead.first_name} {lead.last_name}"
                )

                status = await get_lead_status(
                    lead.first_name,
                    lead.last_name
                )

                if not status:
                    db.rollback()
                    failed_count += 1
                    errors.append({
                        "lead_id": lead.id,
                        "error": "Lead not found in Neos or status unavailable"
                    })
                    continue

                old_status = lead.neos_status
                old_assigned = lead.assigned_to

                new_status = (
                    f"{status['intake_stage']} | "
                    f"{status['intake_status']}"
                )
                new_assigned = status.get("assigned")

                lead.neos_status = new_status
                lead.assigned_to = new_assigned

                apply_agreement_sent(lead, new_status)

                db.commit()

                if (
                    old_status != new_status
                    or old_assigned != new_assigned
                ):
                    updated_count += 1
                    print(
                        f"[Neos Sync] Updated lead {lead.id}: "
                        f"{new_status}, assigned={new_assigned}"
                    )
                else:
                    unchanged_count += 1
                    print(
                        f"[Neos Sync] Lead {lead.id} is already current"
                    )

            except Exception as e:
                db.rollback()
                failed_count += 1

                errors.append({
                    "lead_id": lead.id,
                    "error": str(e)
                })

                print(
                    f"[Neos Sync] Failed to update lead "
                    f"{lead.id}: {e}"
                )

                continue

        print(
            f"[Neos Sync] Completed: total={total_found}, "
            f"updated={updated_count}, "
            f"unchanged={unchanged_count}, "
            f"failed={failed_count}"
        )

        return {
            "message": "Neos status sync completed",
            "total_found": total_found,
            "updated_count": updated_count,
            "unchanged_count": unchanged_count,
            "failed_count": failed_count,
            "errors": errors
        }

    finally:
        db.close()

def _count_unbackfilled(db: Session) -> int:
    """Leads that still carry a legacy Agree/Disagree but have no reviewer_decision yet."""
    legacy = getattr(LeadIntake, "agree_disagree", None)
    if legacy is None:          # legacy column already dropped -> nothing to protect
        return 0
    return (
        db.query(LeadIntake)
        .filter(legacy.isnot(None), LeadIntake.reviewer_decision.is_(None))
        .count()
    )


@router.post("/lead-review/reclassify")
async def reclassify_leads(
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    # A re-run can flip statuses; once a lead flips, the original Agree/Disagree pairing is
    # unrecoverable. Refuse to run until every saved answer has been converted.
    pending = _count_unbackfilled(db)
    if pending:
        raise HTTPException(
            status_code=409,
            detail=f"{pending} leads have a saved Agree/Disagree that is not backfilled yet. "
                   f"Run POST /leads/agreement-backfill first.",
        )
    try:
        return await process_lead_reviews(start_date, end_date)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _norm(s: str | None) -> str:
    return " ".join((s or "").lower().replace(",", " ").split())


class SignedRow(BaseModel):
    neos_number: Optional[str] = None
    first_name: str
    last_name: str
    case_type: Optional[str] = None
    signed_at: Optional[datetime] = None


@router.post("/agreements/signed/import")
async def import_signed_agreements(
    rows: List[SignedRow],
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    summary = {"matched": [], "needs_review": [], "name_mismatch": [], "unmatched": []}

    for r in rows:
        full = _norm(f"{r.first_name} {r.last_name}")
        lead = None
        needs_review = False

        # 1) NEOS intake number -> neos_case_number, then confirm the name
        if r.neos_number:
            cand = (
                db.query(LeadIntake)
                .filter(LeadIntake.neos_case_number == str(r.neos_number))
                .first()
            )
            if cand:
                if _norm(f"{cand.first_name} {cand.last_name}") == full:
                    lead = cand
                else:
                    # number matches, name doesn't: report, do NOT flag
                    db.add(AgreementSignedUnmatched(
                        neos_number=r.neos_number, first_name=r.first_name,
                        last_name=r.last_name, case_type=r.case_type,
                        signed_at=r.signed_at, reason="name_mismatch",
                        matched_lead_id=cand.id))
                    summary["name_mismatch"].append(r.neos_number)
                    continue

        # 2) Fallback only when NEOS Case # is blank on the lead:
        #    full name + case type + date, flagged for review
        if lead is None:
            q = db.query(LeadIntake).filter(
                func.lower(LeadIntake.first_name) == r.first_name.strip().lower(),
                func.lower(LeadIntake.last_name) == r.last_name.strip().lower(),
                or_(LeadIntake.neos_case_number.is_(None),
                    LeadIntake.neos_case_number == ""),
            )
            if r.case_type:
                q = q.filter(LeadIntake.case_type == r.case_type)
            if r.signed_at:
                q = q.filter(LeadIntake.received_at <= r.signed_at)
            matches = q.order_by(LeadIntake.received_at.desc()).all()
            if len(matches) == 1:
                lead, needs_review = matches[0], True

        if lead is None:
            # 5) never create leads; log unmatched
            db.add(AgreementSignedUnmatched(
                neos_number=r.neos_number, first_name=r.first_name,
                last_name=r.last_name, case_type=r.case_type,
                signed_at=r.signed_at, reason="no_lead"))
            summary["unmatched"].append(r.neos_number)
            continue

        lead.agreement_signed = True                    
        lead.agreement_signed_at = r.signed_at or lead.agreement_signed_at
        lead.agreement_signed_neos_number = r.neos_number
        lead.agreement_signed_needs_review = needs_review
        (summary["needs_review"] if needs_review else summary["matched"]).append(lead.id)

    db.commit()
    return {k: len(v) for k, v in summary.items()} | {"detail": summary} 

def build_agreement_rates(db: Session, start: datetime | None = None, end: datetime | None = None) -> dict:
    q = db.query(
        LeadIntake.status,
        func.count(LeadIntake.id),
        func.sum(case((LeadIntake.agreement_sent.is_(True), 1), else_=0)),
        func.sum(case((LeadIntake.agreement_signed.is_(True), 1), else_=0)),
    )
    if start:
        q = q.filter(LeadIntake.received_at >= start)
    if end:
        q = q.filter(LeadIntake.received_at <= end)
    rows = q.group_by(LeadIntake.status).all()

    by_decision = {}
    total_all = sent_all = signed_all = 0
    for status, total, sent, signed in rows:
        sent, signed = int(sent or 0), int(signed or 0)
        total_all += total
        sent_all += sent
        signed_all += signed
        by_decision[status or "none"] = {
            "total": total,
            "sent": sent,
            "signed": signed,
            "sent_rate": round(sent / total * 100, 1) if total else 0,
            "signed_rate": round(signed / total * 100, 1) if total else 0,
        }

    proceed = by_decision.get("proceed", {})
    declined = by_decision.get("declined", {})
    denom = proceed.get("sent", 0) + declined.get("sent", 0)

    return {
        "by_decision": by_decision,
        "totals": {
            "leads": total_all,
            "sent": sent_all,
            "signed": signed_all,
            "sent_rate": round(sent_all / total_all * 100, 1) if total_all else 0,
            "signed_rate": round(signed_all / total_all * 100, 1) if total_all else 0,
        },
        # spec metric: Agreements Sent: Agent Proceed (%)
        "agreements_sent_agent_proceed_pct":
            round(proceed.get("sent", 0) / denom * 100, 1) if denom else None,
    } 

class AgreeDisagreeRequest(BaseModel):
    decision: Literal["Agree", "Disagree"] 

@router.patch("/leads/{lead_id}/agree-disagree")
async def update_agree_disagree(
    lead_id: int,
    payload: AgreeDisagreeRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    lead = (
        db.query(LeadIntake)
        .filter(LeadIntake.id == lead_id)
        .first()
    )

    if not lead:
        raise HTTPException(
            status_code=404,
            detail="Lead not found",
        )

    # Store the reviewer's own decision; Agree/Disagree is derived from it.
    try:
        answered_by = (
            (current_user.get("email") or current_user.get("username"))
            if isinstance(current_user, dict) else None
        )
        record_reviewer_answer(lead, payload.decision, answered_by=answered_by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    db.commit()
    db.refresh(lead)

    return {
        "lead_id": lead.id,
        "agree_disagree": derive_agree_disagree(lead),
        "reviewer_decision": lead.reviewer_decision,
        "reviewer_answer_at": lead.reviewer_answer_at,
        "agent_status_at_answer": lead.agent_status_at_answer,
        "agent_version_at_answer": lead.agent_version_at_answer,
    }


@router.post("/leads/agreement-backfill")
async def backfill_agreement(
    dry_run: bool = Query(True, description="True = only report, nothing is saved"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    legacy = getattr(LeadIntake, "agree_disagree", None)
    if legacy is None:
        raise HTTPException(status_code=400, detail="Legacy agree_disagree column not found.")

    counts: dict = {}
    cannot_convert: list = []
    for lead in db.query(LeadIntake).filter(legacy.isnot(None)).all():
        result = backfill_reviewer_decision(lead)      # uses the status as it is NOW, version v2.8
        counts[result] = counts.get(result, 0) + 1
        if result == "cannot_convert":
            cannot_convert.append({"lead_id": lead.id, "status": lead.status, "stored": lead.agree_disagree})

    if dry_run:
        db.rollback()
    else:
        db.commit()

    return {
        "dry_run": dry_run,
        "counts": counts,
        "needs_manual_review": cannot_convert,
    }


@router.get("/leads/agreement-report")
async def get_agreement_report(
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    query = db.query(LeadIntake).filter(LeadIntake.reviewer_decision.isnot(None))
    if start_date:
        query = query.filter(LeadIntake.received_at >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        query = query.filter(LeadIntake.received_at <= datetime.combine(end_date, datetime.max.time()))
    return agreement_summary(query.all())

@router.post("/neos/update-pending")
async def update_pending_neos_leads(
    lead_ids: Optional[List[int]] = Body(default=None),
    current_user: dict = Depends(get_current_user),
):
    if lead_ids is not None and len(lead_ids) == 0:
        raise HTTPException(status_code=400, detail="Provide at least one lead ID or omit the body.")

    try:
        result = await process_neos_update_for_leads(lead_ids=lead_ids)
        return {
            "message": "Pending Neos updates completed",
            **result,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to process pending Neos updates: {str(e)}") 

DENVER_TZ = ZoneInfo("America/Denver")

class OwlExternalStartRequest(BaseModel):
    requested_by: str = Field(..., min_length=1)
    instruction: str = Field(default="Owl review", min_length=1)
    start_date: date | None = None
    end_date: date | None = None
    statuses: list[str] = Field(default_factory=list)
    limit: int | None = Field(
        default=None,
        ge=1,
        description="Maximum leads to review. Null means unlimited."
    )
    lead_ids: list[int] | None = None
    all_in_review: bool = False


class OwlUnlockRequest(BaseModel):
    code: str = Field(..., min_length=1)


class OwlOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(..., min_length=1)
    verdict: Optional[Literal["agree", "disagree"]] = None
    reason: Optional[str] = Field(default=None, max_length=1000)
    confidence: Optional[int] = Field(
        default=None, strict=True, ge=1, le=5
    )


class OwlExternalStopRequest(BaseModel):
    run_id: Optional[str] = None
    force: bool = False


# External authentication

def verify_external_owl_key(
    authorization: Optional[str] = Header(None),
):
    expected_key = (os.getenv("INTAKEOPS_REVIEW_KEY") or "").strip()
    if not expected_key:
        raise HTTPException(
            status_code=500,
            detail="Owl API key is not configured.",
        )
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing external API key.")

    scheme, separator, supplied_key = authorization.partition(" ")
    if (
        scheme.lower() != "bearer"
        or not separator
        or not supplied_key.strip()
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid authorization format.",
        )
    if not hmac.compare_digest(supplied_key.strip(), expected_key):
        raise HTTPException(status_code=401, detail="Invalid API key.")
    return True


# ---------------------------------------------------------
# Start Owl review
# ---------------------------------------------------------

@router.post("/owl-review/external/start")
async def external_start_owl_review(
    payload: OwlExternalStartRequest,
    db: Session = Depends(get_db),
    authorized: bool = Depends(verify_external_owl_key),
):
    start_date = payload.start_date
    end_date = payload.end_date

    # -------------------------------------------------
    # Validate all_in_review
    # -------------------------------------------------
    if payload.all_in_review:

        if payload.lead_ids is not None:
            raise HTTPException(
                status_code=400,
                detail="all_in_review cannot be combined with lead_ids.",
            )

        if start_date is not None or end_date is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Do not provide start_date or end_date "
                    "when all_in_review is true."
                ),
            )

        # No date restriction: process all eligible leads.
        start_date = None
        end_date = None

    # -------------------------------------------------
    # Validate explicit lead IDs
    # -------------------------------------------------
    elif payload.lead_ids is not None:

        if not payload.lead_ids:
            raise HTTPException(
                status_code=400,
                detail="lead_ids cannot be empty.",
            )

        if any(lead_id <= 0 for lead_id in payload.lead_ids):
            raise HTTPException(
                status_code=400,
                detail="All lead IDs must be positive integers.",
            )

        if len(payload.lead_ids) != len(set(payload.lead_ids)):
            raise HTTPException(
                status_code=400,
                detail="Duplicate lead IDs are not allowed.",
            )

        if payload.limit is not None:
            raise HTTPException(
                status_code=400,
                detail="Use either lead_ids or limit, not both.",
            )

    # -------------------------------------------------
    # Validate date-range requests
    # -------------------------------------------------
    else:

        if bool(start_date) != bool(end_date):
            raise HTTPException(
                status_code=400,
                detail="Provide both start_date and end_date, or neither.",
            )

        if start_date and end_date:

            if start_date > end_date:
                raise HTTPException(
                    status_code=400,
                    detail="start_date cannot be after end_date.",
                )

            denver_today = datetime.now(DENVER_TZ).date()

            if end_date > denver_today:
                raise HTTPException(
                    status_code=400,
                    detail="Future dates are not allowed (Denver local date).",
                )

    # -------------------------------------------------
    # Prepare Owl run
    # -------------------------------------------------
    try:

        run_data = prepare_owl_run(
            db=db,
            requested_by=payload.requested_by.strip(),
            start_date=start_date,
            end_date=end_date,
            statuses=payload.statuses or None,
            instruction=payload.instruction.strip(),
            limit=payload.limit,
            lead_ids=payload.lead_ids,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    except RuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        )

    # -------------------------------------------------
    # Start selected leads
    # -------------------------------------------------
    run_id = run_data["run_id"]
    selected_lead_ids = run_data["lead_ids"]
    selected_count = len(selected_lead_ids)

    if selected_count:
        start_owl_run_task(
            run_id=run_id,
            started_by=payload.requested_by.strip(),
            instruction=payload.instruction.strip(),
            lead_ids=selected_lead_ids,
        )

    # -------------------------------------------------
    # Prepare response message
    # -------------------------------------------------
    effective_limit = run_data["effective_limit"]

    if payload.lead_ids is not None:

        previously_reviewed = (
            run_data["previously_reviewed_selected"]
        )

        message = (
            f"Explicit rerun started. {selected_count} leads selected. "
            f"{previously_reviewed} were previously reviewed by Owl "
            "and will be reviewed again."
        )

    else:

        available = run_data["in_range"]
        considered = run_data["considered"]
        already_reviewed = run_data["already_reviewed_skipped"]
        human_skipped = run_data["human_reviewed_skipped"]
        pending = selected_count

        if available == 0:

            message = (
                "No leads matched the selected date range "
                "and statuses. Nothing to review."
            )

        else:

            if effective_limit is None:
                message = (
                    f"Found {available} leads matching the "
                    "selected date range and statuses. "
                    "Unlimited review is enabled."
                )

            elif available < effective_limit:
                message = (
                    f"Only {available} leads match the selected "
                    f"date range and statuses, although the requested "
                    f"limit was {effective_limit}."
                )

            else:
                message = (
                    f"Found {available} leads matching the "
                    "selected date range and statuses."
                )

            message += f" Considered {considered} leads."

            if already_reviewed:
                message += (
                    f" {already_reviewed} were already reviewed "
                    "by Owl and skipped."
                )

            if human_skipped:
                message += (
                    f" {human_skipped} were excluded because "
                    "of existing human review."
                )

            if pending:
                message += (
                    f" {pending} pending leads were selected "
                    "for Owl review."
                )
            else:
                message += (
                    " No pending leads are available for review."
                )

            if run_data["beyond_limit"]:
                message += (
                    f" {run_data['beyond_limit']} additional matching "
                    "leads were not selected because of the limit."
                )

    # -------------------------------------------------
    # Return response
    # -------------------------------------------------
    return {
        "success": True,
        "message": message,

        "run_id": run_id,
        "status": "queued" if selected_count else "completed",

        "selected": selected_count,

        # None means unlimited.
        "limit": effective_limit,
        "requested_limit": payload.limit,
        "unlimited": effective_limit is None,

        "in_date_range": run_data["in_range"],
        "considered": run_data["considered"],

        "already_reviewed_skipped": (
            run_data["already_reviewed_skipped"]
        ),

        "human_reviewed_skipped": (
            run_data["human_reviewed_skipped"]
        ),

        "human_reviewed": run_data["human_reviewed"],

        "previously_reviewed_selected": (
            run_data["previously_reviewed_selected"]
        ),

        "pending": selected_count,

        "not_considered_due_to_limit": (
            run_data["beyond_limit"]
        ),

        "outside_date_range": 0,

        "completed": 0,
        "remaining": selected_count,

        "instruction": payload.instruction,

        "date_range": {
            "from": start_date.isoformat() if start_date else None,
            "to": end_date.isoformat() if end_date else None,
            "timezone": "America/Denver",
        },

        "statuses": run_data["statuses"],
    }



# ---------------------------------------------------------
# Stop Owl review
# ---------------------------------------------------------

@router.post("/owl-review/external/stop")
async def external_stop_owl_review(
    payload: OwlExternalStopRequest,
    authorized: bool = Depends(verify_external_owl_key),
):
    result = request_stop_owl_run(
        run_id=payload.run_id,
        force=payload.force,
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Run not found." if payload.run_id else "No active run to stop.",
        )
    return {"success": True, **result}


# ---------------------------------------------------------
# Owl run status
# ---------------------------------------------------------

@router.get("/owl-review/external/status")
async def external_owl_review_status(
    authorized: bool = Depends(verify_external_owl_key),
    db: Session = Depends(get_db),
):
    return owl_status(db)


@router.get("/owl-review/external/status/{run_id}")
async def external_owl_run_status(
    run_id: str,
    authorized: bool = Depends(verify_external_owl_key),
):
    refresh_owl_runs()
    result = owl_run_status(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return result

@router.post("/owl-review/unlock")
async def unlock_owl_edit(
    payload: OwlUnlockRequest,
    current_user: dict = Depends(get_current_user),
):
    verify_edit_code(payload.code, user_email(current_user))
    return {
        "unlocked": True,
        "message": "OWL editing access verified.",
    }

@router.patch("/leads/{lead_id}/owl-review")
def override_owl_review(
    lead_id: int,
    payload: OwlOverrideRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    who = user_email(current_user)
    verify_edit_code(payload.code, who)
    result = apply_owl_override(
        db=db,
        lead_id=lead_id,
        verdict=payload.verdict,
        reason=payload.reason,
        confidence=payload.confidence,
        who=who,
    )
    return {"success": True, "lead_id": lead_id, **result}

# Historical/training data

@router.get("/leads/training-data")
async def get_leads_training_data(
    skip: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=1000),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    db: Session = Depends(get_db),
    authorized: bool = Depends(verify_external_owl_key),
):
    """Fetch historical leads with optional Denver-calendar date filters."""
    if bool(start_date) != bool(end_date):
        raise HTTPException(
            status_code=400,
            detail="Provide both start_date and end_date, or neither.",
        )
    if start_date and end_date and start_date > end_date:
        raise HTTPException(
            status_code=400,
            detail="start_date cannot be after end_date.",
        )

    start_utc, end_utc = denver_date_bounds_utc(start_date, end_date)

    query = db.query(
        LeadIntake,
        exists().where(
            LeadReferral.lead_id == LeadIntake.id
        ).label("is_referred"),
    )
    if start_utc is not None:
        query = query.filter(LeadIntake.received_at >= start_utc)
    if end_utc is not None:
        query = query.filter(LeadIntake.received_at < end_utc)

    total = query.order_by(None).count()
    leads = (
        query.order_by(LeadIntake.received_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    owl_by_lead = load_owl_reviews(db, [lead.id for lead, _ in leads])
    # keila_by_lead = load_keila_reviews(db, [lead.id for lead, _ in leads])
    result = []
    for lead, is_referred in leads:
        owl = owl_by_lead.get(lead.id)
        # keila = keila_by_lead.get(lead.id)
        result.append({
            "id": lead.id,
            "lead_id": lead.id,
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
            "lead_accepted": lead.lead_accepted,
            "lead_accepted_by": lead.lead_accepted_by,
            "is_referral_linked": bool(is_referred),
            "neos_updated": lead.neos_updated,
            "neos_case_number": lead.neos_case_number,
            "neos_status": lead.neos_status,
            "neos_assigned_to": lead.assigned_to,
            "agreement_sent": lead.agreement_sent,
            "agreement_sent_at": lead.agreement_sent_at,
            "agreement_signed": lead.agreement_signed,
            "agreement_signed_at": lead.agreement_signed_at,
            "agreement_signed_neos_number": lead.agreement_signed_neos_number,
            "agreement_signed_needs_review": lead.agreement_signed_needs_review,
            "agree_disagree": derive_agree_disagree(lead),
            "reviewer_decision": lead.reviewer_decision,
            "reviewer_answer_at": lead.reviewer_answer_at,
            "agent_status_at_answer": lead.agent_status_at_answer,
            "agent_version_at_answer": lead.agent_version_at_answer,
            **owl_fields(owl),
            # **keila_fields(keila)
        })

    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "returned": len(result),
        "has_more": (skip + len(result)) < total,
        "data": result,
    }


# # =========================================================
# # KEILA REVIEW - NORMAL INTAKEOPS LOGIN AUTHENTICATION
# # =========================================================

# KEILA_EMAIL = "keilakarolan.ma@gmail.com"


# def require_keila_user(
#     current_user=Depends(get_current_user)
# ):
#     email = user_email(current_user)

#     if not email or email.lower() != KEILA_EMAIL.lower():
#         raise HTTPException(
#             status_code=403,
#             detail="You are not authorized to review leads as Keila."
#         )

#     return email


# # =========================================================
# # KEILA BATCH FILTER REQUEST
# # =========================================================

# class KeilaBatchRequest(BaseModel):
#     start_date: Optional[date] = None
#     end_date: Optional[date] = None
#     statuses: Optional[List[str]] = None
#     limit: Optional[int] = Field(default=None, ge=1)
#     all_in_review: bool = False
#     lead_ids: Optional[List[int]] = None


# # =========================================================
# # KEILA DECISION REQUEST
# # =========================================================

# class KeilaDecisionRequest(BaseModel):
#     lead_id: int
#     agrees_with_owl: bool
#     new_decision: Optional[str] = None
#     reason: Optional[str] = None

#     @model_validator(mode="after")
#     def validate_disagreement(self):

#         if not self.agrees_with_owl:

#             if (
#                 not self.new_decision
#                 or not self.new_decision.strip()
#             ):
#                 raise ValueError(
#                     "new_decision is required when Keila disagrees"
#                 )

#             if (
#                 not self.reason
#                 or not self.reason.strip()
#             ):
#                 raise ValueError(
#                     "reason is required when Keila disagrees"
#                 )

#         return self


# # =========================================================
# # KEILA BATCH LEAD FETCH
# # Uses the same date/status/limit filters as Owl.
# # =========================================================

# DECISIONS = {"proceed", "declined", "no_decision"}

# @router.post("/owl-review/external/keila/batch")
# def get_keila_review_batch(
#     payload: KeilaBatchRequest,
#     db: Session = Depends(get_db),
#     keila_email: str = Depends(require_keila_user),
# ):

#     start_date = payload.start_date
#     end_date = payload.end_date

#     # -----------------------------------------------------
#     # Validate all_in_review
#     # -----------------------------------------------------

#     if payload.all_in_review:

#         if payload.lead_ids is not None:
#             raise HTTPException(
#                 status_code=400,
#                 detail="all_in_review cannot be combined with lead_ids."
#             )

#         if start_date is not None or end_date is not None:
#             raise HTTPException(
#                 status_code=400,
#                 detail="Do not provide dates when all_in_review is true."
#             )

#         start_date = None
#         end_date = None

#     # -----------------------------------------------------
#     # Validate explicit lead IDs
#     # -----------------------------------------------------

#     elif payload.lead_ids is not None:

#         if not payload.lead_ids:
#             raise HTTPException(
#                 status_code=400,
#                 detail="lead_ids cannot be empty."
#             )

#         if any(lead_id <= 0 for lead_id in payload.lead_ids):
#             raise HTTPException(
#                 status_code=400,
#                 detail="All lead IDs must be positive integers."
#             )

#         if len(payload.lead_ids) != len(set(payload.lead_ids)):
#             raise HTTPException(
#                 status_code=400,
#                 detail="Duplicate lead IDs are not allowed."
#             )

#         if payload.limit is not None:
#             raise HTTPException(
#                 status_code=400,
#                 detail="Use either lead_ids or limit, not both."
#             )

#     # -----------------------------------------------------
#     # Validate date range
#     # -----------------------------------------------------

#     else:

#         if bool(start_date) != bool(end_date):
#             raise HTTPException(
#                 status_code=400,
#                 detail="Provide both start_date and end_date, or neither."
#             )

#         if start_date and end_date:

#             if start_date > end_date:
#                 raise HTTPException(
#                     status_code=400,
#                     detail="start_date cannot be after end_date."
#                 )

#             denver_today = datetime.now(DENVER_TZ).date()

#             if end_date > denver_today:
#                 raise HTTPException(
#                     status_code=400,
#                     detail="Future dates are not allowed."
#                 )

#     # -----------------------------------------------------
#     # Same default statuses as Owl
#     # -----------------------------------------------------

#     selected_statuses = (
#         payload.statuses
#         if payload.statuses
#         else list(DECISIONS)
#     )

#     # -----------------------------------------------------
#     # Build base query
#     #
#     # Keila only receives leads:
#     # 1. Already reviewed by Owl.
#     # 2. Not yet reviewed by Keila.
#     # 3. Not protected by human review.
#     # 4. Matching Owl's date/status filters.
#     # -----------------------------------------------------

#     query = db.query(LeadIntake).join(
#         LeadOwlReview,
#         LeadOwlReview.lead_id == LeadIntake.id
#     ).filter(
#         LeadIntake.status.in_(selected_statuses)
#     )

#     # Same Denver date conversion used by Owl
#     start_utc, end_utc = denver_date_bounds_utc(
#         start_date,
#         end_date
#     )

#     if start_utc is not None:
#         query = query.filter(
#             LeadIntake.received_at >= start_utc
#         )

#     if end_utc is not None:
#         query = query.filter(
#             LeadIntake.received_at < end_utc
#         )

#     # Explicit IDs, if provided
#     if payload.lead_ids is not None:
#         query = query.filter(
#             LeadIntake.id.in_(payload.lead_ids)
#         )

#     # -----------------------------------------------------
#     # Protect human-reviewed leads
#     # -----------------------------------------------------

#     query = query.filter(
#         LeadIntake.reviewer_decision.is_(None),
#         LeadOwlReview.overridden.is_not(True),
#         LeadOwlReview.overridden_by.is_(None)
#     )

#     # Preserve legacy human-review protection
#     legacy = getattr(LeadIntake, "agree_disagree", None)

#     if legacy is not None:
#         query = query.filter(
#             legacy.is_(None)
#         )

#     # -----------------------------------------------------
#     # Exclude leads already reviewed by Keila
#     # -----------------------------------------------------

#     query = query.filter(
#         ~exists().where(
#             LeadKeilaReview.lead_id == LeadIntake.id
#         )
#     )

#     # Count eligible leads before applying limit
#     total_eligible = query.count()

#     # Same ordering as Owl
#     query = query.order_by(
#         LeadIntake.received_at.desc(),
#         LeadIntake.id.desc()
#     )

#     if payload.limit is not None:
#         query = query.limit(payload.limit)

#     leads = query.all()

#     # -----------------------------------------------------
#     # Prepare response
#     # -----------------------------------------------------

#     results = []

#     for lead in leads:

#         owl_review = db.query(LeadOwlReview).filter(
#             LeadOwlReview.lead_id == lead.id
#         ).first()

#         results.append({

#             "lead_id": lead.id,

#             "first_name": lead.first_name,
#             "last_name": lead.last_name,
#             "email": lead.email,
#             "phone": lead.phone,

#             "status": lead.status,
#             "case_type": lead.case_type,
#             "state_of_incident": lead.state_of_incident,
#             "case_description": lead.case_description,

#             "received_at": (
#                 lead.received_at.isoformat()
#                 if lead.received_at else None
#             ),

#             # Owl's original decision and review
#             "owl_decision": owl_review.owl_decision,
#             "owl_verdict": owl_review.verdict,
#             "owl_reason": owl_review.reason,
#             "owl_confidence": owl_review.confidence,

#             "owl_reviewed_at": (
#                 owl_review.created_at.isoformat()
#                 if owl_review.created_at else None
#             )
#         })

#     return {

#         "success": True,

#         "reviewer": keila_email,

#         "total_eligible": total_eligible,
#         "selected": len(results),

#         "remaining": max(
#             0,
#             total_eligible - len(results)
#         ),

#         "filters": {
#             "start_date": (
#                 start_date.isoformat()
#                 if start_date else None
#             ),

#             "end_date": (
#                 end_date.isoformat()
#                 if end_date else None
#             ),

#             "statuses": selected_statuses,
#             "limit": payload.limit,
#             "all_in_review": payload.all_in_review,
#             "timezone": "America/Denver"
#         },

#         "data": results
#     }


# # =========================================================
# # SAVE KEILA DECISION
# # =========================================================

# @router.post("/owl-review/external/keila-decision")
# def save_keila_decision(
#     payload: KeilaDecisionRequest,
#     db: Session = Depends(get_db),
#     keila_email: str = Depends(require_keila_user)
# ):

#     # -----------------------------------------------------
#     # Find lead
#     # -----------------------------------------------------

#     lead = db.query(LeadIntake).filter(
#         LeadIntake.id == payload.lead_id
#     ).first()

#     if not lead:
#         raise HTTPException(
#             status_code=404,
#             detail="Lead not found."
#         )

#     # -----------------------------------------------------
#     # Find Owl review
#     # -----------------------------------------------------

#     owl_review = db.query(LeadOwlReview).filter(
#         LeadOwlReview.lead_id == payload.lead_id
#     ).first()

#     if not owl_review:
#         raise HTTPException(
#             status_code=409,
#             detail="Owl has not reviewed this lead."
#         )

#     # -----------------------------------------------------
#     # Do not allow human-reviewed leads to be changed
#     # -----------------------------------------------------

#     legacy = getattr(lead, "agree_disagree", None)

#     if (
#         lead.reviewer_decision is not None
#         or owl_review.overridden
#         or owl_review.overridden_by is not None
#         or (
#             legacy is not None
#             and getattr(lead, "agree_disagree", None) is not None
#         )
#     ):
#         raise HTTPException(
#             status_code=409,
#             detail="This lead has a human review and cannot be reviewed by Keila."
#         )

#     # -----------------------------------------------------
#     # Prevent duplicate Keila reviews
#     # -----------------------------------------------------

#     existing_review = db.query(LeadKeilaReview).filter(
#         LeadKeilaReview.lead_id == payload.lead_id
#     ).first()

#     if existing_review:
#         raise HTTPException(
#             status_code=409,
#             detail="Keila has already reviewed this lead."
#         )

#     # -----------------------------------------------------
#     # Keila agrees with Owl
#     # -----------------------------------------------------

#     if payload.agrees_with_owl:

#         if not owl_review.owl_decision:
#             raise HTTPException(
#                 status_code=409,
#                 detail="Owl's lead decision is missing."
#             )

#         keila_decision = owl_review.owl_decision

#         # Reason is optional when Keila agrees
#         reason = (
#             payload.reason.strip()
#             if payload.reason and payload.reason.strip()
#             else None
#         )

#     # -----------------------------------------------------
#     # Keila disagrees with Owl
#     # -----------------------------------------------------

#     else:

#         keila_decision = payload.new_decision.strip()
#         reason = payload.reason.strip()

#     # -----------------------------------------------------
#     # Save Keila review separately
#     # -----------------------------------------------------

#     review = LeadKeilaReview(
#         lead_id=payload.lead_id,
#         keila_email=keila_email,
#         agrees_with_owl=payload.agrees_with_owl,
#         keila_decision=keila_decision,
#         reason=reason,
#         reviewed_at=datetime.now(timezone.utc)
#     )

#     try:

#         db.add(review)
#         db.commit()
#         db.refresh(review)

#     except Exception:

#         db.rollback()

#         raise HTTPException(
#             status_code=500,
#             detail="Could not save Keila's review."
#         )

#     return {

#         "success": True,

#         "message": "Keila review saved successfully.",

#         "data": {

#             "lead_id": review.lead_id,
#             "keila_email": review.keila_email,
#             "agrees_with_owl": review.agrees_with_owl,
#             "keila_decision": review.keila_decision,
#             "reason": review.reason,
#             "reviewed_at": review.reviewed_at.isoformat()
#         }
#     }


# # =========================================================
# # DAILY SUMMARY - DENVER TIMEZONE
# # =========================================================

# DENVER_TZ = ZoneInfo("America/Denver")


# @router.get("/owl-review/external/daily-summary")
# def get_owl_daily_summary(
#     db: Session = Depends(get_db),
#     authorized: bool = Depends(verify_external_owl_key),
# ):

#     now = datetime.now(DENVER_TZ)
#     today = now.date()

#     # Pass today as BOTH start and end, otherwise end_utc is None.
#     # The helper returns naive UTC bounds, matching how the database
#     # stores datetimes in this application.
#     start_utc, end_utc = denver_date_bounds_utc(today, today)

#     # -----------------------------------------------------
#     # Total leads received today
#     # -----------------------------------------------------

#     total_received = db.query(
#         func.count(LeadIntake.id)
#     ).filter(
#         LeadIntake.received_at >= start_utc,
#         LeadIntake.received_at < end_utc
#     ).scalar() or 0

#     # -----------------------------------------------------
#     # Leads not yet reviewed by Owl
#     # -----------------------------------------------------

#     not_reviewed_by_owl = db.query(
#         func.count(LeadIntake.id)
#     ).filter(
#         LeadIntake.received_at >= start_utc,
#         LeadIntake.received_at < end_utc,

#         ~exists().where(
#             LeadOwlReview.lead_id == LeadIntake.id
#         )
#     ).scalar() or 0

#     # -----------------------------------------------------
#     # Leads awaiting human validation
#     # -----------------------------------------------------

#     awaiting_query = db.query(
#         func.count(LeadOwlReview.id)
#     ).join(
#         LeadIntake,
#         LeadIntake.id == LeadOwlReview.lead_id
#     ).filter(
#         LeadIntake.received_at >= start_utc,
#         LeadIntake.received_at < end_utc,
#         LeadIntake.reviewer_decision.is_(None),
#         LeadOwlReview.overridden.is_not(True),
#         LeadOwlReview.overridden_by.is_(None)
#     )

#     # Preserve legacy human validation values
#     legacy = getattr(LeadIntake, "agree_disagree", None)

#     if legacy is not None:
#         awaiting_query = awaiting_query.filter(
#             legacy.is_(None)
#         )

#     awaiting_human_validation = (
#         awaiting_query.scalar() or 0
#     )

#     return {
#         "success": True,
#         "date": today.isoformat(),
#         "timezone": "America/Denver",
#         "count_timestamp": now.isoformat(),

#         "counts": {
#             "total_leads_received": total_received,
#             "not_reviewed_by_owl": not_reviewed_by_owl,
#             "awaiting_human_validation": awaiting_human_validation
#         }
#     }


# # =========================================================
# # LOAD LATEST KEILA REVIEWS
# # =========================================================

# def load_keila_reviews(db, lead_ids):
#     if not lead_ids:
#         return {}

#     rows = (
#         db.query(LeadKeilaReview)
#         .filter(
#             LeadKeilaReview.lead_id.in_(lead_ids)
#         )
#         .order_by(
#             LeadKeilaReview.reviewed_at.desc()
#         )
#         .all()
#     )

#     latest = {}

#     for row in rows:

#         if row.lead_id not in latest:
#             latest[row.lead_id] = row

#     return latest


# # KEILA FIELDS FOR FRONTEND

# def keila_fields(row):
#     if row is None:
#         return {
#             "keila_reviewed": False,
#             "keila_email": None,
#             "keila_agrees_with_owl": None,
#             "keila_decision": None,
#             "keila_reason": None,
#             "keila_reviewed_at": None,
#         }

#     return {
#         "keila_reviewed": True,
#         "keila_email": row.keila_email,
#         "keila_agrees_with_owl": row.agrees_with_owl,
#         "keila_decision": row.keila_decision,
#         "keila_reason": row.reason,
#         "keila_reviewed_at": row.reviewed_at,
#     }
