from fastapi import Request, APIRouter, Depends, UploadFile, File, Depends, Query, Form, HTTPException
from fastapi.responses import HTMLResponse
from Playbook_chatbot.chat_service.rag_service import generate_answer
from Playbook_chatbot.ingestion_service.ingestion_pipeline import process_upload
from Playbook_chatbot.ingestion_service.blob_service import list_blobs, generate_blob_sas_url, delete_blob
from Playbook_chatbot.shared.config import AZURE_OPENAI_CHAT_DEPLOYMENT, EDITOR_UPLOAD_CODE
from Playbook_chatbot.ingestion_service.index_service import delete_existing
from fastapi.templating import Jinja2Templates
from auth_utils import authenticate_user, create_access_token, get_current_user, ACCESS_TOKEN_EXPIRE_MINUTES, authenticate_editor, create_editor_token, get_current_editor, reset_password, generate_otp, verify_otp, SessionLocal, create_password, EDITOR_ACCESS_TOKEN_EXPIRE_MINUTES
from typing import Optional, List
from admin.router import admin_router
from db import get_db, log_prompt_and_update_activity, save_chat_history, Session, ActiveSession,ApiErrorLog ,PromptLog, Whitelist, ChatHistoryPlaybook
from fastapi.security import OAuth2PasswordRequestForm
from datetime import datetime, timedelta
from sqlalchemy import func
import re
router = APIRouter()
PROJECT_NAME = "Playbook_Chatbot"
jinja_templates = Jinja2Templates(directory="Playbook_chatbot/templates")

openai_rag_model_cost = {
    "input_per_million": 2.50,
    "output_per_million": 15.00
} 

async def openai_rag_cost(input_tokens: int, output_tokens: int):
    input_cost = (input_tokens / 1_000_000) * openai_rag_model_cost["input_per_million"]
    output_cost = (output_tokens / 1_000_000) * openai_rag_model_cost["output_per_million"]

    return {
        "input_cost": round(input_cost, 6),
        "output_cost": round(output_cost, 6)
    }

async def extract_referenced_docs(answer: str):

    if not answer or "References:" not in answer:
        return []

    # Extract reference block
    ref_section = answer.split("References:")[-1]
    # Extract filenames
    refs = re.findall(r"-\s*(.+)", ref_section)
    if not refs:
        return []

    # Get blobs from storage
    blobs = await list_blobs()
    blob_names = [b["filename"] for b in blobs]
    matched_files = []

    for ref in refs:
        ref_clean = ref.strip()
        # remove extension if present
        ref_base = ref_clean.rsplit(".", 1)[0].lower()

        for blob in blob_names:
            blob_base = blob.rsplit(".", 1)[0].lower()
            if ref_base in blob_base:
                matched_files.append(blob)
                break
    return list(set(matched_files))

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

