from fastapi import APIRouter, Depends, UploadFile, File, Depends, Form, HTTPException, Request
from ramoslaw_chatgpt.general_chatbot import generate_response, blocked_prompt, scan_file_for_phi, phi_block_response
from ramoslaw_chatgpt.config import  OPENAI_GENERAL_CHAT_MODEL, SECURE_CODE
from db import get_db, log_prompt_and_update_activity, Session, ActiveSession, Whitelist
from fastapi.responses import JSONResponse
from auth_utils import authenticate_user, create_access_token, get_current_user, ACCESS_TOKEN_EXPIRE_MINUTES, generate_otp, verify_otp, SessionLocal, create_password, reset_password
from fastapi.security import OAuth2PasswordRequestForm
from datetime import timedelta, datetime
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func

router = APIRouter()
PROJECT_NAME = "Ramoslaw_ChatGPT"

GENERALCHATBOT_MODEL_PRICING = {
    OPENAI_GENERAL_CHAT_MODEL: {
        "input_per_million": 2.50,
        "output_per_million": 15.00
    },
    "gpt-5.4": {
        "input_per_million": 2.50, # 5.4-mini : 0.75
        "output_per_million": 15.00 # 5.4-mini : 4.50
    }
}

async def calculate_generalchatbot_cost(model: str, input_tokens: int, output_tokens: int):
    pricing = GENERALCHATBOT_MODEL_PRICING.get(model)

    if not pricing:
        raise ValueError(f"Pricing not defined for model: {model}")

    input_cost = (input_tokens / 1_000_000) * pricing["input_per_million"]
    output_cost = (output_tokens / 1_000_000) * pricing["output_per_million"]

    return {
        "input_cost": round(input_cost, 6),
        "output_cost": round(output_cost, 6)
    } 


jinja_templates = Jinja2Templates(directory="ramoslaw_chatgpt/templates") 

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

@router.get("/active-today")
async def active_today(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    today = datetime.utcnow().date()
    active = db.query(ActiveSession.user_email, ActiveSession.last_activity_timestamp).filter(
        ActiveSession.project_name == PROJECT_NAME,
        func.date(ActiveSession.last_activity_timestamp) == today
    ).all()
    return [{"email": email, "last_active": ts.isoformat()} for email, ts in active]


@router.post("/general/chat")
async def chat(
    message: str = Form(default=""),
    previous_response_id: str = Form(default=""),
    files: list[UploadFile] = File(default=[]),
    security_code: str = Form(default=""),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not message.strip() and not files:
        return JSONResponse(
            status_code=400,
            content={"error": "Provide at least a message or one file."},
        )

    model_to_use = OPENAI_GENERAL_CHAT_MODEL
    # if security_code and security_code == SECURE_CODE:
    #     model_to_use = "gpt-5.4"

    if security_code:
        if security_code != SECURE_CODE:
            return JSONResponse(
                status_code=400,
                content={"error": "Wrong security code."}
            )
        model_to_use = "gpt-5.4"

    if message and await blocked_prompt(message):
        print("Blocked prompt detected")
        return await phi_block_response()

    for file in files:
        if await scan_file_for_phi(file):
            print(f"PHI detected in file: {file.filename}")
            return await phi_block_response()

    file_names = [f.filename for f in files if f.filename]
    prompt_text = message.strip()

    if file_names:
        file_text = "\n".join(file_names)
        prompt_text = f"{prompt_text}\n\nFiles:\n{file_text}"

    result = await generate_response(
        message=message,
        files=files,
        previous_response_id=previous_response_id.strip() or None,
        model=model_to_use
    )

    # Calculate cost
    cost_data = await calculate_generalchatbot_cost(
    model=model_to_use,
    input_tokens=result["token_info"]["input_tokens"],
    output_tokens=result["token_info"]["output_tokens"]
    )

    input_cost = cost_data["input_cost"]
    output_cost = cost_data["output_cost"]

    # Log prompt
    prompt_log = await log_prompt_and_update_activity(
        db=db,
        project_name=PROJECT_NAME,
        user_email=current_user["email"],
        prompt_text=prompt_text,
        prompt_type="Playbook Chatbot General Chat",
        model=model_to_use,
        input_tokens=result["token_info"]["input_tokens"],
        output_tokens=result["token_info"]["output_tokens"],
        input_cost_usd=input_cost,
        output_cost_usd=output_cost
    )

    return {
        "answer": result["answer"],
        "response_id": result["response_id"],
        "prompt_log_id": prompt_log.id,
        "token_info": result["token_info"]
    }