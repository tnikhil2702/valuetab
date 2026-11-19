import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import json
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
import os
from dotenv import load_dotenv
from db import SessionLocal, GoogleOAuthToken 
from datetime import datetime, timezone

load_dotenv()

class NeedsReAuth(Exception):
    """
    Raised when the stored refresh token is missing or has been revoked.
    GET /auth/google/url  →  POST /auth/google/callback.
    """
    pass

SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/adwords",
]

REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")

config = {
    "web": {
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "project_id": os.getenv("GOOGLE_PROJECT_ID"),
        "auth_uri": os.getenv("GOOGLE_AUTH_URI"),
        "token_uri": os.getenv("GOOGLE_TOKEN_URI"),
        "auth_provider_x509_cert_url": os.getenv("GOOGLE_AUTH_PROVIDER_CERT_URL"),
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
        "redirect_uris": os.getenv("GOOGLE_REDIRECT_URIS", "").split(","),
    }
}

def build_flow() -> Flow:
    return Flow.from_client_config(
        config,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        autogenerate_code_verifier=False,
    )


def get_auth_url() -> str:
    """Return the Google OAuth consent-screen URL (step 1 of re-auth)."""
    flow = build_flow()
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )
    return authorization_url


def exchange_code_for_tokens(code: str) -> str:
    """
    Exchange an authorization code for tokens, persist them, and return
    the new access token (step 2 of re-auth).
    """
    flow = build_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials

    db = SessionLocal()
    try:
        token = db.query(GoogleOAuthToken).first()
        if not token:
            token = GoogleOAuthToken()

        token.access_token  = creds.token
        token.refresh_token = creds.refresh_token
        token.token_url     = creds.token_uri
        token.scopes        = ",".join(creds.scopes or [])
        token.expires_at    = creds.expiry

        db.add(token)
        db.commit()
        db.refresh(token)
    finally:
        db.close()

    return creds.token

def get_valid_access_token() -> str:
    db = SessionLocal()
    try:
        token = db.query(GoogleOAuthToken).first()

        # No record / no refresh token → cannot proceed without re-auth
        if not token or not token.refresh_token:
            raise NeedsReAuth(
                "No Google refresh token found. Please re-authenticate via GET /auth/google/url."
            )

        # Token still valid
        if (
            token.access_token
            and token.expires_at
            and token.expires_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
        ):
            return token.access_token

        # Token expired → try a silent refresh
        creds = Credentials(
            token=token.access_token,
            refresh_token=token.refresh_token,
            token_uri=token.token_url,
            client_id=config["web"]["client_id"],
            client_secret=config["web"]["client_secret"],
            scopes=SCOPES,
        )

        try:
            creds.refresh(Request())

            token.access_token = creds.token
            token.expires_at   = creds.expiry
            if creds.refresh_token:
                token.refresh_token = creds.refresh_token

            db.commit()
            db.refresh(token)
            return token.access_token

        except RefreshError:
            raise NeedsReAuth(
                "Google refresh token has expired or been revoked. "
                "Please re-authenticate via GET /auth/google/url."
            )

    finally:
        db.close()