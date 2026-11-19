# router.py
import time
import base64
from datetime import datetime,timedelta
from fastapi import APIRouter, Request, UploadFile, Form, File,HTTPException,Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.templating import Jinja2Templates
from requests import Session
from redaction import Redactor
from email2ai.summarize_core_openai import run_staged_summary
from email2ai.prompts import PROMPT_TEMPLATES
import re
import io
import fitz
from PIL import Image
import pytesseract
from session_redis import get_session, set_session
from email2ai.utils import create_summary_docx, send_email_with_attachment, upload_blob,generate_blob_sas_url,blob_exists,delete_blob, save_attachment , decode_mime_words, authenticate_email
from email2ai.pinecone_client import upsert_chunks, query_chunks
from langchain_text_splitters import RecursiveCharacterTextSplitter
from admin.router import admin_router
from email2ai.config import SMTP_USERNAME, SMTP_PASSWORD, IMAP_SERVER , IMAP_PORT , CHECK_INTERVAL, SMTP_PORT, SMTP_SERVER
import imaplib
import email
import asyncio
import base64
from docx import Document
from db import Email2AIRequestLog, SessionLocal, get_db, Email2AI, ApiErrorLog, Whitelist, ActiveSession
import smtplib
from email.message import EmailMessage
from typing import Optional
from auth_utils import authenticate_user, create_access_token, ACCESS_TOKEN_EXPIRE_MINUTES, reset_password, generate_otp, verify_otp, SessionLocal, create_password

router = APIRouter()

PROJECT_NAME = "Email2AI"

email2airedactor = Redactor()

jinja_templates = Jinja2Templates(directory="email2ai/templates")

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
    
@router.post("/upload", response_class=HTMLResponse)
async def upload_document(
    file: UploadFile = File(...),
    prompt: str = Form(...)
):
    try:
        upload_start_time = time.time()
        filename = file.filename

        allowed_extensions = (".pdf", ".docx", ".txt", ".jpg", ".jpeg", ".png")

        if not filename.lower().endswith(allowed_extensions):
            return JSONResponse(
                {"ok": False, "error": "Unsupported file type. Allowed: PDF, DOCX, TXT, JPG, PNG"},
                status_code=400
            )

        file_id = f"{filename}"

        file_bytes = await file.read()
        file_bytes_b64 = base64.b64encode(file_bytes).decode('utf-8')
        # Store session data (Redis / session storage)
        await set_session(file_id, {
            "file_id": file_id,
            "filename": filename,
            "file_bytes_b64": file_bytes_b64,
            "prompt_used": prompt,
            "chat_history": [],
            "processed_at": datetime.utcnow().isoformat(),
            "start_time": upload_start_time        
            })

        return JSONResponse({
            "ok": True,
            "file_id": file_id,
            "filename": filename
        })

    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": str(e)},
            status_code=500
        )

