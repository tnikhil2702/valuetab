import asyncio
import time
from typing import List, Optional
from dotenv import load_dotenv
load_dotenv()
import requests
from fastapi import (
    HTTPException, File, UploadFile,
    Depends, Request,APIRouter
)
from fastapi.responses import JSONResponse, FileResponse
from fastapi.security import OAuth2PasswordRequestForm
from io import BytesIO
import threading
from .utils import SummarizeRequest, N8N_WEBHOOK_URL, CASE_JOBS, claude_client, CLAUDE_MODEL, check_case_summary_in_blob, container, calculate_cost, extract_from_pdf
from sqlalchemy import func
from datetime import datetime, timedelta
from db import get_db, log_prompt_and_update_activity, ActiveSession, Session, ApiErrorLog, PromptLog, Whitelist, CaseValueLogs
from admin.router import admin_router
from auth_utils import authenticate_user, create_access_token, get_current_user, reset_password, generate_otp, verify_otp, SessionLocal, create_password, ACCESS_TOKEN_EXPIRE_MINUTES


router = APIRouter()
PROJECT_NAME = "Casevalue"

# === API Endpoints ===
@router.get("/")
async def home():
    return FileResponse("casevalue/static/index.html")

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

@router.get("/case-status/{case_number}")
async def get_case_status(case_number: str, current_user: dict = Depends(get_current_user)):
    job = CASE_JOBS.get(case_number)
    if not job:
        raise HTTPException(404, "Case not started")

    if job["status"] == "completed":
        return {
            "case_number": case_number,
            "status": "completed",
            "final_summary": job["final_summary"]
        }
    elif job["status"] == "failed":
        return {**job, "final_summary": None}
    else:
        return {
            "case_number": case_number,
            "status": job["status"],
            "started_at": job["started_at"]
        }

@router.post("/summarize")
async def summarize(req: SummarizeRequest, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        message = claude_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=5000,            
            messages=[
                {
                    "role": "user",
                    "content": f"{req.prompt}\n\nContent:\n{req.text}"
                }
            ]
        )

        summary = message.content[0].text
        input_tokens = message.usage.input_tokens
        output_tokens = message.usage.output_tokens
        cost_data = await calculate_cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens
        )
        input_cost = cost_data["input_cost"]
        output_cost = cost_data["output_cost"]
        await log_prompt_and_update_activity(
            db=db,
            project_name=PROJECT_NAME,
            user_email=current_user["email"],
            prompt_text=req.prompt,
            prompt_type="CaseValue Tool - Summarization",
            model = CLAUDE_MODEL, 
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_cost_usd = input_cost,
            output_cost_usd = output_cost
        )
        return {"summary": summary}

    except Exception as e:
        return {"error": str(e)}


@router.post("/extract-text")
async def extract_text_endpoint(
    files: List[UploadFile] = File(..., description="Multiple PDFs expected from n8n"),
    current_user: dict = Depends(get_current_user)
):
    results = await asyncio.gather(
        *[extract_from_pdf(f) for f in files]
    )

    merged_text = "\n\n--- FILE BREAK ---\n\n".join(
        r["redacted_text"] for r in results
    )
    return {
        "merged_text": merged_text,
        "file_count": len(files),
        "total_characters": len(merged_text),
        "status": "success"
    }

