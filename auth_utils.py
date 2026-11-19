from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from passlib.context import CryptContext
from datetime import datetime, timedelta 
from fastapi import  HTTPException, Depends
from db import SessionLocal, Whitelist, OTPVerification
from dotenv import load_dotenv
import os 
import random
import httpx
load_dotenv()
from MVA_Platform.outlook_utils import get_access_token

TENANT_ID = os.getenv("Tenant_ID")
CLIENT_ID = os.getenv("Application_ID")
CLIENT_SECRET = os.getenv("Secret_Value")
USER_EMAIL = os.getenv("MAILBOX_USER")

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPE = ["https://graph.microsoft.com/.default"]

ACCESS_TOKEN = None

async def send_otp_email(
    to_email: str,
    otp: str
):

    global ACCESS_TOKEN

    if not ACCESS_TOKEN:
        ACCESS_TOKEN = get_access_token()

    url = (
        f"https://graph.microsoft.com/v1.0/"
        f"users/{USER_EMAIL}/sendMail"
    )

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    body = {
        "message": {
            "subject": "Verification Code for Secure Lxintel Access",
            "body": {
                "contentType": "html",
                "content": f"""
                <div style="font-family: Arial;">
                    <h2>Verification Code</h2>
                    <p>Your verification code is:</p>
                    <h1>{otp}</h1>
                    <p>This code expires in 10 minutes.</p>
                </div> 
                """
            },
            "toRecipients": [
                {
                    "emailAddress": {
                        "address": to_email
                    }
                }
            ]
        }
    }

    async with httpx.AsyncClient(
        timeout=30
    ) as client:

        response = await client.post(
            url,
            headers=headers,
            json=body
        )

    # TOKEN EXPIRED
    if response.status_code == 401:
        ACCESS_TOKEN = get_access_token()
        headers["Authorization"] = (
            f"Bearer {ACCESS_TOKEN}"
        )

        async with httpx.AsyncClient(
            timeout=30
        ) as client:

            response = await client.post(
                url,
                headers=headers,
                json=body
            )

    if response.status_code != 202:
        raise Exception(
            f"Graph Email Error: {response.text}"
        )

    return True


async def generate_otp(email: str):
    db = SessionLocal()
    try:
        if not await is_authorized(email):
            raise HTTPException(
                status_code=404,
                detail="Email not authorized"
            )
        
        otp = str(random.randint(100000, 999999))

        await send_otp_email(
            email,
            otp
        )

        # DELETE OLD OTP
        db.query(OTPVerification).filter(
            OTPVerification.email == email.lower()
        ).delete()

        otp_record = OTPVerification(
            email=email.lower(),
            otp=otp,
            expires_at=datetime.utcnow() + timedelta(minutes=10)
        )
        db.add(otp_record)
        db.commit()


        return {
            "message": "OTP sent successfully"
        }

    finally:
        db.close()


async def verify_otp(
    email: str,
    otp: str
):
    db = SessionLocal()
    try:
        otp_record = db.query(
            OTPVerification
        ).filter(
            OTPVerification.email == email.lower()
        ).first()

        if not otp_record:
            raise HTTPException(
                status_code=400,
                detail="OTP not found"
            )

        if otp_record.otp != otp:
            raise HTTPException(
                status_code=400,
                detail="Invalid OTP"
            )

        if datetime.utcnow() > otp_record.expires_at:
            raise HTTPException(
                status_code=400,
                detail="OTP expired"
            )

        # db.delete(otp_record)
        # db.commit()

        return True

    finally:
        db.close()

async def create_password(
    email: str,
    password: str
):
    db = SessionLocal()
    try:
        user = db.query(Whitelist).filter(
            Whitelist.email == email.lower()
        ).first()

        if not user:
            raise HTTPException(
                status_code=404,
                detail="User not found"
            )

        if user.password_hash:
            raise HTTPException(
                status_code=400,
                detail="Password already exists"
            )

        validate_password(password)
        hashed_password = pwd_context.hash(password)
        user.password_hash = hashed_password

        db.query(OTPVerification).filter(
            OTPVerification.email == email.lower()
        ).delete()

        db.commit()

        return {
            "message": "Password created"
        }

    finally:
        db.close()

