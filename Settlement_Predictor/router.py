import os
from fastapi import Request, APIRouter, Depends, HTTPException, Form
from fastapi.security import OAuth2PasswordRequestForm
from datetime import datetime, timedelta 
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from openai import OpenAI
import time
from typing import Optional
from dotenv import load_dotenv
from sqlalchemy import func
from db import get_db, log_prompt_and_update_activity, Template, Session, ActiveSession, ApiErrorLog, PromptLog, Whitelist
from auth_utils import authenticate_user, create_access_token, get_current_user, is_authorized, ACCESS_TOKEN_EXPIRE_MINUTES, reset_password, generate_otp, verify_otp, SessionLocal, create_password
from admin.router import admin_router
load_dotenv()
router = APIRouter()
jinja_templates = Jinja2Templates(directory="Settlement_Predictor/templates")
PROJECT_NAME = "Settlement_Predictor"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)
ASSISTANT_ID = os.getenv("SETTLEMENT_assistant_id")


# Settlement Analysis Optimization Prompt
OPTIMIZE_PROMPT = """
🔥 UNIVERSAL SETTLEMENT OPTIMIZATION PROMPT (Formatted Output Required) Attorney pastes this after any case summary:
OPTIMIZATION REQUEST

Assume you are senior plaintiff trial counsel preparing this case for maximum settlement recovery.

Do NOT restate the case summary.

Provide a structured strategic analysis using the exact formatting icons below.

🟢 REVISED SETTLEMENT VALUE

Provide three valuation scenarios formatted EXACTLY like this:

🟢 💰 $LOW - $HIGH 💰 🟢

Label each as:
- Conservative Resolution
- Optimal Pre-Trial Resolution
- Excess Verdict Exposure

If policy limits restrict recovery, clearly state:

⚠️ POLICY LIMIT CONSTRAINT:
Explain whether limits act as a cap or a leverage tool.

🚀 TOP 5 VALUE MAXIMIZATION STRATEGIES

Ranked in order of expected impact.

For each strategy include:
- Why it increases settlement value
- What action triggers adjuster reserve increases
- Whether it increases bad faith exposure
- Timeline to implement

🔎 LEVERAGE ANALYSIS

Separate into:
- Liability Leverage
- Damages Amplification
- Coverage Expansion Opportunities
- Procedural Pressure
- Adjuster Psychology Factors

Identify hidden value drivers not obvious in the summary.

⚠️ VALUE SUPPRESSION RISKS

Identify vulnerabilities that could decrease settlement value and provide a tactical fix for each.

💰 COVERAGE EXPANSION ANALYSIS

Evaluate likelihood of:
- Umbrella policies
- Employer liability
- Stacked UM/UIM
- Corporate exposure
- Personal asset exposure

Estimate probability tiers:
Low / Moderate / High.

🛑 30-DAY ACTION PLAN

Provide concrete litigation or negotiation steps that should be executed in the next 30 days to maximize recovery.

No generic advice.
Tactical and specific only.
"""

# Api endpoints
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