@router.post("/start-case")
async def start_case(request: Request, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    data = await request.json()
    case_number = data.get("caseNumber")
    regenerate = data.get("regenerate", False)

    auth_header = request.headers.get("authorization")

    if not case_number:
        raise HTTPException(400, "Missing caseNumber")
    
    if not regenerate:
        existing_summary, blob_filename = await check_case_summary_in_blob(case_number)
        if existing_summary:            
            timestamp_part = blob_filename.split("summary_")[1].replace(".md", "")
            date_str, time_str = timestamp_part.split("_")
            
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H%M%S")
            date_time = dt.strftime("%d %B %Y at %H:%M")
            
            CASE_JOBS[case_number] = {
                "status": "completed",
                "final_summary": existing_summary,
                "source": "blob_storage",
                "completed_at": time.time(),
                "blob_filename": blob_filename
            }

            case_log = CaseValueLogs(
                project_name=PROJECT_NAME,
                user_email=current_user["email"],
                case_number=case_number,
                status="completed",
                prompt_type="CaseValue Tool - Case Run",
                processing_time_seconds=0,
                generation_type="blob_storage"
            )

            db.add(case_log)
            db.commit()

            return JSONResponse(
                status_code=200,
                content={
                    "status": "completed",
                    "case_number": case_number,
                    "message": f"Summary retrieved from blob storage (Generated Earlier on: {date_time})",
                    "final_summary": existing_summary,
                    "blob_filename": blob_filename
                }
            )

    CASE_JOBS[case_number] = {
        "status": "checking",
        "started_at": time.time(),
        "regenerate": regenerate
    }

    case_log = CaseValueLogs(
            project_name=PROJECT_NAME,
            user_email=current_user["email"],
            case_number=case_number,
            status="started",
            prompt_type="CaseValue Tool - Case Run"
            )

    db.add(case_log)
    db.commit()

    threading.Thread(target=lambda: requests.post(
        N8N_WEBHOOK_URL, 
        json={"caseNumber": case_number},
        headers={"Authorization": auth_header}
    ), daemon=True).start()

    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted",
            "case_number": case_number
        }
    )