@router.post("/editor/login")
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):

    user = await authenticate_editor(
        form_data.username,
        form_data.password
    )

    access_token = await create_editor_token(
        data={
            "sub": user["email"],
            "role": user["role"]
        },
        expires_delta=timedelta(
            minutes=EDITOR_ACCESS_TOKEN_EXPIRE_MINUTES
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

@router.get("/editor/verify-email")
async def verify_email(email: str):

    db = SessionLocal()

    try:

        user = db.query(Whitelist).filter(
            Whitelist.email == email.lower(),
            Whitelist.role == "editor"
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

@router.post("/chat")
async def chat(body: dict, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    query = body.get("question")
    if not query:
        raise HTTPException(status_code=400, detail="Question is required")
    user_email = current_user["email"]

    history = (
    db.query(ChatHistoryPlaybook)
    .filter(ChatHistoryPlaybook.user_email == user_email)
    .order_by(ChatHistoryPlaybook.created_at.desc())
    .limit(3)
    .all()
    )
    history = list(reversed(history))

    conversation_context = "\n".join([h.question for h in history])

    answer, tokens = await generate_answer(query, chat_history=conversation_context)
    await save_chat_history(db, user_email, query, answer)
    cost_data = await openai_rag_cost(
    input_tokens=tokens["input_tokens"],
    output_tokens=tokens["output_tokens"]
    )
    input_cost = cost_data["input_cost"]
    output_cost = cost_data["output_cost"]
    prompt_log = await log_prompt_and_update_activity(
                    db=db,
                    project_name=PROJECT_NAME,
                    user_email=current_user["email"],
                    prompt_text=query,
                    prompt_type="Playbook Chat",
                    model=AZURE_OPENAI_CHAT_DEPLOYMENT,
                    input_tokens=tokens["input_tokens"],
                    output_tokens=tokens["output_tokens"],
                    input_cost_usd = input_cost,
                    output_cost_usd = output_cost
                )
    
    referenced_files = await extract_referenced_docs(answer)
    references = []
    for file in referenced_files:
        sas_url = await generate_blob_sas_url(file)

        references.append({
            "filename": file,
            "url": sas_url
        })

    return {"answer": answer, "prompt_log_id": prompt_log.id, "references": references}

@router.post("/chat/feedback")
async def submit_feedback(
    body: dict,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    prompt_log_id = body.get("prompt_log_id")
    feedback = body.get("feedback")

    if feedback not in ["up", "down"]:
        raise HTTPException(status_code=400, detail="Invalid feedback")

    prompt_log = db.query(PromptLog).filter(
        PromptLog.id == prompt_log_id,
        PromptLog.user_email == current_user["email"],
        PromptLog.project_name == PROJECT_NAME
    ).first()

    if not prompt_log:
        raise HTTPException(status_code=404, detail="Prompt log not found")

    if prompt_log.response_feedback is not None:
        return {
            "message": "Feedback already recorded",
            "current_feedback": prompt_log.response_feedback
        }

    prompt_log.response_feedback = (
        "thumbs_up" if feedback == "up" else "thumbs_down"
    )

    db.commit()

    return {"message": "Feedback recorded"}


@router.get("/active-today")
async def active_today(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    today = datetime.utcnow().date()
    active = db.query(ActiveSession.user_email, ActiveSession.last_activity_timestamp).filter(
        ActiveSession.project_name == PROJECT_NAME,
        func.date(ActiveSession.last_activity_timestamp) == today
    ).all()
    return [{"email": email, "last_active": ts.isoformat()} for email, ts in active]

@admin_router.post("/Playbook_Chatbot/upload")
async def upload(file: UploadFile = File(...), department: Optional[str] = Form(None), regenerate: bool = Query(False),  db: Session = Depends(get_db)):
    filename = file.filename
    if not (filename.lower().endswith(".pdf") or filename.lower().endswith(".docx")):
        raise HTTPException(
            status_code=400,
            detail="Only PDF and DOCX files are allowed"
        )
    result = await process_upload(file, department=department, regenerate=regenerate)
    return {"result": result}


@admin_router.get("/Playbook_Chatbot/error-logs")
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

@admin_router.get("/Playbook_Chatbot/prompt_logs")
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
            "response_feedback": log.response_feedback,
            "timestamp": log.timestamp.isoformat()
        }
        for log in logs
    ]

@admin_router.get("/Playbook_Chatbot/active-today")
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

@admin_router.get("/Playbook_Chatbot/editors")
async def view_editors(db: Session = Depends(get_db)):

    records = db.query(Whitelist).filter(
        Whitelist.role == "editor"
    ).all()

    members = [
        {
            "name": r.name,
            "email": r.email
        }
        for r in records
    ]

    return {"members": members}

@admin_router.post("/Playbook_Chatbot/editors")
async def add_editor(name: str = Form(...), email: str = Form(...), db: Session = Depends(get_db)):

    existing = db.query(Whitelist).filter(
        Whitelist.email == email.lower(),
        Whitelist.role == "editor"
    ).first()

    if existing:
        return {
            "message": "Email already exists",
            "member": email
        }

    db.add(Whitelist(
        name=name,
        email=email.lower(),
        role="editor"
    ))

    db.commit()

    return {
        "message": "Member added successfully",
        "member": email
    }


@admin_router.delete("/Playbook_Chatbot/editors")
async def remove_editor(email: str, db: Session = Depends(get_db)):

    record = db.query(Whitelist).filter(
        Whitelist.email == email.lower(),
        Whitelist.role == "editor"
    ).first()

    if not record:
        raise HTTPException(status_code=404, detail="Email not found in whitelist")

    db.delete(record)
    db.commit()

    return {
        "message": "Member removed successfully",
        "email": email
    }

@router.post("/editor/upload")
async def editor_upload(
    file: UploadFile = File(...),
    department: Optional[str] = Form(None),
    regenerate: bool = Query(False),
    editor_upload_code: str = Form(...),
    editor: dict = Depends(get_current_editor),
    db: Session = Depends(get_db)
):
    filename = file.filename

    if not editor_upload_code or editor_upload_code != EDITOR_UPLOAD_CODE:
        raise HTTPException(
            status_code=403,
            detail="Invalid security code for file upload"
        )

    # Handle .PDF / .Docx etc
    if not filename.lower().endswith((".pdf", ".docx")):
        raise HTTPException(
            status_code=400,
            detail="Only PDF and DOCX files are allowed"
        )

    result = await process_upload(
        file,
        department=department,
        regenerate=regenerate
    )

    return {
        "file": filename,
        "result": result
    }

@router.get("/editor/whitelist")
async def get_editor_whitelist(editor: dict = Depends(get_current_editor), db: Session = Depends(get_db)):

    records = db.query(Whitelist).filter(
        Whitelist.role == "editor"
    ).all()

    members = [
        f"{r.name}; {r.email}"
        for r in records
    ]

    return {"editors": members}

@router.get("/editor/files")
async def get_ingested_files(editor: dict = Depends(get_current_editor)):
    files = await list_blobs()
    return {
        "total_files": len(files),
        "files": files
    }

@router.delete("/editor/file/{filename}")
async def delete_editor_file(
    filename: str,
    editor: dict = Depends(get_current_editor)
):
    # Delete from blob storage
    await delete_blob(filename)

    # Delete embeddings from search index
    await delete_existing(filename)

    return {
        "status": "File and its embeddings deleted successfully",
        "file": filename
    }

# Endpoints for user chat history management in UI 
@router.get("/chat/latest")
async def get_latest_chats(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    user_email = current_user["email"]

    chats = (
        db.query(ChatHistoryPlaybook)
        .filter(ChatHistoryPlaybook.user_email == user_email)
        .order_by(ChatHistoryPlaybook.created_at.desc())
        .limit(3)
        .all()
    )

    if not chats:
        return {"history": []}

    chats = list(reversed(chats))

    return {
        "history": [
            {
                "question": chat.question,
                "answer": chat.answer,
                "timestamp": chat.created_at.isoformat()
            }
            for chat in chats
        ]
    } 

@router.delete("/chat/clear")
async def clear_chat_history(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    db.query(ChatHistoryPlaybook).filter(
        ChatHistoryPlaybook.user_email == current_user["email"]
    ).delete()

    db.commit()

    return {"message": "Chat cleared"}