async def reset_password(
    email: str,
    otp: str,
    new_password: str
):
    db = SessionLocal()
    try:
        otp_record = db.query(
            OTPVerification
        ).filter(
            OTPVerification.email == email.lower()
        ).first()

        if not otp_record:
            raise HTTPException(
                status_code=400,
                detail="OTP not found"
            )

        if otp_record.otp != otp:
            raise HTTPException(
                status_code=400,
                detail="Invalid OTP"
            )

        if datetime.utcnow() > otp_record.expires_at:
            raise HTTPException(
                status_code=400,
                detail="OTP expired"
            )

        user = db.query(Whitelist).filter(Whitelist.email == email.lower()).first()

        if not user:
            raise HTTPException(
                status_code=404,
                detail="User not found"
            )
        validate_password(new_password)
        user.password_hash = pwd_context.hash(new_password)
        db.delete(otp_record)
        db.commit()

        return {
            "message": "Password reset successful"
        }

    finally:
        db.close()

async def load_whitelist():
    db = SessionLocal()
    try:
        records = db.query(Whitelist).filter(
            Whitelist.role == "user"
        ).all()

        return [r.email.lower() for r in records]

    finally:
        db.close()

# === Authorization and JWT Auth Functions ===
async def is_authorized(email: str) -> bool:
    return email.lower() in await load_whitelist()

SECRET_KEY = "supersecretkey123"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES"))    
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")

async def create_access_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def authenticate_user(
    email: str,
    password: str
):

    db = SessionLocal()

    try:

        user = db.query(Whitelist).filter(
            Whitelist.email == email.lower(),
            Whitelist.role == "user"
        ).first()

        if not user:
            return None

        if not user.password_hash:
            raise HTTPException(
                status_code=400,
                detail="Please create password first"
            )

        valid_password = pwd_context.verify(
            password,
            user.password_hash
        )

        if not valid_password:
            return None

        return {
            "email": user.email,
            "role": user.role
        }

    finally:
        db.close()

def validate_password(password: str):

    if len(password) < 8:
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 8 characters"
        )

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(status_code=401, detail="Unauthorized")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None or not await is_authorized(email):
            raise credentials_exception
        return {"email": email}
    except JWTError:
        raise credentials_exception 
    
# Editor JWT Auth Functions
editor_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/editor/login")
async def get_current_editor(token: str = Depends(editor_oauth2_scheme)):
    try:
        payload = jwt.decode(token, EDITOR_SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("role") != "editor":
            raise HTTPException(status_code=403, detail="Editor only")
        return payload

    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid editor token")

async def authenticate_editor(
    email: str,
    password: str
):
    db = SessionLocal()
    try:
        user = db.query(Whitelist).filter(
            Whitelist.email == email.lower(),
            Whitelist.role == "editor"
        ).first()

        if not user:
            raise HTTPException(
                status_code=401,
                detail="Editor's Panel access is denied for this account!"
            )

        user_record = db.query(Whitelist).filter(
            Whitelist.email == email.lower(),
            Whitelist.role == "user"
        ).first()

        if not user_record:
            raise HTTPException(
                status_code=400,
                detail="User account not found, please create user account first!"
            )

        if not user_record.password_hash:
            raise HTTPException(
                status_code=400,
                detail="Please create password first on user account!"
            )

        valid_password = pwd_context.verify(
            password,
            user_record.password_hash
        )

        if not valid_password:
            raise HTTPException(
                status_code=400,
                detail="Incorrect User Password!"
            )

        return {
            "name": user.name,
            "email": user.email,
            "role": "editor"
        }

    finally:
        db.close()

EDITOR_SECRET_KEY = "EDITOR_SECRET_KEY"
ALGORITHM = "HS256"
EDITOR_ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

async def create_editor_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    expire =  datetime.utcnow() + (
        expires_delta or timedelta(
            minutes=EDITOR_ACCESS_TOKEN_EXPIRE_MINUTES
        )
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, EDITOR_SECRET_KEY, algorithm=ALGORITHM)

# endpoints for creating lead tokens
async def create_lead_token(lead_id: int, user_email: str):
    payload = {
        "lead_id": lead_id,
        "email": user_email,
        "exp": datetime.utcnow() + timedelta(hours=48)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


async def verify_lead_token(token: str):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except:
        return None