# app.py
from fastapi import APIRouter, UploadFile, File, HTTPException, Form, Request, Depends
from fastapi.security import OAuth2PasswordRequestForm
from datetime import datetime, timedelta 
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from typing import List, Optional
import fitz
from docx import Document
import io
import os
import uuid
import tiktoken
from openai import OpenAI
from dotenv import load_dotenv
from sqlalchemy import func
from db import get_db, log_prompt_and_update_activity, ActiveSession, Session, ApiErrorLog, PromptLog, Whitelist
from admin.router import admin_router
from auth_utils import authenticate_user, create_access_token, get_current_user, ACCESS_TOKEN_EXPIRE_MINUTES, reset_password, generate_otp, verify_otp, SessionLocal, create_password
load_dotenv()

router = APIRouter()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
PROMPT_ID = os.getenv("DEMAND_OPTIMIZER_PROMPT_ID") 
session_data = {}
PROJECT_NAME ="Demand_optimizer"
# ------------------- Text Extraction -------------------
def extract_text_from_pdf(content: bytes) -> str:
    doc = fitz.open(stream=io.BytesIO(content), filetype="pdf")
    text = ""
    for page in doc:
        text += page.get_text()
    return text

def extract_text_from_docx(content: bytes) -> str:
    doc = Document(io.BytesIO(content))
    return "\n".join(p.text for p in doc.paragraphs)

def extract_text_from_txt(content: bytes) -> str:
    return content.decode("utf-8", errors="ignore")

# ------------------- Chunking -------------------
def chunk_text(text: str, max_tokens: int = 18000) -> List[str]:
    enc = tiktoken.encoding_for_model("gpt-4o-mini")
    tokens = enc.encode(text)
    chunks = []
    for i in range(0, len(tokens), max_tokens):
        chunks.append(enc.decode(tokens[i:i + max_tokens]))
    return chunks

jinja_templates = Jinja2Templates(directory="Demand_optimizer/templates")

@router.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return jinja_templates.get_template("index.html").render({
        "request": request
    })

# Login
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

# ------------------- Upload -------------------
@router.post("/upload")
async def upload_files(files: List[UploadFile] = File(...), current_user: dict = Depends(get_current_user)):
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
        merged_text += f"\n\n--- FILE: {file.filename} ---\n\n{text}"

    chunks = chunk_text(merged_text)

    session_data[session_id] = {
        "chunks": chunks,
        "response_id": None,
        "chat_history": []
    }

    return {"session_id": session_id, "total_chunks": len(chunks)}

@router.post("/process")
async def process(session_id: str = Form(...), current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if session_id not in session_data:
        raise HTTPException(404, detail="Session not found")

    data = session_data[session_id]
    chunks = data["chunks"]

    if not PROMPT_ID:
        raise HTTPException(500, detail="OPENAI_PROMPT_ID not set in .env")

    content_parts = []
    for i, chunk in enumerate(chunks):
        content_parts.append({
            "type": "input_text",
            "text": f"Document Chunk {i+1}/{len(chunks)}:\n\n{chunk}"
        })

    content_parts.append({
        "type": "input_text",
        "text": "Generate the optimized demand letter based on the documents above."
    })

    try:
        request_params = {
            "input": [{"role": "user", "content": content_parts}],
            "store": True,
            "reasoning": {
                "summary": "auto"
            },
            "include": [
                "reasoning.encrypted_content",
                "web_search_call.action.sources"
            ]
        }
        prompt_config = {"id": PROMPT_ID}
        # if PROMPT_VERSION:
        #     prompt_config["version"] = PROMPT_VERSION
        request_params["prompt"] = prompt_config

        if data["response_id"]:
            request_params["previous_response_id"] = data["response_id"]

        response = client.responses.create(**request_params)
        # response = client.responses.create(
        #     prompt={"id": PROMPT_ID},
        #     input=[{"role": "user", "content": content_parts}],
        #     previous_response_id=data["response_id"],
        #     store=True
        # )

        print("completed response:", response)

        # report = "".join([part.text for item in response.output for part in item.content if hasattr(part, "text")])
        
        # Extract report text
        report_parts = []
        if hasattr(response, 'output') and response.output is not None:
            for item in response.output:
                if hasattr(item, "content") and item.content is not None:
                    for part in item.content:
                        if hasattr(part, "text") and part.text:
                            report_parts.append(part.text)
        
        report = "\n".join(report_parts) if report_parts else "No report generated."

        # Save new thread ID
        data["response_id"] = response.id
        data["chat_history"].append({"role": "assistant", "content": report, "type": "process"})

        if response.usage:
            usage = response.usage

            input_tokens = usage.input_tokens
            output_tokens = usage.output_tokens
            usage_dict = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens
            }
        else:
            usage_dict = {"input_tokens": 0, "output_tokens": 0}
        # accumulate_tokens_usage(
        #     db=db,
        #     project_name=PROJECT_NAME,
        #     user_email=current_user["email"],
        #     model=response.model,
        #     new_input=usage_dict["Input_tokens"],
        #     new_output=usage_dict["Output_tokens"]
        # )
        await log_prompt_and_update_activity(
            db=db,
            project_name=PROJECT_NAME,
            user_email=current_user["email"],
            prompt_text="Processed uploaded documents to generate demand letter",
            prompt_type="Demand Optimizer File process",
            model=response.model,
            input_tokens=usage_dict["input_tokens"],
            output_tokens=usage_dict["output_tokens"]
        )

        return {
            "status": "success",
            "report": report,
            "response_id": response.id,
            "chat_id": session_id
        }

    except Exception as e:
        raise HTTPException(500, detail=f"OpenAI Responses API error: {str(e)}")