@router.post("/analyze")
async def analyze_case(request: Request, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Accepts case narrative and sends to the Ramos Law Settlement Intelligence Agent. The assistant handles all extraction, calculation, and strategic analysis.
    """
    try:
        body = await request.json()
        narrative = body.get("narrative")

        if not narrative.strip():
            return {"error": "Case narrative is empty"}

        print("\n" + "="*80)
        print("NEW SETTLEMENT ANALYSIS REQUEST")
        print("="*80)
        print(f"Narrative: {narrative[:200]}..." if len(narrative) > 200 else f"Narrative: {narrative}")
        
        # Create a new thread
        print("\nCreating thread...")
        thread = client.beta.threads.create()
        print(f"✓ Thread ID: {thread.id}")
        
        print("\nSending to Ramos Law Settlement Intelligence Agent...")
        client.beta.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content=narrative
        )

        # Run the assistant
        print("Running analysis...")
        run = client.beta.threads.runs.create(
            thread_id=thread.id,
            assistant_id=ASSISTANT_ID
        )

        # Wait for completion
        response_text = ""
        image_urls = []
        while True:
            status = client.beta.threads.runs.retrieve(
                thread_id=thread.id,
                run_id=run.id
            )
            
            if status.status == "completed":
                if status.usage:
                    input_tokens = status.usage.prompt_tokens
                    output_tokens = status.usage.completion_tokens
                    usage_dict = {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens
                    }
                else:
                    usage_dict = {"input_tokens": 0, "output_tokens": 0}
                messages = client.beta.threads.messages.list(thread_id=thread.id)
                response_parts = []
                for content_block in messages.data[0].content:
                    if content_block.type == 'text':
                        response_parts.append(content_block.text.value)
                    elif content_block.type == 'image_file':
                        file_id = content_block.image_file.file_id
                        if file_id:
                            print(f"Found image file: {file_id}")
                            image_urls.append(file_id)
                            response_parts.append(f"[Image generated - File ID: {file_id}]")
                    elif content_block.type == 'image_url':
                        url = content_block.image_url.url
                        print(f"Found image URL: {url}")
                        image_urls.append(url)
                        response_parts.append(f"[Image: {url}]")
                    else:
                        response_parts.append(f"[{content_block.type} content]")
                
                response_text = "\n".join(response_parts)
                break
            elif status.status in ["failed", "cancelled", "expired"]:
                return {"error": f"Analysis failed with status: {status.status}"}
            
            time.sleep(1.5)

        await log_prompt_and_update_activity(
            db=db,
            project_name=PROJECT_NAME,
            user_email=current_user["email"],
            prompt_text=narrative,
            prompt_type="Settlement Analysis",
            model=run.model,
            input_tokens=usage_dict["input_tokens"],
            output_tokens=usage_dict["output_tokens"],
        )
        return {
            "success": True,
            "thread_id": thread.id,
            "analysis": response_text,
            "image_files": image_urls
        }

    except Exception as e:
        return {"error": str(e)}

@router.post("/optimize")
async def optimize_analysis(
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Optimizes the previous assistant response inside the same thread.
    Uses a static optimization instruction defined in the backend.
    """
    try:
        body = await request.json()
        thread_id = body.get("thread_id")

        if not thread_id:
            return {"error": "Missing thread_id"}

        # Add optimization instruction to existing thread
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=OPTIMIZE_PROMPT
        )

        # Run assistant
        run = client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=ASSISTANT_ID
        )

        # Wait for completion
        while True:
            status = client.beta.threads.runs.retrieve(
                thread_id=thread_id,
                run_id=run.id
            )

            if status.status == "completed":

                if status.usage:
                    usage_dict = {
                        "input_tokens": status.usage.prompt_tokens,
                        "output_tokens": status.usage.completion_tokens
                    }
                else:
                    usage_dict = {"input_tokens": 0, "output_tokens": 0}

                messages = client.beta.threads.messages.list(
                    thread_id=thread_id
                )

                optimized_response = messages.data[0].content[0].text.value

                # Log optimization prompt usage
                await log_prompt_and_update_activity(
                    db=db,
                    project_name=PROJECT_NAME,
                    user_email=current_user["email"],
                    prompt_text=OPTIMIZE_PROMPT,
                    prompt_type="Settlement Optimization",
                    model=run.model,
                    input_tokens=usage_dict["input_tokens"],
                    output_tokens=usage_dict["output_tokens"],
                )

                return {
                    "success": True,
                    "thread_id": thread_id,
                    "optimized_analysis": optimized_response
                }

            elif status.status in ["failed", "cancelled", "expired"]:
                return {"error": f"Optimization failed: {status.status}"}

            time.sleep(1.5)

    except Exception as e:
        return {"error": str(e)}

@router.post("/chat")
async def chat(request: Request, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Follow-up questions on the same thread.
    """
    try:
        body = await request.json()
        thread_id = body.get("thread_id")
        user_message = body.get("message")

        if not thread_id or not user_message:
            return {"error": "Missing thread_id or message"}

        # Add user message to existing thread
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=user_message
        )

        # Run assistant
        run = client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=ASSISTANT_ID
        )

        # Wait for response
        while True:
            status = client.beta.threads.runs.retrieve(
                thread_id=thread_id,
                run_id=run.id
            )
            
            if status.status == "completed":
                if status.usage:
                    input_tokens = status.usage.prompt_tokens
                    output_tokens = status.usage.completion_tokens
                    usage_dict = {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens
                    }
                else:
                    usage_dict = {"input_tokens": 0, "output_tokens": 0}

                messages = client.beta.threads.messages.list(thread_id=thread_id)
                assistant_response = messages.data[0].content[0].text.value

                await log_prompt_and_update_activity(
                db=db,
                project_name=PROJECT_NAME,
                user_email=current_user["email"],
                prompt_text=user_message,
                prompt_type="Settlement Followup Chat",
                model=run.model,
                input_tokens=usage_dict["input_tokens"],
                output_tokens=usage_dict["output_tokens"],
            )
                return {"response": assistant_response}
            elif status.status in ["failed", "cancelled", "expired"]:
                return {"response": "Unable to process your question at this time."}
            
            time.sleep(1.5)

    except Exception as e:
        return {"error": str(e)}


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

@admin_router.get("/Settlement_Predictor/active-today")
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

@admin_router.get("/Settlement_Predictor/error-logs")
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

@admin_router.get("/Settlement_Predictor/prompt_logs")
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