@router.post("/case-complete")
async def case_complete(request: Request, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    data = await request.json()
    final_summary = data.get("summary")
    status = data.get("status", "completed")
    error_message = data.get("error")
    case_number = data.get("caseNumber")
    started_at = CASE_JOBS.get(case_number, {}).get("started_at")
    
    if not case_number:
        raise HTTPException(400, "Missing caseNumber in payload")

    if status == "failed":
        completed_at = time.time()

        CASE_JOBS[case_number] = {
            "status": "failed",
            "error": error_message or "Unknown error",
            "completed_at": completed_at,
            "started_at": started_at
        }

        case_log = (
        db.query(CaseValueLogs)
        .filter(
            CaseValueLogs.project_name == PROJECT_NAME,
            CaseValueLogs.case_number == case_number
        )
        .order_by(
            CaseValueLogs.timestamp.desc()
        )
        .first()
        )

        if case_log:
            case_log.status = "failed"
            case_log.processing_time_seconds = round(completed_at - started_at,2)
            case_log.generation_type = "failed"

            db.commit()

        return JSONResponse(
            status_code=200,
            content={
                "status": "failed",
                "error": error_message or f"Not found: {case_number}"
            }
        )

    if not final_summary:
        raise HTTPException(400, "Missing summary")

    completed_at = time.time()

    CASE_JOBS[case_number] = {
        "status": status,
        "final_summary": final_summary,
        "completed_at": completed_at,
        "started_at": started_at
    }

    case_log = (
        db.query(CaseValueLogs)
        .filter(
            CaseValueLogs.project_name == PROJECT_NAME,
            CaseValueLogs.case_number == case_number
        )
        .order_by(CaseValueLogs.timestamp.desc())
        .first()) 

    if case_log:
        case_log.status = "completed"
        case_log.processing_time_seconds = round(completed_at - started_at,2)
        case_log.generation_type = "Regenerated"

        db.commit()
    
    today = datetime.utcnow().date()
    timestamp = datetime.utcnow().strftime("%H%M%S")
    
    file_name = f"summary_{today}_{timestamp}.md"
    blob_path = f"{case_number}/{file_name}"

    file_content = (
        f"{final_summary}" 
    )
    file_bytes = BytesIO(file_content.encode("utf-8"))
    
    try:
        blob_list = container.list_blobs(name_starts_with=f"{case_number}/")
        md_files = [blob for blob in blob_list if blob.name.endswith(".md")]
        
        for old_blob in md_files:
            blob_client_old = container.get_blob_client(old_blob.name)
            blob_client_old.delete_blob()
        
        blob_client = container.get_blob_client(blob_path)
        blob_client.upload_blob(file_bytes, overwrite=True)
        download_url = blob_client.url
    except Exception as e:
        raise HTTPException(500, f"Failed to upload to Azure Blob: {e}")

    return {
        "status": "completed",
        "case_number": case_number,
        "final_summary": final_summary,
        "blob_file": file_name,
        "blob_path": blob_path,
        "download_url": download_url
    }


@router.get("/case-result/{case_number}")
async def case_result(case_number: str, current_user: dict = Depends(get_current_user)):
    job = CASE_JOBS.get(case_number)

    if not job:
        raise HTTPException(404, "Case not found")

    response = {
        "case_number": case_number,
        "status": job.get("status", "in-progress"),
        "completed_at": job.get("completed_at"),
        "pdf_status": job.get("pdf_status"),
        "billing_status": job.get("billing_status"),
    }

    if job.get("status") == "failed":
        response["error"] = f" This casefolder is not present in sharepoint : {case_number}"
        response["final_summary"] = None
        return response

    response["final_summary"] = job.get("final_summary")
    return response

@router.post("/case-pdf")
async def case_pdf(request: Request, current_user: dict = Depends(get_current_user)):
    data = await request.json()
    case_number = data.get("caseNumber")

    if not case_number:
        raise HTTPException(400, "Missing caseNumber in payload")

    try:
        response_data = {
            "name": data.get("name", "answer"),
            "value": data.get(
                "value",
                "No PDF files were found.\n\nThe folder is empty or contains no documents."
            )
        }
        CASE_JOBS.setdefault(case_number, {})
        CASE_JOBS[case_number]["pdf_status"] = response_data

        return response_data

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/case-billing")
async def case_billing(request: Request, current_user: dict = Depends(get_current_user)):
    try:
        data = await request.json()
        case_number = data.get("caseNumber")

        string_items = data.get("parameters", {}).get("values", {}).get("string", [])

        if not string_items:
            response_data = {"answer": "No billing information received."}
        else:
            response_data = {"answer": string_items[0].get("value")}

        CASE_JOBS.setdefault(case_number, {})
        CASE_JOBS[case_number]["billing_status"] = response_data

        return response_data

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/active-today")
async def active_today(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    today = datetime.utcnow().date()
    active = db.query(ActiveSession.user_email, ActiveSession.last_activity_timestamp).filter(
        ActiveSession.project_name == PROJECT_NAME,
        func.date(ActiveSession.last_activity_timestamp) == today
    ).all()
    return [{"email": email, "last_active": ts.isoformat()} for email, ts in active]

@admin_router.get("/casevalue/active-today")
async def active_today(db: Session = Depends(get_db)):
    today = datetime.utcnow().date()
    active = db.query(ActiveSession.user_email, ActiveSession.last_activity_timestamp).filter(
        ActiveSession.project_name == PROJECT_NAME,
        func.date(ActiveSession.last_activity_timestamp) == today
    ).all()
    return [{"email": email, "last_active": ts.isoformat()} for email, ts in active] 

@admin_router.get("/casevalue/error-logs")
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

@admin_router.get("/casevalue/prompt_logs")
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

@router.get("/dashboard/stats")
async def dashboard_stats(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        user_email = current_user["email"]
        logs = (
            db.query(CaseValueLogs)
            .filter(
                CaseValueLogs.project_name == PROJECT_NAME,
                CaseValueLogs.user_email == user_email,
                CaseValueLogs.prompt_type == "CaseValue Tool - Case Run"
            )
            .order_by(CaseValueLogs.timestamp.desc())
            .all()
        )

        total_cases = len(logs)

        successful_cases = len([
            log for log in logs
            if log.status == "completed"
        ])

        failed_cases = len([
            log for log in logs
            if log.status == "failed"
        ])

        # Average processing time
        durations = [
            log.processing_time_seconds
            for log in logs
            if log.processing_time_seconds
        ]

        average_time = (
            round(sum(durations) / len(durations), 2)
            if durations else 0
        )

        # Recent 3 cases
        return {
            "user": user_email,
            "total_cases": total_cases,
            "successful_cases": successful_cases,
            "failed_cases": failed_cases,
            "average_processing_time_seconds": average_time,
            "recent_cases": [
                {
                    "case_number": log.case_number,
                    "status": log.status,
                    "timestamp": log.timestamp.isoformat(),
                    "processing_time_seconds": log.processing_time_seconds,
                    "generation_type": log.generation_type
                }
                for log in logs[:3]
            ]
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )
