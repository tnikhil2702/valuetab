import os
from typing import Optional
from fastapi import APIRouter, Form, HTTPException, Request, Depends
from fastapi.security import OAuth2PasswordRequestForm
from datetime import datetime, timedelta 
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from openai import OpenAI
from dotenv import load_dotenv
from sqlalchemy import func
from db import get_db, log_prompt_and_update_activity, Template , Session, ActiveSession ,ApiErrorLog ,PromptLog, Whitelist
from admin.router import admin_router
from auth_utils import authenticate_user, create_access_token, get_current_user, is_authorized, ACCESS_TOKEN_EXPIRE_MINUTES, reset_password, generate_otp, verify_otp, SessionLocal, create_password

load_dotenv()

router = APIRouter()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

PROMPT_ID = os.getenv("VEHICLE_LOSS_ANALYZER_PROMPT_ID")
PROJECT_NAME = "vehicle_loss_analyzer"

class ChatResponse(BaseModel):
    report: str
    response_id: str

jinja_templates = Jinja2Templates(directory="Vehicle_loss_Analyzer/templates")

#------------------- Login ---------------------- # 

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


@router.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return jinja_templates.get_template("index.html").render({
        "request": request
    })

@router.post("/chat", response_model=ChatResponse)
async def Vehicle_loss_analyzer(
    user_message: Optional[str] = Form(None),
    previous_response_id: Optional[str] = Form(None),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # Build content parts (list of dicts)
    content_parts = []

    # Add user text
    content_parts.append({
        "type": "input_text",
        "text": user_message
    })
    
    if len(content_parts) != 1 and not previous_response_id:
        raise HTTPException(
            status_code=400,
            detail="Please enter make, year and model of the car to proceed further."
        )

    try:
        input_items = [
            {
                "role": "user",
                "content": content_parts
            }
        ]

        request_params = {
            "input": input_items,
            "store": True,
            "reasoning": {
                "summary": "concise"
            },
            "tools": [
            {
            "type": "file_search",
            "vector_store_ids": [
                "vs_69693888cc3c8191bb8f0aa1df39184d"
            ]
            }
            ],
            "include": [
                "reasoning.encrypted_content",
                "web_search_call.action.sources"
            ]
        }

        if PROMPT_ID:
            request_params["prompt"] = {
                "id": PROMPT_ID,
            }

        if previous_response_id:
            request_params["previous_response_id"] = previous_response_id

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

        report_parts = []
        
        # Check if output is iterable
        if hasattr(response, 'output') and response.output is not None:
            for item in response.output:
                if hasattr(item, "content") and item.content is not None:
                    for part in item.content:
                        if hasattr(part, "text") and part.text:
                            report_parts.append(part.text)
        else:
            # alternative response structures
            if hasattr(response, 'choices') and response.choices:
                for choice in response.choices:
                    if hasattr(choice, 'message') and hasattr(choice.message, 'content'):
                        report_parts.append(choice.message.content)
            elif hasattr(response, 'content') and response.content:
                report_parts.append(str(response.content))
            else:
                print(f"WARNING: Could not find output in response. Attributes: {dir(response)}")

        report = "\n".join(report_parts) if report_parts else "No detailed report generated."
        new_response_id = getattr(response, 'id', None) or getattr(response, 'response_id', 'unknown')

        await log_prompt_and_update_activity(
            db=db,
            project_name=PROJECT_NAME,
            user_email=current_user["email"],
            prompt_text=user_message,
            prompt_type="Vehicle Loss Analyzer Chat",
            model=response.model,
            input_tokens=usage_dict["input_tokens"],
            output_tokens=usage_dict["output_tokens"],
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=e)

    return {"report": report, "response_id": new_response_id}

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

@admin_router.get("/Vehicle_Loss_Analyzer/active-today")
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

@admin_router.get("/Vehicle_Loss_Analyzer/error-logs")
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

@admin_router.get("/Vehicle_Loss_Analyzer/prompt_logs")
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