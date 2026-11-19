# main.py
from fastapi import APIRouter, Form, Request, Depends, HTTPException, UploadFile, File
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import StreamingResponse
from datetime import datetime, timedelta
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from openai import OpenAI
import os
from sqlalchemy import func, desc
import time
from dotenv import load_dotenv
from db import get_db, log_prompt_and_update_activity, Template , Session, ActiveSession, ApiErrorLog ,PromptLog, LedgerRun, LedgerEntry, LedgerEditHistory, LedgerSummaryRun, LedgerSummaryEntry, Whitelist
from admin.router import admin_router
from typing import Optional
from .utils.ledger_pipeline import generate_ledger
import pandas as pd
import io
from collections import defaultdict
from .utils.doc_entries_generator import generate_full_ledger, extract_context, attorney_summary_metrices
import uuid
from auth_utils import authenticate_user, create_access_token, get_current_user, is_authorized, ACCESS_TOKEN_EXPIRE_MINUTES, reset_password, generate_otp, verify_otp, SessionLocal, create_password
load_dotenv()

router = APIRouter()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
assistant_id = os.getenv("VICP_assistant_id")

PROJECT_NAME = "VICP_Attorney_Fees"

jinja_templates = Jinja2Templates(directory="VICP_Attorney_Fees/templates")

@router.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return jinja_templates.get_template("index.html").render({
        "request": request
    })

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

@router.post("/api/chat", response_class=JSONResponse)
async def chat(request: Request, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    data = await request.json()
    message = data.get("message", "").strip()
    thread_id = data.get("thread_id")
    
    if not message:
        return JSONResponse({"error": "Message is required"}, status_code=400)
    
    try:
        # Create or use existing thread
        if not thread_id:
            thread = client.beta.threads.create()
            thread_id = thread.id
        
        # Add user message to thread
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=message
        )
        
        # Run the assistant
        run = client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=assistant_id,
        )
        
        # Poll until done
        while True:
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
            if run.status in ["completed", "failed", "cancelled", "expired"]:
                break
            time.sleep(1.2)
        
        if run.status == "completed":
            if run.usage:
                input_tokens = run.usage.prompt_tokens
                output_tokens = run.usage.completion_tokens
                usage_dict = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens
                }
            else:
                usage_dict = {"input_tokens": 0, "output_tokens": 0}
            messages = client.beta.threads.messages.list(thread_id=thread_id, order="asc")
            chat_history = []
            
            for msg in messages.data:
                content = ""
                for block in msg.content:
                    if block.type == "text":
                        content += block.text.value
                
                chat_history.append({
                    "role": msg.role,
                    "content": content,
                    "timestamp": msg.created_at
                })

            await log_prompt_and_update_activity(
            db=db,
            project_name=PROJECT_NAME,
            user_email=current_user["email"],
            prompt_text=message,
            prompt_type="VICP Chat",
            model=run.model,
            input_tokens=usage_dict["input_tokens"],
            output_tokens=usage_dict["output_tokens"],
            )
            
            return JSONResponse({
                "success": True,
                "thread_id": thread_id,
                "chat_history": chat_history
            })
        else:
            return JSONResponse({
                "success": False,
                "error": f"Assistant failed: {run.status}"
            }, status_code=500)
            
    except Exception as e:
        return JSONResponse({
            "success": False,
            "error": str(e)
        }, status_code=500)

@router.post("/api/get-history", response_class=JSONResponse)
async def get_history(request: Request, current_user: dict = Depends(get_current_user)):
    """Get chat history for an existing thread"""
    data = await request.json()
    thread_id = data.get("thread_id")
    
    if not thread_id:
        return JSONResponse({"error": "Thread ID is required"}, status_code=400)
    
    try:
        messages = client.beta.threads.messages.list(thread_id=thread_id, order="asc")
        chat_history = []
        
        for msg in messages.data:
            content = ""
            for block in msg.content:
                if block.type == "text":
                    content += block.text.value
            
            chat_history.append({
                "role": msg.role,
                "content": content,
                "timestamp": msg.created_at
            })
        
        return JSONResponse({
            "success": True,
            "thread_id": thread_id,
            "chat_history": chat_history
        })
        
    except Exception as e:
        return JSONResponse({
            "success": False,
            "error": str(e)
        }, status_code=500)

