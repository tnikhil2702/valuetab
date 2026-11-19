# router.py
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.templating import Jinja2Templates
import csv
import io
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, case, or_
from datetime import datetime, date, timedelta
from typing import Optional, List
from collections import defaultdict
import logging
from db import (get_db, Matter, MatterEvent, ComputedDeadline, DeadlineAttorney, ActiveSession, Session, Whitelist)
from deadlineops.utils.deadline_engine import compute_deadlines, TriggerEvent
from deadlineops.utils.graph_sync import sync_matter_deadlines, delete_graph_event, verify_calendar_event, verify_graph_connection
from deadlineops.utils.rules_loader import load_all_rules, JURISDICTION_FILES
from auth_utils import authenticate_user, create_access_token, ACCESS_TOKEN_EXPIRE_MINUTES, get_current_user, reset_password, generate_otp, verify_otp, SessionLocal, create_password
from typing import Literal

logger = logging.getLogger(__name__)

router = APIRouter()
PROJECT_NAME = "deadlineops"
jinja_templates = Jinja2Templates(directory="deadlineops/templates")

async def get_date_range(range_type: str, start_date: datetime):
    if range_type == "weekly":
        start_dt = start_date.replace(hour=0,minute=0,second=0,microsecond=0)
        end_dt = start_dt + timedelta(days=7)

    elif range_type == "monthly":
        start_dt = start_date.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
        if start_dt.month == 12:
            end_dt = start_dt.replace(year=start_dt.year + 1,month=1)
        else:
            end_dt = start_dt.replace(month=start_dt.month + 1)

    else: 
        start_dt = start_date.replace(month=1,day=1,hour=0,minute=0,second=0,microsecond=0)
        end_dt = start_dt.replace(year=start_dt.year + 1)
    return start_dt, end_dt

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

