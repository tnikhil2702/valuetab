from __future__ import annotations

import logging
import os

import httpx

from PeakTrac.neos_client import BASE_URL_DEFAULT, NeosAuthError, NeosClient

try:  # the existing app already does this; harmless if repeated
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("peaktrac.neos")
_client: NeosClient | None = None
_warned_shared_login = False


def _credentials() -> dict:
    global _warned_shared_login
    user = os.getenv("NEOS_PT_USER")
    password = os.getenv("NEOS_PT_PASSWORD")
    if not (user and password):
        user, password = os.getenv("NEOS_USER"), os.getenv("NEOS_PASSWORD")
        if user and password and not _warned_shared_login:
            log.warning("PeakTrac is using the shared NEOS_USER login (DEV ONLY, write-capable). "
                        "Set NEOS_PT_USER / NEOS_PT_PASSWORD to the read-only service account.")
            _warned_shared_login = True
    firm = os.getenv("NEOS_PT_FIRM_ID") or os.getenv("NEOS_FIRM_ID")
    if not (firm and user and password):
        raise NeosAuthError(
            "Neos credentials not configured (NEOS_PT_FIRM_ID / NEOS_PT_USER / NEOS_PT_PASSWORD)."
        )
    return {"FirmId": firm, "Username": user, "Password": password,
            "ExpirationTime": int(os.getenv("NEOS_TOKEN_EXPIRATION_SECONDS", "43200"))}


async def login(transport: httpx.AsyncBaseTransport | None = None) -> str:
    """POST /Login -> AccessToken. Errors never include the password or response body."""
    creds = _credentials()
    try:
        async with httpx.AsyncClient(base_url=BASE_URL_DEFAULT, timeout=20, transport=transport) as http:
            resp = await http.post("/Login", json=creds)
    except httpx.HTTPError as e:
        raise NeosAuthError(f"Neos login failed ({type(e).__name__}).") from None
    if resp.status_code >= 400:
        raise NeosAuthError(f"Neos login failed (status={resp.status_code}).")
    token = (resp.json() or {}).get("AccessToken")
    if not token:
        raise NeosAuthError("Neos login response had no AccessToken.")
    log.info("neos POST /Login status=%s", resp.status_code)
    return token


async def _provider(force_refresh: bool) -> str:
    static = os.getenv("NEOS_ACCESS_TOKEN")           # optional short-lived test token
    if static and not os.getenv("NEOS_PT_USER") and not os.getenv("NEOS_USER"):
        return static
    return await login()


def get_neos_client() -> NeosClient:
    global _client
    if _client is None:
        _client = NeosClient(token_provider=_provider)
    return _client