# --- templates ---- #
@router.post("/templates")
async def create_template(
    template_name: str = Form(...),
    description: str = Form(None),
    tags: str = Form(None),
    template_text: str = Form(...),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    new_template = Template(
        project_name=PROJECT_NAME,
        user_email=current_user["email"],
        template_name=template_name,
        description=description,
        tags=tags,
        template_text=template_text
    )
    db.add(new_template)
    db.commit()
    db.refresh(new_template)
    return {"status": "success", "template_id": new_template.id}

@router.get("/templates")
async def list_templates(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    template = db.query(Template).filter(
        Template.project_name == PROJECT_NAME,
        Template.user_email == current_user["email"]
    ).all()
    return [
        {"id": t.id, "name": t.template_name, "description": t.description, "tags": t.tags, "Full Text": t.template_text}
        for t in template
    ]

@router.get("/templates/{template_id}")
async def get_template(
    template_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    template = db.query(Template).filter(
        Template.project_name == PROJECT_NAME,
        Template.id == template_id,
        Template.user_email == current_user["email"]
    ).first()
    if not template:
        raise HTTPException(404, "Template not found")
    return {"template_text": template.template_text, "name": template.template_name}

@router.delete("/templates/{template_id}")
async def delete_template(
    template_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    template = db.query(Template).filter(
        Template.project_name == PROJECT_NAME,
        Template.id == template_id,
        Template.user_email == current_user["email"]
    ).first()

    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    db.delete(template)
    db.commit()

    return {
        "status": "success",
        "message": "Template deleted successfully",
        "template_id": template_id
    }

# --- VICP Excel Ledger Routes --- #
@router.post("/generate/ledger")
async def generate_vicp_ledger(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    contents = await file.read()
    run_id = await generate_ledger(
        contents,
        current_user["email"],
        db,
        file.filename
    )
    return {
        "message": "Ledger generated successfully",
        "run_id": run_id
    }

@router.get("/ledger/runs")
async def get_runs(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):

    runs = db.query(LedgerRun).order_by(
        LedgerRun.created_at.desc()
    ).all()
    return runs

@router.get("/ledger/{run_id}")
async def get_ledger(run_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    rows = db.query(LedgerEntry).filter(
        LedgerEntry.run_id == run_id,
        LedgerEntry.status != "deleted"
    ).order_by(LedgerEntry.id).all()
    # return rows
    result = []

    for entry in rows:
        result.append({
            "id": entry.id,
            "run_id": entry.run_id,
            "date": entry.date,
            "timekeeper": entry.timekeeper,
            "source_staff_code": entry.source_staff_code,
            "assigned_staff_code": entry.assigned_staff_code,
            "assigned_role": entry.assigned_role,
            "entry_type": entry.entry_type,
            "sheet_name": entry.sheet_name,
            "needs_review": entry.needs_review,
            "description": entry.description,
            "original_text": entry.audit.original_memo if entry.audit else None,
            "hours": entry.hours,
            "category": entry.category,
            "source": entry.source,
            "status": entry.status,
            "updated_at": "null"
        })
    return result

ROLE_MAP = {
    "lead_attorney" : "JPR",
    "paralegal" : "KHO"
}
ENTRY_TYPE_MAP = {
    "paralegal": "direct",
    "lead_attorney": "derived"
}

@router.patch("/ledger/entry/{entry_id}")
async def edit_entry(
    entry_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    entry = db.query(LedgerEntry).filter(
        LedgerEntry.id == entry_id
    ).first()

    if not entry:
        return {"error": "Entry not found"}

    fields = ["date", "description", "hours", "category", "assigned_role"]

    for field in fields:

        if field in payload:

            old_value = getattr(entry, field)
            new_value = payload[field]

            if str(old_value) != str(new_value):

                history = LedgerEditHistory(
                    entry_id=entry.id,
                    field_name=field,
                    old_value=str(old_value),
                    new_value=str(new_value),
                    edited_by=current_user["email"]
                )

                db.add(history)

                setattr(entry, field, new_value)
    
    if "assigned_role" in payload:
        role = payload["assigned_role"].lower()

        entry.assigned_role = role
        entry.assigned_staff_code = ROLE_MAP.get(role) 
        entry.entry_type = ENTRY_TYPE_MAP.get(role)

    entry.status = "edited"
    entry.updated_at = func.now()

    db.commit()

    return {"message": "Entry updated"}

@router.patch("/ledger/entry/{entry_id}/approve")
async def approve_entry(
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    entry = db.query(LedgerEntry).filter(
        LedgerEntry.id == entry_id
    ).first()

    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    entry.status = "approved"

    db.commit()

    return {"message": "Entry approved"}

@router.delete("/ledger/entry/{entry_id}")
async def delete_entry(
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    entry = db.query(LedgerEntry).filter(
        LedgerEntry.id == entry_id
    ).first()

    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    entry.status = "deleted"
    db.commit()
    return {"message": "Entry deleted"}

@router.patch("/ledger/bulk-approve")
async def bulk_approve(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    entry_ids = payload.get("entry_ids", [])
    entries = db.query(LedgerEntry).filter(
        LedgerEntry.id.in_(entry_ids)
    ).all()

    for entry in entries:
        entry.status = "approved"
    db.commit()

    return {"message": "Entries approved"}

@router.get("/ledger/{run_id}/export")
async def export_ledger(run_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):

    entries = db.query(LedgerEntry).filter(
        LedgerEntry.run_id == run_id,
        LedgerEntry.status == "approved"
    ).order_by(LedgerEntry.date).all()

    if not entries:
        return {"error": "No approved entries. Please approve entries before exporting."}

    data = []

    for e in entries:
        data.append({
            "Date": e.date,
            "Timekeeper": e.timekeeper,
            "Task Description": e.description,
            "Hours": e.hours,
            "Category": e.category
        })

    df = pd.DataFrame(data)

    # Summary---
    total_hours = df["Hours"].sum()
    total_entries = len(df)

    hours_by_category = df.groupby("Category")["Hours"].sum().reset_index()

    summary = pd.DataFrame({
        "Metric": ["Total Entries", "Total Attorney Hours"],
        "Value": [total_entries, total_hours]
    })

    buffer = io.BytesIO()
    # df.to_excel(buffer, index=False)
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        df.to_excel(writer, sheet_name="Ledger", index=False)
        summary.to_excel(writer, sheet_name="Summary", index=False)
        hours_by_category.to_excel(writer, sheet_name="Summary", startrow=5, index=False)
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition":
            "attachment; filename=vicp_attorney_ledger.xlsx"
        }
    )


@router.get("/ledger/{run_id}/edit-history")
async def get_run_edit_history(
    run_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    rows = (
        db.query(LedgerEditHistory, LedgerEntry)
        .join(LedgerEntry, LedgerEntry.id == LedgerEditHistory.entry_id)
        .filter(LedgerEntry.run_id == run_id)
        .order_by(LedgerEditHistory.edited_at.desc())
        .all()
    )

    grouped = defaultdict(list)

    for h, entry in rows:
        grouped[entry.id].append({
            "field": h.field_name,
            "old_value": h.old_value,
            "new_value": h.new_value,
            "edited_by": h.edited_by,
            "edited_at": h.edited_at
        })

    return {
        "run_id": run_id,
        "total_entries_edited": len(grouped),   # key metric
        "entries": [
            {
                "entry_id": entry_id,
                "changes": changes
            }
            for entry_id, changes in grouped.items()
        ]
    }


# --- Files ledger Generation Routes --- #
@router.post("/generate-summary-lead-paralegal")
async def generate_summary_from_docs(
    petition_file: UploadFile = File(...),
    exhibits_file: UploadFile = File(...),
    paralegal_entries: int = Form(...),
    total_paralegal_hours: float = Form(...),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    run_id = str(uuid.uuid4())
    file_data = []

    petition_bytes = await petition_file.read()
    exhibits_bytes = await exhibits_file.read()

    file_data = [
        {"name": petition_file.filename.lower(), "bytes": petition_bytes},
        {"name": exhibits_file.filename.lower(), "bytes": exhibits_bytes}
    ]

    context_text = await extract_context(file_data) 

    attorney_entries_count = int(paralegal_entries * 0.20)
    summary = await generate_full_ledger(
        context_text,
        paralegal_entries,
        total_paralegal_hours,
        attorney_entries_count
    )
    ledger = summary["raw_ledger"]

    # ---- COMPUTE ATTORNEY SUMMARY ----
    summary_metrics = await attorney_summary_metrices(ledger)
    run = LedgerSummaryRun(
        run_id=run_id,
        user_email=current_user["email"],
        petition_filename=petition_file.filename,
        exhibits_filename=exhibits_file.filename,
        paralegal_entries_requested=paralegal_entries,
        attorney_entries_generated=attorney_entries_count,
        total_paralegal_hours_requested=total_paralegal_hours,
        summary=summary_metrics,
        status="success"
    )

    db.add(run)
    db.flush()

    for entry in ledger:
        db.add(
            LedgerSummaryEntry(
                run_id=run_id,
                assigned_role=entry["assigned_role"],
                task=entry["task"],
                hours=entry["hours"],
                category=entry.get("category")
            )
        )

    db.commit()
    
    return {
        "summary": summary,
        "lead_attorney_entries": attorney_entries_count,
        "run_id": run_id,
        "metrics": summary_metrics
    }

@router.get("/ledger-summary-runs")
async def get_all_ledger_summary_runs(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    runs = db.query(LedgerSummaryRun).order_by(desc(LedgerSummaryRun.created_at)).all()

    return [
        {
            "id": run.id,
            "run_id": run.run_id,
            "user_email": run.user_email,
            "petition_filename": run.petition_filename,
            "exhibits_filename": run.exhibits_filename,
            "paralegal_entries_requested": run.paralegal_entries_requested,
            "attorney_entries_generated": run.attorney_entries_generated,
            "total_paralegal_hours_requested": run.total_paralegal_hours_requested,
            "summary": run.summary,
            "status": run.status,
            "created_at": run.created_at
        }
        for run in runs
    ]

@router.get("/ledger-summary-entries/{run_id}")
async def get_ledger_summary_entries_by_run_id(
    run_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    entries = db.query(LedgerSummaryEntry).filter(
        LedgerSummaryEntry.run_id == run_id
    ).all()

    return [
        {
            "id": e.id,
            "run_id": e.run_id,
            "assigned_role": e.assigned_role,
            "task": e.task,
            "hours": e.hours,
            "category": e.category,
            "created_at": e.created_at
        }
        for e in entries
    ]

# --- Admin Routes --- #
@admin_router.get("/VICP_Attorney_Fees/active-today")
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

@admin_router.get("/VICP_Attorney_Fees/error-logs")
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

@admin_router.get("/VICP_Attorney_Fees/prompt_logs")
async def get_prompt_logs(
    user_email: Optional[str] = None,
    prompt_type: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    query = db.query(PromptLog).filter(
        PromptLog.project_name == PROJECT_NAME
    )

    if user_email:
        query = query.filter(PromptLog.user_email == user_email)

    if prompt_type:
        query = query.filter(PromptLog.prompt_type == prompt_type)

    logs = (
        query
        .order_by(PromptLog.timestamp.desc())
        .limit(limit)
        .all()
    )

    if not logs:
        raise HTTPException(status_code=404, detail="No prompt logs found")

    return [
        {
            "id": log.id,
            "user_email": log.user_email,
            "prompt_text": log.prompt_text,
            "prompt_type": log.prompt_type,
            "model": log.model,
            "input_tokens": log.input_tokens,
            "output_tokens": log.output_tokens,
            "input_cost_usd": log.input_cost_usd,
            "output_cost_usd": log.output_cost_usd,
            "timestamp": log.timestamp.isoformat()
        }
        for log in logs
    ]