@router.post("/read")
async def read_document(
    file_id: str = Form(...)
):
    try:
        session = await get_session(file_id)
        if session is None:
            return JSONResponse({"ok": False, "error": "Invalid file_id or session expired"}, status_code=404)
        filename = session["filename"]
        file_bytes_b64 = session.get("file_b64")
        if not file_bytes_b64:
            return JSONResponse({"ok": False, "error": "No file bytes found in session"}, status_code=400)

        try:
            file_bytes = base64.b64decode(file_bytes_b64)
        except Exception as e:
            return JSONResponse({"ok": False, "error": "Invalid file data in session"}, status_code=500)

        extract_start = time.time()
        is_pdf = filename.lower().endswith(".pdf")

        if is_pdf:
            pdf_stream = io.BytesIO(file_bytes)
            doc = fitz.open(stream=pdf_stream, filetype="pdf")

            pages = []

            for page in doc:
                text = page.get_text("text")

                if not text.strip() or len(text.strip()) < 50:
                    pix = page.get_pixmap(dpi=300)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    text = pytesseract.image_to_string(img)

                if text.strip():
                    pages.append(text)

            doc.close()

            file_text = "\n\n".join(pages)

        else:
            filename_lower = filename.lower()
            # DOCX
            if filename_lower.endswith(".docx"):
                print("Entered DOCX Block")
                doc_stream = io.BytesIO(file_bytes)
                document = Document(doc_stream)

                paragraphs = [p.text for p in document.paragraphs if p.text.strip()]
                file_text = "\n".join(paragraphs)

            # IMAGE OCR
            elif filename_lower.endswith((".jpg", ".jpeg", ".png")):
                image_stream = io.BytesIO(file_bytes)
                image = Image.open(image_stream)

                file_text = pytesseract.image_to_string(image)
            else:
                try:
                    file_text = file_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    file_text = file_bytes.decode("utf-8", errors="ignore")
     
        extract_time = time.time() - extract_start
        session["file_text"] = file_text
        session["extract_time_s"] = extract_time

        await set_session(file_id, session)


        return JSONResponse(
            {
                "ok": True,
                "file_id": file_id,
                "filename": filename,
                "extract_time": round(extract_time, 4),
                "chars": len(file_text),
            }
        )

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/redact")
async def redact_document(
    file_id: str = Form(...)
):
    try:
        session = await get_session(file_id)
        if session is None:
            return JSONResponse({"ok": False, "error": "Invalid file_id or session expired"}, status_code=404)
        filename = session["filename"]
        file_text = session.get("file_text")

        if not file_text:
            return JSONResponse({"ok": False, "error": "No extracted text. Call /read first."}, status_code=400)

        redaction_start = time.time()
        redacted_info = "WITH Redaction : AUTOMATIC REDACTION"
        redaction_mode = "AUTOMATIC"
        text = file_text

        try:
            result = email2airedactor.redact(
                file_text,
                filename
            )
            text = result["text"]
            redacted_info = "WITH Redaction : AUTOMATIC REDACTION"
        except Exception as e:
            text = file_text
            redacted_info = "REDACTION FAILED"
    
        redaction_time = time.time() - redaction_start

        session["text"] = text 
        session["redaction_mode"] = redaction_mode
        session["redacted_info"] = redacted_info
        session["redaction_time_s"] = redaction_time
        await set_session(file_id, session)

        return JSONResponse(
            {
                "ok": True,
                "file_id": file_id,
                "redaction_mode": redaction_mode,
                "redacted_info": redacted_info,
                "redaction_time": round(redaction_time, 4),
                "chars": len(text),
            }
        )

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@router.post("/vectors/upsert")
async def vectors_upsert_api(
    file_id: str
):
    try:
        session = await get_session(file_id)
        if session is None:
            return JSONResponse({"ok": False, "error": "Invalid file_id or session expired"}, status_code=404)
        filename = session["filename"]
        text = session.get("text")
        if not text:
            return JSONResponse({"ok": False, "error": "No redacted text. Call /redact first."}, status_code=400)

        chunks_upsert_start = time.time()

        splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)
        rag_chunks = splitter.split_text(text) 

        upsert_chunks(
            chunks=rag_chunks,
            file_id=file_id,
            filename=filename,
        ) 

        chunks_upsert_time = time.time() - chunks_upsert_start

        session["rag_chunk_count"] = len(rag_chunks)
        session["chunks_upsert_time_s"] = chunks_upsert_time
        await set_session(file_id, session)

        return JSONResponse(
            {
                "ok": True,
                "file_id": file_id,
                "chunks": len(rag_chunks),
                "upsert_time": round(chunks_upsert_time, 4),
            }
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/vectors/retrieve")
async def vectors_retrieve_api(
    file_id: str,
    top_k: int = 10,
    updated_prompt: str | None = None):
    try:
        session = await get_session(file_id)
        if session is None:
            return JSONResponse({"ok": False, "error": "Invalid file_id or session expired"}, status_code=404)
        filename = session["filename"]
        if updated_prompt is not None and updated_prompt.strip() != "":
            resolved_prompt = updated_prompt.strip()
            session["prompt_used"] = "custom"
            session["resolved_prompt"] = resolved_prompt 
        else:
            resolved_prompt = session.get("resolved_prompt")
            if not resolved_prompt:
                prompt = session.get("prompt_used")
                resolved_prompt = PROMPT_TEMPLATES.get(
                    prompt,
                    PROMPT_TEMPLATES["Enhanced Medical Records Summary"]
                )

            session["resolved_prompt"] = resolved_prompt

        retrieved = []
        context = ""
        attempts = 0
        chunks_retrieve_start = time.time()

        while attempts < 3:
            attempts += 1

            retrieved = query_chunks(
                query=resolved_prompt,
                filename=filename,
                file_id=file_id,
                top_k=top_k,
            )

            chunks_retrieve_time = time.time() - chunks_retrieve_start

            if retrieved:
                context = "\n\n".join(retrieved)
                break

        if not retrieved:
            context = session.get("text", "")

            session["context"] = context
            await set_session(file_id, session)

            return JSONResponse(
                {
                    "ok": True,
                    "retrieved_chunks": 0,
                    "fallback": "full_document"
                }
            )
        session["retrieved"] = retrieved
        session["context"] = context

        session["chunks_retrieve_time_s"] = chunks_retrieve_time
        await set_session(file_id, session)

        return JSONResponse(
            {
                "ok": True,
                "file_id": file_id,
                "retrieved_chunks": len(retrieved),
                "retrieve_time": round(chunks_retrieve_time, 4),
                "context_chars": len(context),
            }
        )

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/llm/generate", response_class=HTMLResponse)
async def llm_generate_api(
    file_id: str
):
    try:
        session = await get_session(file_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Invalid file_id or file session expired. Reupload the document.")
        filename = session["filename"]

        resolved_prompt = session.get("resolved_prompt") or PROMPT_TEMPLATES.get(
            session.get("prompt_used"),
            PROMPT_TEMPLATES["Enhanced Medical Records Summary"]
        )

        is_image = session.get("is_image", False)
        image_b64 = session.get("image_b64")
        context = session.get("context")

        if is_image:
            if not image_b64 or len(image_b64) < 300:
                raise HTTPException(400, "Image mode but no valid image data")
            context_chunks = [context]
        else:
            if session.get("prompt_used") == "custom" and not filename:
                context_chunks = " None "
            else:
                if not context:
                    context = session.get("text")
                    if not context:
                        raise HTTPException(
                            404,
                            "No document text available"
                        )
                context_chunks = [context]

        # Run initial LLM
        summary_result, usage_dict = await run_staged_summary(context_chunks, resolved_prompt, filename, image_b64)
        session["summary"] = summary_result
        session["usage_dict"] = usage_dict
        await set_session(file_id, session)

        return HTMLResponse(
            content="<h3>Initial summary generated successfully</h3>",
            status_code=200
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "error": str(e),
                "endpoint": "/llm/generate",
                "type": type(e).__name__
            }
        )

@router.post("/summary/send-email")
async def send_summary_email(file_id: str):
    try:
        session = await get_session(file_id)

        if session is None:
            raise HTTPException(
                status_code=404,
                detail="Invalid file session"
            )

        final_summary = session.get("summary")

        if not final_summary:
            raise HTTPException(
                status_code=404,
                detail="No summary found. Run generation first."
            )

        filename = session["filename"]
        start_time = session.get("start_time")

        total_processing_time = time.time() - start_time

        usage_dict = session.get("usage_dict", {})

        filename_blob, file_bytes = await create_summary_docx(final_summary, file_id)

        if await blob_exists(filename_blob):
           await delete_blob(filename_blob)

        await upload_blob(file_bytes, filename_blob) 

        download_url = await generate_blob_sas_url(filename_blob)

        filename = session.get("filename")

        if filename:
            attachment_type = "." + filename.split(".")[-1].lower()
        else:
            attachment_type = "custom"  

        attachment_size = round(
            len(file_bytes) / (1024 * 1024), 2
        )

        prompt_tokens = usage_dict.get("prompt_tokens", 0)
        completion_tokens = usage_dict.get("completion_tokens", 0)
        input_cost = usage_dict.get("input_cost", 0.0) 
        output_cost = usage_dict.get("output_cost", 0.0)

        print(f"Total processing time: {total_processing_time}")

        selected_prompt = session.get("prompt_used")
        resolved_prompt = session.get("resolved_prompt")

        if selected_prompt == "custom":
            prompt_message = f"""
                Custom Prompt Used:

                {resolved_prompt}
                """
        else:
            prompt_message = f"""
                Prompt Template Used:

                {resolved_prompt}
                """

        email_body = f"""
            Response Generated

            Your Given Prompt Details
            -----------------------
            {prompt_message}

            Download the generated DOCX summary using the secure link below.
            """

        user_email = session.get("user_email")
        subject = session.get("subject")

        await send_email_with_attachment(
            to_email=user_email,
            subject=subject,
            body=email_body,
            download_url=download_url

        )
        try:
            db = SessionLocal()

            log = Email2AIRequestLog(
                sender_email=user_email,
                subject=subject,
                prompt_text=resolved_prompt,
                attachment_name=filename if filename else "custom_text",
                attachment_type=attachment_type,
                attachment_size=attachment_size,
                input_tokens = prompt_tokens,
                output_tokens = completion_tokens,
                input_cost_usd = input_cost, 
                output_cost_usd = output_cost,
                response_time=total_processing_time,
                status="success"
            )

            db.add(log)
            db.commit()
            db.close()

        except Exception as db_error:
            return{"Logging failed:", str(db_error)}

        return {
            "status": "email_sent",
            "file_id": file_id,
            "download_url": download_url
        }

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail={
                "error": str(e),
                "endpoint": "/summary/send-email",
                "type": type(e).__name__
            }
        )
    