# ------------------- Chat (Follow-up Questions) -------------------
@router.post("/chat")
async def chat(session_id: str = Form(...), message: str = Form(...), current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if session_id not in session_data:
        raise HTTPException(404, detail="Session not found. Run /process first.")

    data = session_data[session_id]
    if not data["response_id"]:
        raise HTTPException(400, detail="Run /process first to initialize the analysis.")

    try:
        request_params = {
            "input": [{"role": "user", "content": [{"type": "input_text", "text": message}]}],
            "previous_response_id": data["response_id"],
            "store": True,
            "reasoning": {
                "summary": "auto"
            },
            "include": [
                "reasoning.encrypted_content",
                "web_search_call.action.sources"
            ]
        }

        prompt_config = {"id": PROMPT_ID}

        request_params["prompt"] = prompt_config

        response = client.responses.create(**request_params)
        if response.usage:
            usage = response.usage
            input_tokens = usage.input_tokens
            output_tokens = usage.output_tokens
            usage_dict = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens
            }
        else:
            usage_dict = {"input_tokens": 0, "output_tokens": 0}

        # Extract reply text
        reply_parts = []
        if hasattr(response, 'output') and response.output is not None:
            for item in response.output:
                if hasattr(item, "content") and item.content is not None:
                    for part in item.content:
                        if hasattr(part, "text") and part.text:
                            reply_parts.append(part.text)
        
        reply = "\n".join(reply_parts) if reply_parts else "No response generated."

        data["response_id"] = response.id
        data["chat_history"].extend([
            {"role": "user", "content": message, "type": "chat"},
            {"role": "assistant", "content": reply, "type": "chat"}
        ])

        await log_prompt_and_update_activity(
            db=db,
            project_name=PROJECT_NAME,
            user_email=current_user["email"],
            prompt_text=message,
            prompt_type="Demand Optimizer chat",
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

# ------------------- Other Endpoints -------------------
@router.get("/chat/history/{session_id}")
async def get_history(session_id: str, current_user: dict = Depends(get_current_user)):
    if session_id not in session_data:
        raise HTTPException(404)
    return {"messages": session_data[session_id]["chat_history"]}

@router.get("/active-today")
async def active_today(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    today = datetime.utcnow().date()
    active = db.query(ActiveSession.user_email, ActiveSession.last_activity_timestamp).filter(
        ActiveSession.project_name == PROJECT_NAME,
        func.date(ActiveSession.last_activity_timestamp) == today
    ).all()
    return [{"email": email, "last_active": ts.isoformat()} for email, ts in active]

@admin_router.get("/Demand_optimizer/active-today")
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

@admin_router.get("/Demand_optimizer/error-logs")
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

@admin_router.get("/Demand_optimizer/prompt_logs")
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