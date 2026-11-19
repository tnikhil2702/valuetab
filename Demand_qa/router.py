from fastapi import APIRouter, UploadFile, File, HTTPException, Form, Request, Depends
from fastapi.security import OAuth2PasswordRequestForm
from datetime import datetime, timedelta
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from typing import List, Optional
import fitz  # PyMuPDF
from docx import Document
import io
import os
from openai import OpenAI
from sqlalchemy import func
from dotenv import load_dotenv
from db import get_db, log_prompt_and_update_activity, Template ,Session, ActiveSession, Session, ApiErrorLog, PromptLog, Whitelist
from admin.router import admin_router
import uuid
import tiktoken
from auth_utils import authenticate_user, create_access_token, get_current_user, ACCESS_TOKEN_EXPIRE_MINUTES, reset_password, generate_otp, verify_otp, SessionLocal, create_password

load_dotenv()

router = APIRouter()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

PROMPT_ID = os.getenv("DEMAND_QUALITY_ASSURANCE_PROMPT_ID")
PROJECT_NAME = "Demand_Quality_Assurance"

session_data = {}

# --- Utility Functions --- #
def extract_text_from_pdf(content: bytes) -> str:
    try:
        doc = fitz.open(stream=io.BytesIO(content), filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text()
        return text
    except Exception as e:
        raise HTTPException(400, detail=f"Error reading PDF: {str(e)}")

def extract_text_from_docx(content: bytes) -> str:
    try:
        doc = Document(io.BytesIO(content))
        return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
    except Exception as e:
        raise HTTPException(400, detail=f"Error reading DOCX: {str(e)}")

def extract_text_from_txt(content: bytes) -> str:
    try:
        return content.decode("utf-8", errors="ignore")
    except:
        raise HTTPException(400, detail="Error reading TXT file")


# --------------- chunking --------------------- # 
def chunk_text(text: str, max_tokens: int = 18000) -> List[str]:
    enc = tiktoken.encoding_for_model("gpt-4o-mini")
    tokens = enc.encode(text)
    chunks = []
    for i in range(0, len(tokens), max_tokens):
        chunks.append(enc.decode(tokens[i:i + max_tokens]))
    return chunks

jinja_templates = Jinja2Templates(directory="Demand_qa/templates")

@router.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return jinja_templates.get_template("index.html").render({"request": request})

# ------------------- LOGIN -------------------
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

# ------------------- UPLOAD -------------------
@router.post("/upload")
async def upload_files(
    files: List[UploadFile] = File(...),
    current_user: dict = Depends(get_current_user)
):
    if not files:
        raise HTTPException(400, detail="No files uploaded")
    if len(files) > 3:
        raise HTTPException(400, detail="Maximum 3 files allowed")

    session_id = str(uuid.uuid4())
    merged_text = ""

    for file in files:
        content = await file.read()
        ext = os.path.splitext(file.filename)[1].lower()

        if ext == ".pdf":
            text = extract_text_from_pdf(content)
        elif ext == ".docx":
            text = extract_text_from_docx(content)
        elif ext == ".txt":
            text = extract_text_from_txt(content)
        else:
            raise HTTPException(400, detail=f"Unsupported file: {file.filename}")

        merged_text += f"\n\n--- FILE: {file.filename} ---\n{text}"

    chunks = chunk_text(merged_text)

    session_data[session_id] = {
        "chunks": chunks,
        "response_id": None,
        "chat_history": []
    }

    return {
        "session_id": session_id,
        "total_chunks": len(chunks)
    }

# ------------------- PROCESS -------------------
@router.post("/process")
async def process(
    session_id: str = Form(...),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if session_id not in session_data:
        raise HTTPException(404, detail="Session not found. Upload files first.")

    session = session_data[session_id]
    chunks = session.get("chunks")

    if not chunks:
        raise HTTPException(400, detail="No document content found for this session.")

    instruction = (
        "Generate a high-quality, optimized demand letter based on the following"
        "medical records and documents."
    )

    try:
        content_parts = []

        for i, chunk in enumerate(chunks):
            content_parts.append({
                "type": "input_text",
                "text": f"Document Chunk {i + 1}/{len(chunks)}:\n\n{chunk}"
            })

        content_parts.append({
            "type": "input_text",
            "text": instruction
        })

        response = client.responses.create(
            prompt={"id": PROMPT_ID},
            input=[
                {
                    "role": "user",
                    "content": content_parts
                }
            ],
            store=True
        )
        if response.usage:
            input_tokens = response.usage.input_tokens 
            output_tokens = response.usage.output_tokens
            usage_dict = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens
            }
        else:
            usage_dict = {"input_tokens": 0, "output_tokens": 0}

        await log_prompt_and_update_activity(
        db=db,
        project_name=PROJECT_NAME,
        user_email=current_user["email"],
        prompt_text="Processed document for demand letter generation",
        prompt_type="Demand QA Process",
        model=response.model,
        input_tokens=usage_dict.get("input_tokens"),
        output_tokens=usage_dict.get("output_tokens"),
    )

        report_parts = []
        for item in response.output or []:
            for part in getattr(item, "content", []) or []:
                if getattr(part, "text", None):
                    report_parts.append(part.text)

        report = "\n".join(report_parts) or "No report generated."

        session["response_id"] = response.id
        session["chat_history"].append({
            "role": "assistant",
            "content": report,
            "type": "process"
        })

        return {
            "status": "success",
            "report": report,
            "response_id": response.id
        }

    except Exception as e:
        raise HTTPException(500, detail=f"Processing failed: {str(e)}")

# ------------------- CHAT -------------------
@router.post("/chat")
async def chat(
    session_id: str = Form(...),
    message: str = Form(...),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if session_id not in session_data:
        raise HTTPException(404, detail="Session not found")

    session = session_data[session_id]
    response_id = session.get("response_id")

    if not response_id:
        raise HTTPException(400, detail="Run /process first")

    try:
        response = client.responses.create(
            prompt={"id": PROMPT_ID},
            previous_response_id=response_id,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": message
                        }
                    ]
                }
            ],
            store=True
        )
        if response.usage:
            input_tokens = response.usage.input_tokens 
            output_tokens = response.usage.output_tokens
            usage_dict = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens
            }
        else:
            usage_dict = {"input_tokens": 0, "output_tokens": 0}

        reply_parts = []
        for item in response.output or []:
            for part in getattr(item, "content", []) or []:
                if getattr(part, "text", None):
                    reply_parts.append(part.text)

        reply = "\n".join(reply_parts) or "No response generated."

        # Update session state
        session["response_id"] = response.id
        session["chat_history"].extend([
            {"role": "user", "content": message, "type": "chat"},
            {"role": "assistant", "content": reply, "type": "chat"}
        ])

        await log_prompt_and_update_activity(
            db=db,
            project_name=PROJECT_NAME,
            user_email=current_user["email"],
            prompt_text=message,
            prompt_type="Demand QA chat",
            model=response.model,
            input_tokens=usage_dict["input_tokens"],
            output_tokens=usage_dict["output_tokens"],
        )

        return {
            "status": "success",
            "response": reply,
            "response_id": response.id
        }

    except Exception as e:
        raise HTTPException(500, detail=f"Chat error: {str(e)}")

@router.get("/chat/history/{session_id}")
async def get_chat_history(session_id: str, current_user: dict = Depends(get_current_user)):
    if session_id not in session_data:
        raise HTTPException(404, detail="Session not found")
    return {"messages": session_data[session_id]["chat_history"]}

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

@router.get("/active-today")
async def active_today(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    today = datetime.utcnow().date()
    active = db.query(ActiveSession.user_email, ActiveSession.last_activity_timestamp).filter(
        ActiveSession.project_name == PROJECT_NAME,
        func.date(ActiveSession.last_activity_timestamp) == today
    ).all()
    return [{"email": email, "last_active": ts.isoformat()} for email, ts in active]

@admin_router.get("/Demand_Quality_Assurance/active-today")
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


@admin_router.get("/Demand_Quality_Assurance/error-logs")
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

@admin_router.get("/Demand_Quality_Assurance/prompt_logs")
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