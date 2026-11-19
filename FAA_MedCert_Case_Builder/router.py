import base64
from datetime import datetime, time, timedelta
from typing import Optional
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func
from FAA_MedCert_Case_Builder.services.ingestion_pipeline import case_generate, upload_and_extract, store_session
from FAA_MedCert_Case_Builder.services.parser_service import extract_text
from FAA_MedCert_Case_Builder.services.chunk_service import chunk_text
from FAA_MedCert_Case_Builder.services.embed_service import generate_embedding
from FAA_MedCert_Case_Builder.services.index_service import index_chunks
from FAA_MedCert_Case_Builder.services.blob_search import upload_blob
from auth_utils import authenticate_user, create_access_token, get_current_user, is_authorized, ACCESS_TOKEN_EXPIRE_MINUTES, reset_password, generate_otp, verify_otp, SessionLocal, create_password
from db import ApiErrorLog, MedCertFiles, MedCertRuns, get_db ,Session, ActiveSession, Session, Whitelist
from fastapi.templating import Jinja2Templates
from admin.router import admin_router
from session_redis import get_session, set_session
from redaction import Redactor
import time
from datetime import datetime, timedelta


router = APIRouter()
PROJECT_NAME = "FAA_MedCert_Case_Builder"
jinja_templates = Jinja2Templates(directory="FAA_MedCert_Case_Builder/templates")

medcertRedactor = Redactor()

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