@router.post("/register")
async def register_user(
    name: str,
    birthdate_code: str,
    primary_email: str,
    secondary_email: str | None = None,
    db: Session = Depends(get_db)
):

    if len(birthdate_code) != 8:
        raise HTTPException(status_code=400, detail="Birthdate must be MMDDYYYY")

    user = Email2AI(
        name=name,
        birthdate_code=birthdate_code,
        primary_email=primary_email,
        secondary_email=secondary_email
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    return {
        "message": "User registered",
        "Birthdate code": birthdate_code,
        "user_id": user.id
    }

@router.get("/users")
async def get_all_users(db: Session = Depends(get_db)):

    users = db.query(Email2AI).all()
    result = []
    for user in users:
        result.append({
            "id": user.id,
            "name": user.name,
            "birthdate_code": user.birthdate_code,
            "primary_email": user.primary_email,
            "secondary_email": user.secondary_email,
            "created_at": user.created_at
        })

    return {
        "total_users": len(result),
        "users": result
    } 

@admin_router.get("/email2ai/prompt_logs")
async def get_email_logs(db: Session = Depends(get_db)):

    logs = db.query(Email2AIRequestLog).order_by(
        Email2AIRequestLog.created_at.desc()
    ).all()

    data = []

    for log in logs:
        data.append({
            "id": log.id,
            "sender_email": log.sender_email,
            "subject": log.subject,
            "prompt_text": log.prompt_text,
            "attachment_name": log.attachment_name,
            "attachment_type": log.attachment_type,
            "input_tokens": log.input_tokens,
            "output_tokens": log.output_tokens,
            "input_cost_usd": log.input_cost_usd,
            "output_cost_usd": log.output_cost_usd,
            "response_time": log.response_time,
            "status": log.status,
            "created_at": log.created_at
        })

    return {
        "total": len(data),
        "data": data
    } 

@admin_router.get("/email2ai/error-logs")
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

# process incoming emails
async def process_email(msg):

    subject = await decode_mime_words(msg["Subject"])
    from_email = email.utils.parseaddr(msg.get("From"))[1]

    try:
        subject = await decode_mime_words(msg["Subject"])
        parts = subject.split()

        if len(parts) == 0:
            print("Invalid subject format")
            return
        
        birthdate_code = parts[0]

        if not birthdate_code.isdigit() or len(birthdate_code) != 8:
            print("Invalid birthdate code format")
            return

        subject_without_code = subject.replace(birthdate_code, "").strip()
        from_email = email.utils.parseaddr(msg.get("From"))[1]
        user = await authenticate_email(from_email, birthdate_code)

        if not user:
            return        
        subject_lower = subject_without_code.lower()

        prompt_template_name = "Enhanced Medical Records Summary"

        for key in PROMPT_TEMPLATES:
            words = key.lower().split()

            for word in words:
                if word in subject_lower:
                    prompt_template_name = key
                    break
            else:
                continue
            break

        # Extract email body
        body_text = ""

        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body_text = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8"
                    )
                    break
        else:
            body_text = msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8"
            )

        if "custom" in subject_without_code.lower():
            print("Custom prompt detected from subject")
            custom_prompt = body_text.strip()
            if "{document_text}" not in custom_prompt:
                custom_prompt += "\n\nDOCUMENT TEXT:\n{document_text}"
            session_prompt_name = "custom"
            resolved_prompt = custom_prompt
        else:
            session_prompt_name = prompt_template_name
            resolved_prompt = PROMPT_TEMPLATES.get(
                prompt_template_name,
                PROMPT_TEMPLATES["Enhanced Medical Records Summary"]
            )

        filename, file_bytes = await save_attachment(msg)
        
        if not file_bytes:
            if session_prompt_name == "custom":
                print("No attachment found → using email body directly")
                filename = None
                file_bytes = None 
            else:
                print("No attachment found, skipping email.")
                return
            
        # Unique file_id to avoid conflicts
        
        safe_filename = re.sub(r'[^a-zA-Z0-9_.-]', '_', filename) if filename else "custom_email" 
        file_id = f"{int(time.time())}_{safe_filename}"

        # Save session
        await set_session(file_id, {
            "file_id": file_id,
            "filename": filename,
            "file_b64": base64.b64encode(file_bytes).decode("utf-8") if file_bytes else None,
            "prompt_used": session_prompt_name,
            "resolved_prompt": resolved_prompt,
            "chat_history": [],
            "processed_at": time.time(),
            "user_email": from_email,
            "subject": subject_without_code,
            "birthdate_code": birthdate_code,
            "start_time": time.time()
        })
        
        filename_lower = filename.lower() if filename else ""

        if (session_prompt_name == "custom" and not file_bytes) or filename_lower.endswith((".jpg", ".jpeg", ".png")):
            if session_prompt_name == "custom" and not file_bytes:
                print("Custom prompt detected without attachment → Direct LLM")
            elif filename_lower.endswith((".jpg", ".jpeg", ".png")):
                print("Image detected → Direct LLM processing")
                session = await get_session(file_id)
                image_b64 = session["file_b64"]

                session["image_b64"] = image_b64
                session["context"] = ""
                session["is_image"] = True

                await set_session(file_id, session)

            await llm_generate_api(file_id=file_id)
            await send_summary_email(file_id=file_id)
            return
        else:
            await read_document(file_id=file_id)
            await redact_document(file_id=file_id)
            await vectors_upsert_api(file_id=file_id)
            await vectors_retrieve_api(file_id=file_id, top_k=10)
            await llm_generate_api(file_id=file_id)
            await send_summary_email(file_id=file_id)    
    except Exception as e:
        try:
            await send_failure_email(
                to_email=from_email,
                subject=subject,   # same subject user sent
                reason=str(e)
            )
        except Exception as mail_error:
            return{"Could not send failure email:", mail_error}

async def check_mailbox():
    while True:
        try:
            mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            mail.login(SMTP_USERNAME, SMTP_PASSWORD)
            mail.select("inbox")
            status, messages = mail.search(None, '(UNSEEN)')  # Only unseen emails
            mail_ids = messages[0].split()
            print(f"Found {len(mail_ids)} new emails.")

            for mail_id in mail_ids:
                status, data = mail.fetch(mail_id, "(RFC822)")
                raw_email = data[0][1]
                msg = email.message_from_bytes(raw_email)
                await process_email(msg)
                mail.store(mail_id, '+FLAGS', '\\Seen')

            mail.logout()
        except Exception as e:
            print(f"Error in mailbox check: {e}")

        await asyncio.sleep(CHECK_INTERVAL)


async def send_failure_email(to_email, subject, reason):

    msg = EmailMessage()
    msg["Subject"] = subject   # same subject user sent
    msg["From"] = SMTP_USERNAME
    msg["To"] = to_email

    msg.set_content(f"""
    Your Email2AI request could not be processed.
                    
    Reason for failure:
    {reason}

    Please review your request and try again.
    """)

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)

        print("Failure email sent")

    except Exception as e:
        print("Failed to send failure email:", e) 