@router.post("/matters")
async def create_matter(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    case_name = payload.get("case_name")
    jurisdiction = payload.get("jurisdiction")

    if not case_name:
        raise HTTPException(
            status_code=400,
            detail="case_name is required"
        )

    if not jurisdiction:
        raise HTTPException(
            status_code=400,
            detail="jurisdiction is required"
        )

    matter = Matter(
        case_name=case_name,
        jurisdiction=jurisdiction
    )

    db.add(matter)
    db.commit()
    db.refresh(matter)

    return {
        "success": True,
        "matter_id": matter.id,
        "case_name": matter.case_name,
        "jurisdiction": matter.jurisdiction
    }

@router.post("/events")
async def create_events(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Create events and compute deadlines.
    Deadlines are created but NOT synced to calendar until approved.
    """

    matter_id = payload.get("matter_id")
    events_payload = payload.get("events", [])

    if not matter_id:
        raise HTTPException(
            status_code=400,
            detail="matter_id is required"
        )

    if not events_payload:
        raise HTTPException(
            status_code=400,
            detail="events array is required"
        )

    matter = (
        db.query(Matter)
        .filter(Matter.id == matter_id)
        .first()
    )

    if not matter:
        raise HTTPException(
            status_code=404,
            detail="Matter not found"
        )

    trigger_events = []
    created_event_ids = []
    filing_status_map = {}   # ← NEW: collects complaint_filed_date status for engine

    for item in events_payload:
        event_type = item.get("event_type")
        event_date = item.get("event_date")
        notes = item.get("notes")
        filing_status = item.get("filing_status")   # ← NEW

        if not event_type or not event_date:
            continue

        # --- NEW: validate filing_status when event type requires it ---
        if event_type == "complaint_filed_date":
            if filing_status not in ("Planned", "Filed"):
                raise HTTPException(
                    status_code=400,
                    detail="filing_status must be 'Planned' or 'Filed' for complaint_filed_date"
                )

        try:
            parsed_date = datetime.strptime(event_date, "%Y-%m-%d").date()
        except Exception:
            raise HTTPException(status_code=400, detail=f"Invalid date format for {event_type}")

        existing_event = (
            db.query(MatterEvent)
            .filter(
                MatterEvent.matter_id == matter.id,
                MatterEvent.event_type == event_type,
                MatterEvent.event_date == parsed_date
            )
            .first()
        )
        if existing_event:
            # Event already saved — still need it for deadline computation
            trigger_events.append(TriggerEvent(trigger=event_type, date=parsed_date))
            if event_type == "complaint_filed_date" and existing_event.filing_status:
                filing_status_map["complaint_filed_date"] = existing_event.filing_status
            continue 

        db_event = MatterEvent(
            matter_id  = matter.id,
            event_type = event_type,
            event_date = parsed_date,
            notes      = notes,
            filing_status = filing_status
        )

        if event_type == "complaint_filed_date":
            filing_status_map["complaint_filed_date"] = filing_status

        db.add(db_event)
        db.commit()
        db.refresh(db_event)
        created_event_ids.append(db_event.id)

        trigger_events.append(
            TriggerEvent(
                trigger=event_type,
                date=parsed_date
            )
        )

    # COMPUTE DEADLINES — pass filing_status_map ← NEW
    computed_deadlines = compute_deadlines(
        jurisdiction=matter.jurisdiction,
        matter_id=str(matter.id),
        events=trigger_events,
        filing_status_map= filing_status_map,   # ← NEW
    )

    stored_deadlines = []
    
    event_type_to_db_id = {
        e.event_type: e.id
        for e in db.query(MatterEvent)
        .filter(MatterEvent.matter_id == matter.id)
        .all()
    }
    for item in computed_deadlines:

        # prevent duplicate insert
        existing = (
            db.query(ComputedDeadline)
            .filter(
                ComputedDeadline.matter_id == matter.id,
                ComputedDeadline.rule_id == item.rule_id,
                ComputedDeadline.due_date == item.deadline_date
            )
            .first()
        )

        if existing:
            continue

        deadline = ComputedDeadline(
            matter_id=matter.id,

            # optional: first event id
            # source_event_id=None,
            source_event_id=event_type_to_db_id.get(item.trigger),

            rule_id=item.rule_id,
            label=item.label,

            due_date=item.deadline_date,

            color=item.color,
            notes=item.notes,

            reminders_days=[
                d.isoformat()
                for d in item.reminder_dates
            ],

            computation_snapshot=item.to_dict(),
            
            # NEW: Deadlines are pending approval by default
            is_approved=False,
            approved_at=None,
            approved_by=None,
            filing_status=(
                    item.filing_status
                    or filing_status_map.get("complaint_filed_date")
                ),
        )

        db.add(deadline)
        db.flush()

        stored_deadlines.append({
            "id": deadline.id,
            "rule_id": item.rule_id,
            "label": item.label,
            "due_date": item.deadline_date.isoformat(),
            "color": item.color,
            "notes": item.notes,
            "is_approved": False,
            "filing_status": item.filing_status,   # ← NEW
        })

    db.commit()

    return {
        "success": True,
        "matter_id": matter.id,
        "events_created": created_event_ids,
        "computed_deadlines": stored_deadlines,
        "message": "Deadlines computed. Please review and approve them to sync to calendars."
    }


@router.get("/deadlines/pending")
async def get_pending_deadlines(
    matter_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Get all deadlines that are pending approval.
    Optionally filter by matter_id.
    """
    
    query = (
        db.query(ComputedDeadline)
        .filter(ComputedDeadline.is_approved == False)
        .order_by(ComputedDeadline.due_date.asc())
    )
    
    if matter_id:
        query = query.filter(ComputedDeadline.matter_id == matter_id)
    
    deadlines = query.all()
    
    result = []
    for d in deadlines:
        matter = db.query(Matter).filter(Matter.id == d.matter_id).first()
        result.append({
            "id": d.id,
            "matter_id": d.matter_id,
            "case_name": matter.case_name if matter else None,
            "jurisdiction": matter.jurisdiction if matter else None,
            "rule_id": d.rule_id,
            "label": d.label,
            "due_date": d.due_date.isoformat(),
            "color": d.color,
            "notes": d.notes,
            "created_at": d.created_at.isoformat() if d.created_at else None
        })
    
    return {
        "success": True,
        "pending_count": len(result),
        "deadlines": result
    }


@router.post("/deadlines/approve")
async def approve_deadlines(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Approve deadlines and sync them to attorney calendars.
    
    Payload:
    {
        "deadline_ids": [1, 2, 3],  // List of deadline IDs to approve
        "approved_by": "user@example.com",  // Who approved
        "attorney_emails": ["attorney1@firm.com", "attorney2@firm.com"]  // Target calendars
    }
    """
    
    deadline_ids = payload.get("deadline_ids", [])
    approved_by = current_user["email"]
    attorney_emails = payload.get("attorney_emails", [])
    
    if not deadline_ids:
        raise HTTPException(
            status_code=400,
            detail="deadline_ids array is required"
        )
    
    if not attorney_emails:
        raise HTTPException(
            status_code=400,
            detail="attorney_emails array is required"
        )
    
    # Enforce maximum of 3 attorney emails per approval request
    if len(attorney_emails) > 3:
        raise HTTPException(
            status_code=400,
            detail="Maximum 3 attorney emails are allowed per approval request"
        )
    
    # Fetch deadlines
    deadlines = (
        db.query(ComputedDeadline)
        .filter(ComputedDeadline.id.in_(deadline_ids))
        .all()
    )
    
    if not deadlines:
        raise HTTPException(
            status_code=404,
            detail="No deadlines found with provided IDs"
        )
    
    # Filter out already approved deadlines
    pending_deadlines = []
    skipped_deadlines = []
    
    for deadline in deadlines:
        if deadline.is_approved:
            print(f"Deadline {deadline.id} already approved, skipping")
            skipped_deadlines.append({
                "deadline_id": deadline.id,
                "reason": "already_approved"
            })
        else:
            pending_deadlines.append(deadline)
    
    if not pending_deadlines:
        return {
            "success": True,
            "message": "All deadlines were already approved",
            "skipped": skipped_deadlines,
            "approved_count": 0,
            "sync_results": {}
        }
    
    # Group by matter for efficient syncing
    matters_to_sync = {}
    for deadline in pending_deadlines:
        if deadline.matter_id not in matters_to_sync:
            matters_to_sync[deadline.matter_id] = []
        matters_to_sync[deadline.matter_id].append(deadline.id)
    
    # Sync to calendars
    sync_results = {}
    successfully_synced_deadline_ids = []
    
    for matter_id, deadline_id_list in matters_to_sync.items():
        try:
            result = await sync_matter_deadlines(
                db=db,
                matter_id=matter_id,
                deadline_ids=deadline_id_list,
                attorney_emails=attorney_emails
            )
            
            sync_results[f"matter_{matter_id}"] = result
            
            # Check which deadlines were successfully synced to ALL attorneys
            for deadline_id, deadline_result in result.get("results", {}).items():
                all_attorneys_success = all(
                    attorney_data.get("success", False)
                    for attorney_data in deadline_result.get("attorneys", {}).values()
                )
                
                if all_attorneys_success:
                    successfully_synced_deadline_ids.append(deadline_id)
            
        except Exception as e:
            logger.error(f"Failed to sync matter {matter_id}: {str(e)}")
            sync_results[f"matter_{matter_id}"] = {
                "error": str(e)
            }
    
    # Mark deadlines as approved ONLY if successfully synced to all attorneys
    approved_count = 0
    failed_approvals = []
    
    for deadline in pending_deadlines:
        if deadline.id in successfully_synced_deadline_ids:
            deadline.is_approved = True
            deadline.approved_at = datetime.utcnow()
            deadline.approved_by = approved_by
            db.add(deadline)
            approved_count += 1
        else:
            failed_approvals.append({
                "deadline_id": deadline.id,
                "rule_id": deadline.rule_id,
                "reason": "sync_failed_for_one_or_more_attorneys"
            })
    
    db.commit()
    
    return {
        "success": True,
        "approved_count": approved_count,
        "skipped": skipped_deadlines,
        "failed_approvals": failed_approvals,
        "sync_results": sync_results,
        "message": f"Approved {approved_count} deadlines. {len(failed_approvals)} failed to sync."
    }

@router.delete("/deadlines/{deadline_id}")
async def delete_deadline(
    deadline_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Delete a deadline. If it's already synced to calendar, remove it.
    """
    
    deadline = (
        db.query(ComputedDeadline)
        .filter(ComputedDeadline.id == deadline_id)
        .first()
    )
    
    if not deadline:
        raise HTTPException(
            status_code=404,
            detail="Deadline not found"
        )
    
    # If it has a graph_event_id, we need to delete from calendar
    if deadline.attorney_syncs:
        print("approved deadlines init....")
        # Get all attorneys who have this deadline
        links = db.query(DeadlineAttorney).filter(DeadlineAttorney.deadline_id == deadline_id, DeadlineAttorney.graph_event_id != None).all()
        
        for link in links:
            try:
                await delete_graph_event(link.attorney_email, link.graph_event_id)
            except Exception as e:
                logger.error(f"Failed to delete calendar event for {link.attorney_email}: {str(e)}")
    
    db.delete(deadline)
    db.commit()
    
    return {
        "success": True,
        "message": f"Deadline {deadline_id} deleted"
    }

class DeleteDeadlinesRequest(BaseModel):
    deadline_ids: List[int]

@router.delete("/deadlines")
async def delete_deadlines(
    payload: DeleteDeadlinesRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Delete multiple deadlines.
    If synced to Outlook, delete calendar events too.
    """

    deadlines = (
        db.query(ComputedDeadline)
        .filter(
            ComputedDeadline.id.in_(payload.deadline_ids)
        )
        .all()
    )

    if not deadlines:
        raise HTTPException(
            status_code=404,
            detail="No deadlines found"
        )

    deleted_count = 0
    failed_calendar_deletes = []

    for deadline in deadlines:
        # Delete Outlook events
        if deadline.attorney_syncs:
            links = (
                db.query(DeadlineAttorney)
                .filter(
                    DeadlineAttorney.deadline_id == deadline.id,
                    DeadlineAttorney.graph_event_id != None
                )
                .all()
            )

            for link in links:
                try:
                    await delete_graph_event(
                        link.attorney_email,
                        link.graph_event_id
                    )

                except Exception as e:
                    logger.error(
                        f"Failed to delete calendar event "
                        f"for {link.attorney_email}: {str(e)}"
                    )

                    failed_calendar_deletes.append({
                        "deadline_id": deadline.id,
                        "attorney_email": link.attorney_email,
                        "error": str(e)
                    })

        db.delete(deadline)
        deleted_count += 1

    db.commit()
    return {
        "success": True,
        "deleted_count": deleted_count,
        "failed_calendar_deletes": failed_calendar_deletes,
        "message": f"Deleted {deleted_count} deadlines"
    }

@router.get("/matters/{matter_id}/deadlines")
async def get_matter_deadlines(
    matter_id: int,
    include_pending: bool = False,
    include_approved: bool = False,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    matter = (
        db.query(Matter)
        .filter(Matter.id == matter_id)
        .first()
    )

    if not matter:
        raise HTTPException(
            status_code=404,
            detail="Matter not found"
        )

    query = (
        db.query(ComputedDeadline)
        .filter(ComputedDeadline.matter_id == matter_id)
    )
    
    # Filter by approval status
    if include_pending and not include_approved:
        query = query.filter(ComputedDeadline.is_approved == False)
    elif include_approved and not include_pending:
        query = query.filter(ComputedDeadline.is_approved == True)
    
    deadlines = query.order_by(ComputedDeadline.due_date.asc()).all()

    response = []

    for d in deadlines:

        response.append({
            "id": d.id,
            "rule_id": d.rule_id,
            "label": d.label,
            "due_date": d.due_date.isoformat(),
            "color": d.color,
            "notes": d.notes,
            "is_approved": d.is_approved,
            "approved_at": d.approved_at.isoformat() if d.approved_at else None,
            "approved_by": d.approved_by,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "filing_status": d.filing_status,
        })

    return {
        "success": True,
        "matter_id": matter.id,
        "case_name": matter.case_name,
        "deadlines": response
    }

@router.get("/matters/{matter_id}/events")
async def get_matter_events(
    matter_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    events = (
        db.query(MatterEvent)
        .filter(MatterEvent.matter_id == matter_id)
        .order_by(MatterEvent.event_date.asc())
        .all()
    )

    return {
        "success": True,
        "events": [
            {
                "id": e.id,
                "event_type": e.event_type,
                "event_date": e.event_date.isoformat(),
                "notes": e.notes,
                "filing_status":  e.filing_status
            }
            for e in events
        ]
    }


@router.patch("/events/{event_id}")
async def patch_event(
    event_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    event = (
        db.query(MatterEvent)
        .filter(MatterEvent.id == event_id)
        .first()
    )

    if not event:
        raise HTTPException(
            status_code=404,
            detail="Event not found"
        )

    matter = (
        db.query(Matter)
        .filter(Matter.id == event.matter_id)
        .first()
    )

    # update events
    new_event_type = payload.get(
        "event_type",
        event.event_type
    )

    new_event_date = payload.get("event_date")
    new_filing_status = payload.get("filing_status")
    if new_event_date:

        try:
            parsed_date = datetime.strptime(
                new_event_date,
                "%Y-%m-%d"
            ).date()

        except Exception:
            raise HTTPException(
                status_code=400,
                detail="event_date must be YYYY-MM-DD"
            )

        event.event_date = parsed_date

    event.event_type = new_event_type
    if "notes" in payload:
        event.notes = payload["notes"]

    filing_status_map = {}
    if event.event_type == "complaint_filed_date":

        if new_filing_status:
            event.filing_status = new_filing_status

        filing_status_map["complaint_filed_date"] = (
            event.filing_status or "Planned"
        )

    db.commit()
    db.refresh(event)

    existing_deadlines = (
        db.query(ComputedDeadline)
        .filter(ComputedDeadline.matter_id == matter.id)
        .all()
    )

    # delete calender events
    for deadline in existing_deadlines:

        if deadline.attorney_syncs:

            links = (
                db.query(DeadlineAttorney)
                .filter(
                    DeadlineAttorney.deadline_id == deadline.id,
                    DeadlineAttorney.graph_event_id != None
                )
                .all()
            )

            for link in links:

                try:
                    await delete_graph_event(
                        link.attorney_email,
                        link.graph_event_id
                    )

                except Exception as e:

                    logger.error(
                        f"Failed deleting graph event "
                        f"{link.graph_event_id}: {str(e)}"
                    )

    # delete all attorney links
    (
        db.query(DeadlineAttorney)
        .filter(
            DeadlineAttorney.deadline_id.in_(
                [d.id for d in existing_deadlines]
            )
        )
        .delete(synchronize_session=False)
    )

    db.commit()

    # delete all deadlines
    (
        db.query(ComputedDeadline)
        .filter(
            ComputedDeadline.matter_id == matter.id
        )
        .delete(synchronize_session=False)
    )

    db.commit()

    # load all events again
    all_events = (
        db.query(MatterEvent)
        .filter(MatterEvent.matter_id == matter.id)
        .all()
    )

    for e in all_events:
        if e.event_type == "complaint_filed_date" and e.filing_status:
            filing_status_map["complaint_filed_date"] = e.filing_status

    trigger_events = [TriggerEvent(trigger=e.event_type, date=e.event_date) for e in all_events]

    computed_deadlines = compute_deadlines(
        jurisdiction      = matter.jurisdiction,
        matter_id         = str(matter.id),
        events            = trigger_events,
        filing_status_map = filing_status_map,   # ← NEW
    )

    event_lookup = {e.event_type: e.id for e in all_events}
    stored_deadlines = []
    for item in computed_deadlines:

        source_event_id = event_lookup.get(
            item.trigger
        )
        deadline = ComputedDeadline(
            matter_id=matter.id,
            source_event_id=source_event_id,
            rule_id=item.rule_id,
            label=item.label,
            due_date=item.deadline_date,
            color=item.color,
            notes=item.notes,
            reminders_days=[
                d.isoformat()
                for d in item.reminder_dates
                ],
            computation_snapshot=item.to_dict(),
            is_approved=False,
            approved_at=None,
            approved_by=None,
            filing_status=item.filing_status,
        )

        db.add(deadline)

        stored_deadlines.append({
            "rule_id": item.rule_id,
            "label": item.label,
            "due_date": item.deadline_date.isoformat(),
            "color": item.color,
            "notes": item.notes,
            "filing_status":item.filing_status,   # ← NEW
        })

    db.commit()

    return {
        "success": True,
        "event_id": event.id,
        "updated_event_type": event.event_type,
        "updated_event_date": event.event_date.isoformat(),
        "filing_status":        event.filing_status,    # ← NEW
        "recomputed_deadlines": stored_deadlines,
        "message": "All matter deadlines were recomputed. Please approve again to sync calendars."
    }

@router.get("/deadlines/{deadline_id}/calendar-status")
async def check_calendar_sync_status(
    deadline_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Verify whether a deadline's calendar event(s) actually exist in Microsoft Graph.
    Checks every attorney that was synced for this deadline.

    Returns per-attorney status: exists / missing / never_synced.
    """
    deadline = (
        db.query(ComputedDeadline)
        .filter(ComputedDeadline.id == deadline_id)
        .first()
    )

    if not deadline:
        raise HTTPException(status_code=404, detail="Deadline not found")

    if not deadline.is_approved:
        return {
            "success": True,
            "deadline_id": deadline_id,
            "status": "not_approved",
            "message": "Deadline has not been approved/synced yet.",
            "attorney_statuses": []
        }

    links = (
        db.query(DeadlineAttorney)
        .filter(DeadlineAttorney.deadline_id == deadline_id)
        .all()
    )

    if not links:
        return {
            "success": True,
            "deadline_id": deadline_id,
            "status": "never_synced",
            "message": "No attorney calendar links found for this deadline.",
            "attorney_statuses": []
        }

    attorney_statuses = []
    all_ok = True

    for link in links:
        if not link.graph_event_id:
            attorney_statuses.append({
                "attorney_email": link.attorney_email,
                "status": "missing_event_id",
                "graph_event_id": None,
            })
            all_ok = False
            continue

        result = await verify_calendar_event(link.attorney_email, link.graph_event_id)
        if result.get("exists"):
            attorney_statuses.append({
                "attorney_email": link.attorney_email,
                "status": "ok",
                "graph_event_id": link.graph_event_id,
                "subject": result.get("subject"),
                "start": result.get("start"),
            })
        else:
            attorney_statuses.append({
                "attorney_email": link.attorney_email,
                "status": "missing_in_graph",
                "graph_event_id": link.graph_event_id,
                "error": result.get("error"),
            })
            all_ok = False

    return {
        "success": True,
        "deadline_id": deadline_id,
        "label": deadline.label,
        "due_date": deadline.due_date.isoformat(),
        "overall_status": "ok" if all_ok else "degraded",
        "attorney_statuses": attorney_statuses,
    }

@router.get("/jurisdictions")
async def list_jurisdictions(
    sort: Optional[str] = Query(default=None), current_user: dict = Depends(get_current_user)):
    """
    List all jurisdictions and the event_types.
    Pass ?sort=CO_STATE,AZ_FEDERAL,... to sort the jurisdictions in the response.
    """

    valid_keys = set(JURISDICTION_FILES.keys())   # CO_STATE, CO_FEDERAL, …
 
    if sort:
        requested = [k.strip().upper() for k in sort.split(",") if k.strip()]
        unknown = [k for k in requested if k not in valid_keys]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown jurisdiction(s): {unknown}. "
                    f"Valid values: {sorted(valid_keys)}"
                ),
            )
        ordered_keys = requested
    else:
        ordered_keys = sorted(valid_keys)
 
    # Load YAML rules for all jurisdictions
    all_rules = load_all_rules()
    jurisdictions = []
 
    for key in ordered_keys:
        jur = all_rules[key]
 
        # Deduplicate triggers while preserving first-occurrence order.
        seen = dict.fromkeys(rule.trigger for rule in jur.rules)
        event_types = list(seen.keys())
        jurisdictions.append(
            {
                "jurisdiction": jur.jurisdiction,
                "name": jur.name,
                "event_types": event_types,
            }
        )
    return {"jurisdictions": jurisdictions}

@router.get("/dashboard")
async def dashboard_table(
    matter_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    query = (
        db.query(
            ComputedDeadline,
            Matter
        )
        .join(
            Matter,
            Matter.id == ComputedDeadline.matter_id
        )
    )

    if matter_id:
        query = query.filter(
            ComputedDeadline.matter_id == matter_id
        )

    deadlines = (
        query
        .order_by(
            Matter.created_at.desc(),
            ComputedDeadline.due_date.asc()
        )
        .all()
    )

    today = date.today()

    grouped_rows = defaultdict(list)

    matter_info = {}

    for deadline, matter in deadlines:

        days_left = (
            deadline.due_date - today
        ).days

        matter_info[matter.id] = {
            "matter_id": matter.id,
            "case_name": matter.case_name,
            "jurisdiction": matter.jurisdiction
        }

        grouped_rows[matter.id].append({

            "deadline_id": deadline.id,
            "deadline_type": deadline.label,
            "category": deadline.color,
            "due_date": deadline.due_date.isoformat(),
            "days_left": days_left,
            "rule_id": deadline.rule_id,
            "notes": deadline.notes,
            "is_approved": deadline.is_approved,
            "approved_by": deadline.approved_by,
            "approved_at": (
                deadline.approved_at.isoformat()
                if deadline.approved_at
                else None
            ),
            "filing_status": deadline.filing_status
        })

    response = []

    for matter_id, deadlines_list in grouped_rows.items():

        response.append({
            **matter_info[matter_id],
            "deadlines": deadlines_list
        })

    return {
        "success": True,
        "total_cases": len(response),
        "total_deadlines": len(deadlines),
        "rows": response
    }

@router.get("/analytics/dashboard")
async def get_dashboard_analytics(
    range_type: Literal["today", "weekly", "monthly", "yearly"] = Query("weekly"),
    start_date: datetime = Query(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    start_dt, end_dt = await get_date_range(
        range_type,
        start_date
    )

    today = date.today()
    base_filters = [
        ComputedDeadline.created_at >= start_dt,
        ComputedDeadline.created_at <= end_dt
    ]

    total_deadlines = (
        db.query(ComputedDeadline)
        .filter(*base_filters)
        .count()
    )

    total_matters = (
        db.query(ComputedDeadline.matter_id)
        .filter(*base_filters)
        .distinct()
        .count()
    )

    pending_approvals = (
        db.query(ComputedDeadline)
        .filter(
            *base_filters,
            ComputedDeadline.is_approved == False
        )
        .count()
    )

    upcoming_deadlines = (
        db.query(ComputedDeadline)
        .filter(
            *base_filters,
            ComputedDeadline.is_approved == True,
            ComputedDeadline.due_date >= today
        )
        .count()
    )

    overdue_deadlines = (
        db.query(ComputedDeadline)
        .filter(
            *base_filters,
            ComputedDeadline.is_approved == True,
            ComputedDeadline.due_date < today
        )
        .count()
    )

    risk_distribution_raw = (
        db.query(func.lower(func.coalesce(ComputedDeadline.color,"unknown")).label("color"),
            func.count(ComputedDeadline.id).label("count")).filter(
            *base_filters
        ).group_by(func.lower(
            func.coalesce(ComputedDeadline.color,"unknown"))).all())

    risk_distribution = {
        color or "unknown": count
        for color, count in risk_distribution_raw
    }

    deadlines = (
        db.query(ComputedDeadline)
        .filter(
            *base_filters
        )
        .order_by(
            ComputedDeadline.created_at.asc()
        )
        .all()
    )

    workload_clusters = {}

    for deadline in deadlines:
        created_date = deadline.created_at.date()
        if range_type == "weekly":
            cluster_key = created_date.isoformat()

        elif range_type == "monthly":
            week_start = (
                created_date -
                timedelta(days=created_date.weekday())
            )
            cluster_key = week_start.isoformat()

        else:
            cluster_key = (
                f"{created_date.year}-"
                f"{created_date.month:02d}"
            )

        if cluster_key not in workload_clusters:
            workload_clusters[cluster_key] = {
                "period": cluster_key,
                "count": 0,
                "deadlines": []
            }

        workload_clusters[cluster_key]["count"] += 1
        workload_clusters[cluster_key]["deadlines"].append({
            "id": deadline.id,
            "label": deadline.label,
            "due_date": deadline.due_date.isoformat(),
            "created_at": deadline.created_at.isoformat(),
            "color": deadline.color,
            "matter_id": deadline.matter_id,
            "is_approved": deadline.is_approved
        })

    workload_data = sorted(
        workload_clusters.values(),
        key=lambda x: x["period"]
    )

    peak_period = (
        max(
            workload_data,
            key=lambda x: x["count"]
        )
        if workload_data else None
    )

    matters_with_counts = (
        db.query(
            Matter.id,
            Matter.case_name,
            Matter.jurisdiction,

            func.count(
                ComputedDeadline.id
            ).label("total_deadlines"),

           func.sum(
                case(
                    (
                        and_(
                            ComputedDeadline.is_approved == True,
                            ComputedDeadline.due_date >= today
                        ),
                        1
                    ),
                    else_=0
                )
            ).label("upcoming_deadlines"),

           func.sum(
                case(
                    (
                        and_(
                            ComputedDeadline.is_approved == True,
                            ComputedDeadline.due_date < today
                        ),
                        1
                    ),
                    else_=0
                )
            ).label("overdue_deadlines")
        )
        .outerjoin(
            ComputedDeadline,
            and_(
                ComputedDeadline.matter_id == Matter.id,
                ComputedDeadline.created_at >= start_dt,
                ComputedDeadline.created_at <= end_dt
            )
        )
        .group_by(
            Matter.id,
            Matter.case_name,
            Matter.jurisdiction
        )
        .all()
    )

    matters_data = []

    for (
        matter_id,
        case_name,
        jurisdiction,
        total,
        upcoming,
        overdue
    ) in matters_with_counts:

        matters_data.append({
            "matter_id": matter_id,
            "case_name": case_name,
            "jurisdiction": jurisdiction,
            "total_deadlines": total or 0,
            "upcoming_deadlines": upcoming or 0,
            "overdue_deadlines": overdue or 0
        })

    matters_data.sort(
        key=lambda x: x["total_deadlines"],
        reverse=True
    )

    jurisdiction_data = (
        db.query(
            Matter.jurisdiction,

            func.count(
                Matter.id.distinct()
            ).label("matter_count"),

            func.count(
                ComputedDeadline.id
            ).label("deadline_count")
        )
        .outerjoin(
            ComputedDeadline,
            and_(
                ComputedDeadline.matter_id == Matter.id,
                ComputedDeadline.created_at >= start_dt,
                ComputedDeadline.created_at <= end_dt
            )
        )
        .group_by(Matter.jurisdiction)
        .all()
    )

    jurisdictions = []

    for (
        jurisdiction,
        matter_count,
        deadline_count
    ) in jurisdiction_data:

        jurisdictions.append({
            "jurisdiction": jurisdiction,
            "matter_count": matter_count,
            "deadline_count": deadline_count or 0
        })

    return {
        "success": True,

        "filters": {
            "range_type": range_type,
            "start_date": start_dt.isoformat(),
            "end_date": end_dt.isoformat()
        },

        "overview": {
            "total_matters": total_matters,
            "total_deadlines": total_deadlines,
            "pending_approvals": pending_approvals,
            "upcoming_deadlines": upcoming_deadlines,
            "overdue_deadlines": overdue_deadlines
        },

        "risk_distribution": risk_distribution,

        "workload": {
            "total_deadlines": len(deadlines),
            "clusters": workload_data,
            "peak_period": peak_period
        },

        "matters": matters_data,

        "jurisdictions": jurisdictions
    }
@router.get("/instrument_panel")
async def instrument_panel(
    range_type: Literal["today", "weekly", "monthly", "yearly"] = Query("weekly"),
    start_date: datetime = Query(...),
    matter_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    start_dt, end_dt = await get_date_range(
        range_type,
        start_date
    )

    today = date.today()
    next_7_days = today + timedelta(days=7)
    next_3_days = today + timedelta(days=3)

    base_filters = [
        ComputedDeadline.created_at >= start_dt,
        ComputedDeadline.created_at <= end_dt
    ]

    if matter_id:

        base_filters.append(
            ComputedDeadline.matter_id == matter_id
        )

    total_deadlines = (
        db.query(ComputedDeadline)
        .filter(*base_filters)
        .count()
    )

    approved = (
        db.query(ComputedDeadline)
        .filter(
            *base_filters,
            ComputedDeadline.is_approved == True
        )
        .count()
    )

    pending_review = (
        db.query(ComputedDeadline)
        .filter(
            *base_filters,
            ComputedDeadline.is_approved == False
        )
        .count()
    )

    upcoming_7d = (
        db.query(ComputedDeadline)
        .filter(
            *base_filters,
            ComputedDeadline.is_approved == True,
            ComputedDeadline.due_date >= today,
            ComputedDeadline.due_date <= next_7_days
        )
        .count()
    )

    high_risk = (
        db.query(ComputedDeadline)
        .filter(
            *base_filters,
            ComputedDeadline.is_approved == True,
            or_(

                ComputedDeadline.color.ilike("red"),

                ComputedDeadline.due_date <= next_3_days,

                ComputedDeadline.due_date < today
            )
        )
        .count()
    )

    return {
        "success": True,

        "filters": {
            "range_type": range_type,
            "start_date": start_dt.isoformat(),
            "end_date": end_dt.isoformat(),
            "matter_id": matter_id
        },

        "instrument_panel": {

            "total_deadlines": {
                "count": total_deadlines,
                "description": "Deadlines created during selected period"
            },

            "approved": {
                "count": approved,
                "description": "Approved for calendar sync"
            },

            "pending_review": {
                "count": pending_review,
                "description": "Awaiting approval"
            },

            "upcoming_7d": {
                "count": upcoming_7d,
                "description": "Upcoming within next 7 days"
            },

            "high_risk": {
                "count": high_risk,
                "description": "Critical or overdue deadlines"
            }
        }
    }

@router.get("/outlook/sync-status")
async def outlook_sync_status(
    matter_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Case-wise Outlook sync status.
    """

    matter_query = db.query(Matter)

    if matter_id:
        matter_query = matter_query.filter(Matter.id == matter_id)

    matters = matter_query.all()

    response = []
    attorney_email = current_user["email"]
    graph_connection = await verify_graph_connection(attorney_email)

    for matter in matters:

        approved_deadlines = (
            db.query(ComputedDeadline)
            .filter(
                ComputedDeadline.matter_id == matter.id,
                ComputedDeadline.is_approved == True
            )
            .all()
        )

        total = len(approved_deadlines)

        fully_synced = 0
        partial_synced = 0
        not_synced = 0

        attorney_summary = {}

        for deadline in approved_deadlines:

            links = (
                db.query(DeadlineAttorney)
                .filter(DeadlineAttorney.deadline_id == deadline.id)
                .all()
            )

            if not links:
                not_synced += 1
                continue

            synced_count = 0

            for link in links:

                is_synced = bool(link.graph_event_id)

                # ALWAYS verify real Outlook event
                if is_synced and link.graph_event_id:
                    result = await verify_calendar_event(
                        link.attorney_email,
                        link.graph_event_id
                    )
                    is_synced = result.get("exists", False)

                if link.attorney_email not in attorney_summary:
                    attorney_summary[link.attorney_email] = {
                        "synced": 0,
                        "missing": 0
                    }

                if is_synced:
                    synced_count += 1
                    attorney_summary[link.attorney_email]["synced"] += 1
                else:
                    attorney_summary[link.attorney_email]["missing"] += 1

            if synced_count == 0:
                not_synced += 1
            elif synced_count == len(links):
                fully_synced += 1
            else:
                partial_synced += 1

        if total == 0:
            status = "no_approved_deadlines"
        elif fully_synced == total:
            status = "fully_synced"
        elif not_synced == total:
            status = "not_synced"
        else:
            status = "partial_sync"

        response.append({
            "matter_id": matter.id,
            "case_name": matter.case_name,
            "jurisdiction": matter.jurisdiction,

            "outlook_sync_status": status,

            "summary": {
                "total_approved_deadlines": total,
                "fully_synced": fully_synced,
                "partial_synced": partial_synced,
                "not_synced": not_synced
            },

            "attorney_sync_summary": [
                {
                    "attorney_email": email,
                    "synced": data["synced"],
                    "missing": data["missing"]
                }
                for email, data in attorney_summary.items()
            ]
        })

    return {
        "success": True,
        "graph_connection": graph_connection,
        "matters": response
    }

# Column order shared by CSV and Excel writers
EXPORT_COLUMNS = [
    # Matter context
    "matter_id",
    "case_name",
    "jurisdiction",
    # Trigger events (semicolon-separated list in one cell)
    "trigger_events",
    # Deadline fields
    "deadline_id",
    "rule_id",
    "label",
    "due_date",
    "days_until_due",
    "color",
    "is_approved",
    "approved_by",
    "approved_at",
    "reminder_dates",
    "notes",
    "created_at",
]
 
def _build_export_query(db: Session, matter_id: Optional[List[int]]):
    """
    Return a SQLAlchemy query that joins ComputedDeadline → Matter, optionally
    Include one or more ?matter_id=<id> values to filter the export..  Ordered matter_id ASC, due_date ASC so the
    CSV/Excel is naturally grouped by matter and sorted chronologically.
    """
    q = (
        db.query(ComputedDeadline, Matter)
        .join(Matter, Matter.id == ComputedDeadline.matter_id)
        .order_by(Matter.id.asc(), ComputedDeadline.due_date.asc())
    )
    if matter_id:
        q = q.filter(ComputedDeadline.matter_id.in_(matter_id))
    return q
 
 
def _load_events_map(db: Session, matter_id: Optional[List[int]]) -> dict:
    """
    Return {matter_id: "event_type=YYYY-MM-DD; ..."} for the scope of the export.
    Pre-loads all relevant events in a single query so the row loop stays O(1).
    """
    q = db.query(MatterEvent).order_by(
        MatterEvent.matter_id.asc(), MatterEvent.event_date.asc()
    )
    if matter_id:
        q = q.filter(MatterEvent.matter_id.in_(matter_id))
 
    events_map: dict[int, list[str]] = {}
    for ev in q.all():
        events_map.setdefault(ev.matter_id, []).append(
            f"{ev.event_type}={ev.event_date.isoformat()}"
        )
    return {mid: "; ".join(parts) for mid, parts in events_map.items()}
 
 
def _row_dict(
    deadline: "ComputedDeadline",
    matter: "Matter",
    events_map: dict,
    today: date,
) -> dict:
    """Build one flat export row from a deadline + matter pair."""
    due = deadline.due_date
    days_until = (due - today).days if due else None
 
    reminders = ""
    if deadline.reminders_days:
        # reminders_days is stored as a JSON list of ISO date strings
        if isinstance(deadline.reminders_days, list):
            reminders = "; ".join(str(r) for r in deadline.reminders_days)
        else:
            reminders = str(deadline.reminders_days)
 
    return {
        "matter_id":       matter.id,
        "case_name":       matter.case_name,
        "jurisdiction":    matter.jurisdiction,
        "trigger_events":  events_map.get(matter.id, ""),
        "deadline_id":     deadline.id,
        "rule_id":         deadline.rule_id,
        "label":           deadline.label,
        "due_date":        due.isoformat() if due else "",
        "days_until_due":  days_until if days_until is not None else "",
        "color":           deadline.color or "",
        "is_approved":     "Yes" if deadline.is_approved else "No",
        "approved_by":     deadline.approved_by or "",
        "approved_at":     deadline.approved_at.isoformat() if deadline.approved_at else "",
        "reminder_dates":  reminders,
        "notes":           deadline.notes or "",
        "created_at":      deadline.created_at.isoformat() if deadline.created_at else "",
    }
 
 
# ── CSV streaming export ──────────────────────────────────────────────────────
 
def _stream_csv(db: Session, matter_id: Optional[List[int]]):
    """
    Generator that yields UTF-8 CSV chunks directly from the DB cursor.
    No temp file is created — the response body is built on the fly.
    """
    today = date.today()
    events_map = _load_events_map(db, matter_id)
    query = _build_export_query(db, matter_id)
 
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=EXPORT_COLUMNS,
        extrasaction="ignore",
        lineterminator="\r\n",
    )
 
    # Header row
    writer.writeheader()
    yield buf.getvalue()
    buf.seek(0)
    buf.truncate(0)
 
    # Data rows — streamed one at a time to keep RAM constant
    for deadline, matter in query.yield_per(200):
        writer.writerow(_row_dict(deadline, matter, events_map, today))
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
 
 
@router.get("/export/deadlines/csv")
async def export_deadlines_csv(
    matter_id:  Optional[List[int]] = Query(default=None, description="Filter to one or more matter"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Stream all computed deadlines as CSV.
 
    - No temporary file is created; rows are yielded directly from the DB.
    - Include `?matter_id=<id>` to export a single matter.
    - Trigger events for each matter are included as a semicolon-separated
      column so the export is self-contained.
 
    The browser (or API client) receives the file as an attachment:
        Content-Disposition: attachment; filename="deadlines_<scope>.csv"
    """
    scope = f"matters_{'_'.join(map(str, matter_id))}" if matter_id else "all_matters"
    filename = f"deadlines_{scope}.csv"
 
    # Validate matter exists when filtering
    if matter_id:
        matters = db.query(Matter.id).filter(Matter.id.in_(matter_id)).all()

        found_ids = {m.id for m in matters}
        missing_ids = [mid for mid in matter_id if mid not in found_ids]

        if missing_ids:
            raise HTTPException(
                status_code=404,
                detail=f"Matters not found: {missing_ids}"
            )
 
    return StreamingResponse(
        _stream_csv(db, matter_id),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Export-Scope": scope,
        },
    )
 
 
# ── Excel streaming export ────────────────────────────────────────────────────
def _build_excel_bytes(db: Session, matter_id: Optional[List[int]]) -> bytes:    
    """
    Build an Excel workbook in memory using openpyxl and return raw bytes.
 
    Two sheets are written:
      1. "Deadlines"  — one row per deadline (all matters or one matter)
      2. "Events"     — one row per trigger event so the source data is
                        preserved in its original granular form
 
    openpyxl writes to a BytesIO buffer — no file system access needed.
    """
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="openpyxl is required for Excel export. Install it with: pip install openpyxl",
        )
 
    today = date.today()
    events_map = _load_events_map(db, matter_id)
    query = _build_export_query(db, matter_id)
 
    wb = openpyxl.Workbook()
 
    # ── Sheet 1: Deadlines ────────────────────────────────────────────────────
    ws_dl = wb.active
    ws_dl.title = "Deadlines"
 
    HEADER_FILL = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
    HEADER_FONT = Font(color="FFFFFF", bold=True)
    COLOR_MAP = {
        "red":    "FFB3B3",
        "orange": "FFD9B3",
        "yellow": "FFFAB3",
        "blue":   "B3D9FF",
        "green":  "B3FFB3",
    }
 
    # Header
    for col_idx, col_name in enumerate(EXPORT_COLUMNS, start=1):
        cell = ws_dl.cell(row=1, column=col_idx, value=col_name.replace("_", " ").title())
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center")
 
    # Data rows
    row_num = 2
    for deadline, matter in query.yield_per(200):
        row = _row_dict(deadline, matter, events_map, today)
        fill_color = COLOR_MAP.get((deadline.color or "").lower())
 
        for col_idx, col_name in enumerate(EXPORT_COLUMNS, start=1):
            cell = ws_dl.cell(row=row_num, column=col_idx, value=row[col_name])
            if fill_color:
                cell.fill = PatternFill(
                    start_color=fill_color,
                    end_color=fill_color,
                    fill_type="solid",
                )
        row_num += 1
 
    # Auto-size columns (cap at 60 chars wide)
    for col_idx in range(1, len(EXPORT_COLUMNS) + 1):
        col_letter = get_column_letter(col_idx)
        max_len = max(
            (len(str(ws_dl.cell(row=r, column=col_idx).value or "")) for r in range(1, row_num)),
            default=10,
        )
        ws_dl.column_dimensions[col_letter].width = min(max_len + 4, 60)
 
    # Freeze header row
    ws_dl.freeze_panes = "A2"
 
    # ── Sheet 2: Events ───────────────────────────────────────────────────────
    ws_ev = wb.create_sheet("Events")
    EVENT_COLS = ["matter_id", "case_name", "jurisdiction", "event_type", "event_date", "notes"]
 
    for col_idx, col_name in enumerate(EVENT_COLS, start=1):
        cell = ws_ev.cell(row=1, column=col_idx, value=col_name.replace("_", " ").title())
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center")
 
    ev_query = (
        db.query(MatterEvent, Matter)
        .join(Matter, Matter.id == MatterEvent.matter_id)
        .order_by(Matter.id.asc(), MatterEvent.event_date.asc())
    )
    if matter_id:
        ev_query = ev_query.filter(MatterEvent.matter_id.in_(matter_id))
 
    ev_row = 2
    for ev, matter in ev_query.yield_per(200):
        ws_ev.cell(row=ev_row, column=1, value=matter.id)
        ws_ev.cell(row=ev_row, column=2, value=matter.case_name)
        ws_ev.cell(row=ev_row, column=3, value=matter.jurisdiction)
        ws_ev.cell(row=ev_row, column=4, value=ev.event_type)
        ws_ev.cell(row=ev_row, column=5, value=ev.event_date.isoformat() if ev.event_date else "")
        ws_ev.cell(row=ev_row, column=6, value=ev.notes or "")
        ev_row += 1
 
    for col_idx in range(1, len(EVENT_COLS) + 1):
        col_letter = get_column_letter(col_idx)
        max_len = max(
            (len(str(ws_ev.cell(row=r, column=col_idx).value or "")) for r in range(1, ev_row)),
            default=10,
        )
        ws_ev.column_dimensions[col_letter].width = min(max_len + 4, 60)
 
    ws_ev.freeze_panes = "A2"
 
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()
 
 
@router.get("/export/deadlines/excel")
async def export_deadlines_excel(
    matter_id:  Optional[List[int]] = Query(default=None, description="Filter to a one or more matter"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Export all computed deadlines as an Excel (.xlsx) workbook.
 
    Two sheets are included:
    - **Deadlines** — one row per deadline, colour-coded by risk level,
      with trigger events summarised in a column.
    - **Events**    — one row per trigger event in its original granular form.
 
    Include `?matter_id=<id>` to export a single matter.
 
    The workbook is built in memory (no temp file) and streamed directly
    to the client as an attachment.
    """
    scope = f"matters_{'_'.join(map(str, matter_id))}" if matter_id else "all_matters"
    filename = f"deadlines_{scope}.xlsx"
 
    if matter_id:
        matters = db.query(Matter.id).filter(Matter.id.in_(matter_id)).all()

        found_ids = {m.id for m in matters}
        missing_ids = [mid for mid in matter_id if mid not in found_ids]

        if missing_ids:
            raise HTTPException(
                status_code=404,
                detail=f"Matters not found: {missing_ids}"
            )
 
    excel_bytes = _build_excel_bytes(db, matter_id)
 
    return StreamingResponse(
        iter([excel_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(excel_bytes)),
            "X-Export-Scope": scope,
        },
    )
 
 
# ── JSON export (bonus — useful for API consumers / debugging) ────────────────
 
@router.get("/export/deadlines")
async def export_deadlines_json(
    matter_id:  Optional[List[int]] = Query(default=None, description="Filter to one or more matter"),
    include_events: bool = Query(default=True, description="Include trigger events per matter"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Export all computed deadlines as JSON.
    """
    today = date.today()
 
    if matter_id:
        matters = db.query(Matter.id).filter(Matter.id.in_(matter_id)).all()

        found_ids = {m.id for m in matters}
        missing_ids = [mid for mid in matter_id if mid not in found_ids]

        if missing_ids:
            raise HTTPException(
                status_code=404,
                detail=f"Matters not found: {missing_ids}"
            )
 
    # Load events map if requested
    events_by_matter: dict[int, list] = {}
    if include_events:
        ev_q = db.query(MatterEvent).order_by(
            MatterEvent.matter_id.asc(), MatterEvent.event_date.asc()
        )
        if matter_id:
            ev_q = ev_q.filter(MatterEvent.matter_id.in_(matter_id))
        for ev in ev_q.all():
            events_by_matter.setdefault(ev.matter_id, []).append({
                "event_id":   ev.id,
                "event_type": ev.event_type,
                "event_date": ev.event_date.isoformat(),
                "notes":      ev.notes or "",
            })
 
    # Load deadlines
    query = _build_export_query(db, matter_id)
 
    matters_out: dict[int, dict] = {}
    total_deadlines = 0
 
    for deadline, matter in query.all():
        mid = matter.id
        if mid not in matters_out:
            matters_out[mid] = {
                "matter_id":       mid,
                "case_name":       matter.case_name,
                "jurisdiction":    matter.jurisdiction,
                "events":          events_by_matter.get(mid, []) if include_events else [],
                "deadlines":       [],
            }
 
        due = deadline.due_date
        matters_out[mid]["deadlines"].append({
            "deadline_id":   deadline.id,
            "rule_id":       deadline.rule_id,
            "label":         deadline.label,
            "due_date":      due.isoformat() if due else None,
            "days_until_due": (due - today).days if due else None,
            "color":         deadline.color,
            "is_approved":   deadline.is_approved,
            "approved_by":   deadline.approved_by,
            "approved_at":   deadline.approved_at.isoformat() if deadline.approved_at else None,
            "reminder_dates": deadline.reminders_days or [],
            "notes":         deadline.notes or "",
            "created_at":    deadline.created_at.isoformat() if deadline.created_at else None,
        })
        total_deadlines += 1
 
    matters_list = list(matters_out.values())
 
    return {
        "export_scope":     f"matters_{'_'.join(map(str, matter_id))}" if matter_id else "all_matters",
        "exported_at":      datetime.utcnow().isoformat() + "Z",
        "total_matters":    len(matters_list),
        "total_deadlines":  total_deadlines,
        "matters":          matters_list,
    }