@router.post("/zip/upload")
async def upload_zip(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    start = time.time()
    session_id = str(uuid.uuid4())

    try:
        zip_filename, extracted_files = await upload_and_extract(file)

        db.add(MedCertFiles(
            zip_filename=zip_filename,
            session_id=session_id,
            user_email=current_user["email"]
        ))
        db.commit()

        await store_session(
            session_id,
            current_user["email"],
            zip_filename,
            extracted_files=extracted_files,
            status="uploaded"
        )
        
        completion_time = time.time() - start 
        print(f"Upload and extraction completed in {round(completion_time, 2)} seconds")        
        return {
            "session_id": session_id,
            "message": "Uploaded & extracted"
        }

    except Exception as e:
        print("Upload error:", e)
        return {"error": str(e)}
    
@router.post("/zip/process")
async def process_endpoint(
    session_id: str = Form(...),
    current_user: dict = Depends(get_current_user)
):
    start = time.time()
    try: 
        session = await get_session(session_id)

        if not session:
            return {"error": "Session not found"}

        extracted_files = session.get("extracted_files")

        if not extracted_files:
            return {"error": "No extracted files found"}

        processed_data = []

        for item in extracted_files:
            try:
                file_bytes = base64.b64decode(item["bytes"])
                text = extract_text(file_bytes, item["filename"])

                processed_data.append({
                    "document_path": item["document_path"],
                    "text": text   
                })

            except Exception as e:
                print(f"Processing error {item['filename']}: {e}")
        
        session["processed_data"] = processed_data
        session["status"] = "processed"

        await set_session(session_id, session) 

        completion_time = time.time() - start 
        print(f"process to extract the text from the file in {round(completion_time, 2)} seconds")

        return {
            "session_id": session_id,
            "processed_docs": len(processed_data)
        }

    except Exception as e:
        print("Process error:", e)
        return {"error": str(e)}
    
@router.post("/zip/redact")
async def redact_endpoint(
    session_id: str = Form(...),
    current_user: dict = Depends(get_current_user)
): 
    start = time.time()

    session = await get_session(session_id)

    if not session:
        return {"error": "Session not found"}

    processed_data = session.get("processed_data")

    if not processed_data:
        return {"error": "No processed data found"}

    redacted_output = []

    for item in processed_data:
        try: 
            result = medcertRedactor.redact(
                text=item["text"],
                filename=item.get("filename", "unknown")
            )

            redacted_output.append({
                "filename": item.get("filename"),
                "document_path": item.get("document_path"),
                "redacted_text": result["text"],
                "stats": result["stats"],
                "redactions": result["redacted_items"]
            })

        except Exception as e:
            print(f"Redaction error {item.get('filename')}: {e}")

    session["redacted_data"] = redacted_output
    session["status"] = "redacted"

    await set_session(session_id, session)

    completion_time = time.time() - start 
    print(f"Total time taken to complete redaction in {round(completion_time, 2)} seconds")

    return {
        "session_id": session_id,
        "redacted_files": len(redacted_output),
        "data": redacted_output
    }


@router.post("/zip/index")
async def index_endpoint(
    session_id: str = Form(...),
    current_user: dict = Depends(get_current_user)
):
    start = time.time()
    session = await get_session(session_id)

    if not session:
        return {"error": "Session not found"}

    if session.get("status") != "redacted":
        return {"error": "Please complete redaction first"}

    redacted_data = session.get("redacted_data")

    if not redacted_data:
        return {"error": "No redacted data found"}

    indexed_files = []

    for item in redacted_data:

        try:

            chunks = chunk_text(item["redacted_text"])
            
            await index_chunks(
                chunks=chunks,
                document_path=item["document_path"],
                embedder=generate_embedding,
                session_id=session_id
            ) 
            indexed_files.append(item["document_path"])

        except Exception as e:
            print(f"Indexing error {item['document_path']}: {e}")

    session["status"] = "indexed"
    await set_session(session_id, session)

    completion_time = time.time() - start 
    print(f"Total time taken to chunk the model {round(completion_time, 2)} seconds")

    return {
        "session_id": session_id,
        "indexed_files": indexed_files
    }
    
@router.post("/zip/llm-generate")
async def generate_llm_endpoint(
    session_id: str = Form(...),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return await case_generate(
        session_id=session_id,
        user_email=current_user["email"],
        db=db
    )

@router.post("/upload-guideline")
async def upload_guideline(file: UploadFile = File(...),current_user: dict = Depends(get_current_user)):
    file_bytes = await file.read()
    filename = file.filename

    await upload_blob(file_bytes, filename)      
    text = extract_text(file_bytes, filename)
    chunks = chunk_text(text)

    await index_chunks(
        chunks=chunks,
        document_path=filename,
        embedder=generate_embedding,
        session_id="guideline"
    )


    return {
        "message": "Guideline uploaded and indexed successfully",
        "chunks_created": len(chunks)
    } 

@router.get("/medcert/runhistory")
async def get_user_history(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    Files_data = db.query(MedCertFiles).filter(
        MedCertFiles.user_email == current_user["email"]
    ).order_by(MedCertFiles.id.desc()).all()

    return [
        {
            "id": f.id,
            "zip_filename": f.zip_filename,
            "session_id": f.session_id,
            "user_email": f.user_email,
            "created_at": f.created_at
        }
        for f in Files_data
    ]

@router.get("/medcert/case/{session_id}")
async def get_case_by_session(
    session_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    case = db.query(MedCertRuns).filter(
        MedCertRuns.session_id == session_id,
        MedCertRuns.user_email == current_user["email"]
    ).first()

    if not case:
        raise HTTPException(status_code=404, detail="Case not found or unauthorized")

    return {
        "id": case.id,
        "session_id": case.session_id,
        "user_email": case.user_email,
        "case_header": case.case_header or {},
        "addressee_logic": case.addressee_logic or {},
        "procedural_stage": case.procedural_stage or {},
        "documents": case.documents or {},
        "taxonmy": case.taxonomy or {},
        "issue_stack": case.issue_stack or [],
        "fact_model": case.fact_model or {},
        "timeline": case.timeline or [],
        "missing_items": case.missing_items or {},
        "guideline_checklists": case.guideline_checklists or {},
        "draft_outline": case.draft_outline or {},
        "created_at": case.created_at
    }

@admin_router.get("/FAA_MedCert_Case_Builder/active-today")
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

@admin_router.get("/FAA_MedCert_Case_Builder/error-logs")
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