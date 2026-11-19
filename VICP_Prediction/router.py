from fastapi import APIRouter, Request , HTTPException, Depends
from auth_utils import authenticate_user, create_access_token, get_current_user, is_authorized, ACCESS_TOKEN_EXPIRE_MINUTES, reset_password, generate_otp, verify_otp, SessionLocal, create_password
from .utils import predict_case
from sqlalchemy import func
from fastapi.security import OAuth2PasswordRequestForm
from datetime import datetime, timedelta 
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from db import get_db, Session, ActiveSession, Whitelist

router = APIRouter()

jinja_templates = Jinja2Templates(directory="VICP_Prediction/templates")

PROJECT_NAME="Vicp_Prediction"

# === API Endpoints ===
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


@router.post("/predict")
async def predict_case_outcome(
    injury: str,
    vaccine: str,
    procedure_present: bool,
    stipulation_flag: bool,
    has_expert: bool,
    has_hearing: bool,
    has_motion: bool,
    current_user: dict = Depends(get_current_user)
):

    case_data = {
        "injury": injury,
        "vaccine": vaccine,
        "procedure_present": procedure_present,
        "stipulation_flag": stipulation_flag,
        "has_expert": has_expert,
        "has_hearing": has_hearing,
        "has_motion": has_motion
    }

    result = await predict_case(case_data)

    return result