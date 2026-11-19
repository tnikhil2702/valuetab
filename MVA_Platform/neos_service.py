# neos_service.py
import asyncio
import httpx
import os
import urllib.parse
from datetime import datetime, timezone
from db import SessionLocal, get_neos_token, save_neos_token, delete_neos_token
from dotenv import load_dotenv
load_dotenv()

NEOS_BASE_URL    = "https://nc-api.neos-cloud.com"
PRIMARY_STAFF_ID = os.getenv("NEOS_PRIMARY_STAFF_ID")
DEFAULT_CASE_TYPE_ID = os.getenv("NEOS_CASE_TYPE_ID")
NEOS_FIRM_ID  = os.getenv("NEOS_FIRM_ID")
NEOS_USERNAME = os.getenv("NEOS_USER")
NEOS_PASSWORD = os.getenv("NEOS_PASSWORD")
NEOS_TOKEN_EXPIRATION_SECONDS = int(os.getenv("NEOS_TOKEN_EXPIRATION_SECONDS", "43200"))  # 12h

neos_client = httpx.AsyncClient(
    timeout=30,
    limits=httpx.Limits(
        max_keepalive_connections=20,
        max_connections=50
    )
)

# Token management
async def _login() -> str:
    """
    POST /Login with firm credentials, store the returned AccessToken, and
    return it. Raises if credentials are missing or the login call fails.
    """
    if not (NEOS_FIRM_ID and NEOS_USERNAME and NEOS_PASSWORD):
        raise RuntimeError(
            "Missing NEOS_FIRM_ID / NEOS_USERNAME / NEOS_PASSWORD env vars; "
            "cannot auto-login to Neos."
        )

    resp = await neos_client.post(
        f"{NEOS_BASE_URL}/Login",
        headers={"Content-Type": "application/json"},
        json={
            "FirmId": NEOS_FIRM_ID,
            "Username": NEOS_USERNAME,
            "Password": NEOS_PASSWORD,
            "ExpirationTime": NEOS_TOKEN_EXPIRATION_SECONDS,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("AccessToken")
    if not token:
        raise RuntimeError(f"Neos /Login response missing AccessToken: {data}")

    db = SessionLocal()
    try:
        save_neos_token(db, token)
    finally:
        db.close()

    return token


async def _get_token() -> str:
    db = SessionLocal()
    try:
        token_row = get_neos_token(db)
    finally:
        db.close()

    if not token_row:
        # No cached token — log in automatically instead of failing.
        return await _login()
    return token_row.token


def _auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


# HTTP helper
async def _request(method: str, path: str, _retried: bool = False, _conflict_retries: int = 0,
                    _transport_retries: int = 0, **kwargs):

    token = await _get_token()

    try:
        resp = await neos_client.request(
            method,
            f"{NEOS_BASE_URL}{path}",
            headers=_auth_headers(token),
            **kwargs,
        )
    except (httpx.TimeoutException, httpx.TransportError) as e:
        if _transport_retries < 3:
            wait_seconds = 1.0 * (2 ** _transport_retries)  # 1s, 2s, 4s
            print(f"[Neos] {type(e).__name__} on {method} {path}, retrying in {wait_seconds}s "
                  f"(attempt {_transport_retries + 1}/3)")
            await asyncio.sleep(wait_seconds)
            return await _request(method, path, _retried=_retried, _conflict_retries=_conflict_retries,
                                   _transport_retries=_transport_retries + 1, **kwargs)
        raise RuntimeError(
            f"Neos {method} {path} failed after 3 retries due to {type(e).__name__}: {e}"
        ) from e

    if resp.status_code == 401:
        db = SessionLocal()
        try:
            delete_neos_token(db)
        finally:
            db.close()

        if not _retried:
            # Token was stale/expired — log in fresh and try once more.
            await _login()
            return await _request(method, path, _retried=True, _conflict_retries=_conflict_retries,
                                   _transport_retries=_transport_retries, **kwargs)

        raise RuntimeError(
            "Neos token expired (401) even after re-login. "
            "Check NEOS_USERNAME/NEOS_PASSWORD/NEOS_FIRM_ID."
        )

    if resp.status_code == 409 and _conflict_retries < 3:
        wait_seconds = 0.5 * (2 ** _conflict_retries)  # 0.5s, 1s, 2s
        print(f"[Neos] 409 conflict on {method} {path}, retrying in {wait_seconds}s "
              f"(attempt {_conflict_retries + 1}/3)")
        await asyncio.sleep(wait_seconds)
        return await _request(method, path, _retried=_retried, _conflict_retries=_conflict_retries + 1,
                               _transport_retries=_transport_retries, **kwargs)

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError:
        print("\n========== NEOS ERROR ==========")
        print(f"Status: {resp.status_code}")
        print(f"Method: {method}")
        print(f"Path: {path}")

        try:
            print("Response JSON:")
            print(resp.json())
        except Exception:
            print("Response Text:")
            print(resp.text)

        print("================================\n")

        raise
    return resp.json() if resp.content else {}


# Case lookup
async def _search_case(first_name: str, last_name: str) -> dict | None:
    """
    Search Neos for an existing case. Follows the documented priority order:
      1. ResultType == "Intake"          -> result.Id
      2. ResultType == "Name" and IsParty -> PartyMetadata.LastCreatedCase.Id
      3. ResultType == "Case"            -> result.Id
    Returns {"case_id": ..., "case_number": ...} or None.
    """
    encoded = urllib.parse.quote(f"{last_name}, {first_name}")
    try:
        data = await _request("GET", f"/neos/search/KEYWORD?searchCriteria={encoded}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise

    if data is None:
        return None

    results = data if isinstance(data, list) else data.get("Results", [])
    if not results:
        return None

    # 1st priority: Intake results — pick the one with the highest case number
    intake_results = [
        r for r in results
        if r.get("ResultType") == "Intake" and r.get("Id")
    ]
    if intake_results:
        def _case_num(r) -> int:
            try:
                return int(r.get("CaseNumber") or 0)
            except (ValueError, TypeError):
                return 0

        best = max(intake_results, key=_case_num)
        return {"case_id": best["Id"], "case_number": best.get("CaseNumber")}

    # 2nd priority: Name results where IsParty is true
    for r in results:
        if r.get("ResultType") == "Name" and r.get("IsParty"):
            party_metadata = r.get("PartyMetadata") or {}
            case_id = party_metadata.get("LastCreatedCase", {}).get("Id")
            if case_id:
                return {"case_id": case_id, "case_number": None}
            print(f"[Neos] Name/IsParty result missing PartyMetadata.LastCreatedCase.Id: {r}")

    # 3rd priority: Case results
    for r in results:
        if r.get("ResultType") == "Case" and r.get("Id"):
            return {"case_id": r["Id"], "case_number": r.get("CaseNumber")}

    return None


async def _search_case_any(first_name: str, last_name: str) -> dict | None:
    """
    Last-resort fallback used only after the priority-ordered _search_case
    comes back empty. Only matches results that actually represent a case:
      - Intake / Case ResultType -> Id IS the case id
      - Any ResultType with PartyMetadata.LastCreatedCase.Id present
    Deliberately does NOT fall back to a bare Name record's own Id, since
    that Id is a party/name id, not a case id, and using it 404s downstream.
    """
    encoded = urllib.parse.quote(f"{last_name}, {first_name}")
    try:
        data = await _request("GET", f"/neos/search/KEYWORD?searchCriteria={encoded}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise

    if data is None:
        return None

    results = data if isinstance(data, list) else data.get("Results", [])
    for r in results:
        if r.get("ResultType") in ("Intake", "Case") and r.get("Id"):
            print(f"[Neos] Last-resort match for {first_name} {last_name}: {r}")
            return {"case_id": r["Id"], "case_number": r.get("CaseNumber")}
        party_metadata = r.get("PartyMetadata") or {}
        case_id = party_metadata.get("LastCreatedCase", {}).get("Id")
        if case_id:
            print(f"[Neos] Last-resort PartyMetadata match for {first_name} {last_name}: {r}")
            return {"case_id": case_id, "case_number": None}
    return None


async def _get_party_id(case_id: str) -> str | None:
    """Return parties[0].Id for the given case."""
    data = await _request("GET", f"/cases/{case_id}/parties")
    if data is None:
        return None
    parties = data if isinstance(data, list) else data.get("parties", [])
    if not parties:
        return None
    return parties[0].get("Id")


async def _find_existing_name_id(first_name: str, last_name: str) -> str | None:
    """
    Check whether a Name record already exists for this person before
    creating a new one. Returns the Id of the first exact FirstName/LastName
    match with ResultType == "Name", or None if no match is found.

    This exists because repeatedly calling POST /Names for a person who
    already has a Name record creates duplicates, which appears to be what
    causes /import/intakes to 500 (Neos docs: 500 on intake creation can mean
    "duplicate name in Neos").
    """
    encoded = urllib.parse.quote(f"{last_name}, {first_name}")
    try:
        data = await _request("GET", f"/neos/search/KEYWORD?searchCriteria={encoded}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise

    if data is None:
        return None

    results = data if isinstance(data, list) else data.get("Results", [])
    for r in results:
        if (
            r.get("ResultType") == "Name"
            and r.get("FirstName") == first_name
            and r.get("LastName") == last_name
            and r.get("Id")
        ):
            return r["Id"]
    return None


async def _create_case(first_name: str, last_name: str) -> dict:
    """Create a new Name + Intake in Neos and return the new case_id.

    /import/intakes occasionally 500s. This can be a transient server error
    OR a duplicate-name situation. We retry the intake call itself a few
    times with backoff first (covers the transient case), then re-search
    (covers the duplicate-name case). If neither resolves it, we raise
    clearly rather than guessing at a case id.
    """
    existing_name_id = await _find_existing_name_id(first_name, last_name)
    if existing_name_id:
        print(
            f"[Neos] Name record already exists for {first_name} {last_name} "
            f"({existing_name_id}) -- skipping /Names creation to avoid duplicates."
        )
    else:
        await _request("POST", "/Names", json={
            "FirstName": first_name,
            "LastName": last_name,
            "IsPerson": True,
        })

    intake_payload = {
        "FirstName": first_name,
        "LastName": last_name,
        "PrimaryStaffId": PRIMARY_STAFF_ID,
        "CaseTypeId": DEFAULT_CASE_TYPE_ID,
    }

    intake_created = False
    last_error = None
    for attempt in range(3):
        try:
            await _request("POST", "/import/intakes", json=intake_payload)
            intake_created = True
            break
        except httpx.HTTPStatusError as e:
            last_error = e
            if e.response.status_code == 500:
                wait_seconds = 1.5 * (attempt + 1)
                print(
                    f"[Neos] /import/intakes 500'd for {first_name} {last_name} "
                    f"(attempt {attempt + 1}/3), retrying in {wait_seconds}s."
                )
                await asyncio.sleep(wait_seconds)
            else:
                raise

    if not intake_created:
        print(
            f"[Neos] /import/intakes failed all 3 attempts for {first_name} {last_name}, "
            f"rechecking if a case already exists."
        )

    # Give Neos a moment to index the Name record before searching.
    await asyncio.sleep(2)
    case_info = await _search_case(first_name, last_name)

    if not case_info:
        case_info = await _search_case_any(first_name, last_name)

    if not case_info:
        if last_error is not None and not intake_created:
            raise RuntimeError(
                f"Neos /import/intakes failed repeatedly for {first_name} {last_name} "
                f"and no existing case was found -- this needs manual intake creation in Neos."
            ) from last_error
        raise RuntimeError(
            f"Could not find Neos case after creating it for {first_name} {last_name} "
            f"(intake creation may have failed due to a duplicate name -- check Neos manually)"
        )
    return case_info


# Tag builder
def _build_tag(status: str, decline_type: str | None, reason: str) -> str:
    if status == "proceed":
        return f"[Ramos AI PROCEED] {reason}"
    if decline_type == "declined_refer_out":
        return f"[Ramos AI DECLINE – REFER OUT] {reason}"
    return f"[Ramos AI DECLINE] {reason}"


# Neos writes
async def _write_blue_banner(case_id: str, tag: str) -> None:
    """PATCH /cases/{case_id} — blue IntakeComment banner in case header.
    Only visible on case types that have IntakeComment in their layout (e.g. not CPC)."""
    await _request("PATCH", f"/cases/{case_id}",
        json=[{"op": "replace", "path": "/IntakeComment", "value": tag}]
    )


async def _write_party_comment(case_id: str, party_id: str, tag: str) -> None:
    """PUT /dynamicforms/{case_id} — IntakeComments field under Party tab."""
    await _request("PUT", f"/dynamicforms/{case_id}",
        json=[{"FieldId": "IntakeComments", "Value": tag, "PartyId": party_id}]
    )


async def _write_activity_note(case_id: str, tag: str) -> None:
    """POST /cases/{case_id}/notes — posts a note to the activity feed (always visible)."""
    await _request("POST", f"/cases/{case_id}/notes",
        json={
            "TopicId": "0ac7e736-a55a-4bcd-9e9f-774f7079cf5e",  # AI Conversation topic — hardcoded
            "Text": f"<p><strong>{tag}</strong></p>",
        }
    )


async def update_neos_lead(
    first_name: str,
    last_name: str,
    status: str,
    decline_type: str | None,
    reason: str,
) -> dict:
    """
    Full Neos update flow for a single classified lead.

    1. Find or create the case.
    2. Get party_id.
    3. Write to Neos in three places:
       - POST  /cases/{case_id}/notes    → Activity feed note (always visible, primary)
       - PATCH /cases/{case_id}          → blue IntakeComment banner (layout-dependent)
       - PUT   /dynamicforms/{case_id}   → Party tab IntakeComments field

    Returns { case_id, case_number, party_id, tag }.
    """
    tag = _build_tag(status, decline_type, reason)
    case_info = await _search_case(first_name, last_name)

    if not case_info:
        case_info = await _create_case(first_name, last_name)

    case_id = case_info["case_id"]
    case_number = case_info["case_number"]

    party_id = await _get_party_id(case_id)
    if not party_id:
        raise RuntimeError(
            f"Could not get party_id for case {case_id} ({first_name} {last_name})"
        )

    # Primary write — do this first and let it raise if it fails.
    await _write_activity_note(case_id, tag)

    try:
        await _write_blue_banner(case_id, tag)
    except Exception:
        pass

    try:
        await _write_party_comment(case_id, party_id, tag)
    except Exception:
        pass

    return {"case_id": case_id, "case_number": case_number, "party_id": party_id, "tag": tag}


async def get_case_status(intake_id: str):
    intake = await _request(
        "GET",
        f"/intakes/v2/{intake_id}/header"
    )
    return intake

async def get_case_header(case_id: str):
    return await _request(
        "GET",
        f"/cases/{case_id}/header"
    )

async def get_assigned_attorney(case_id: str):
    header = await get_case_header(case_id)
    assigned = None

    for item in header.get("Details", []):
        if item.get("Title") == "Assigned":
            assigned = item.get("Value")
            break
    return assigned

async def get_case_status(intake_id: str):
    intake = await _request(
        "GET",
        f"/intakes/v2/{intake_id}/header"
    )
    return intake 

async def get_lead_status(first_name: str, last_name: str):
    case = await _search_case(first_name, last_name)

    if not case:
        return None

    intake = await get_case_status(case["case_id"])
    assigned = await get_assigned_attorney(case["case_id"])

    return {
        "case_id": case["case_id"],
        "case_number": case["case_number"],
        "intake_stage": intake.get("IntakeStage"),
        "intake_status": intake.get("IntakeStatus"),
        "assigned": assigned,
    }

def _extract_case_results(data) -> list[dict]:
    """Normalize the list wrappers returned by different Neos API versions."""
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("Results", "results", "Data", "data", "Items", "items"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


async def search_cases(
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    order_by: str = "CaseStatus",
    sort_order: str = "desc",
) -> list[dict]:
    """Return retained Neos cases, never intake/lead records."""
    if sort_order.lower() not in {"asc", "desc"}:
        raise ValueError("sort_order must be 'asc' or 'desc'")

    params = {"OrderBy": order_by, "SortOrder": sort_order.lower()}
    if date_from:
        params["DateFrom"] = _neos_datetime(date_from)
    if date_to:
        params["DateTo"] = _neos_datetime(date_to)

    data = await _request("GET", "/cases/v2/search", params=params)
    return _extract_case_results(data)


def _neos_datetime(value: datetime) -> str:
    """Format a datetime as UTC ISO-8601, as required by Cases v2 search."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def get_case_by_number(
    case_number: int,
    first_name: str,
    last_name: str,
) -> dict | None:

    print("\n========== NEOS CASE LOOKUP ==========")
    print(f"Requested CaseNumber: {case_number}")
    print(f"Client: {first_name} {last_name}")

    # ---------------------------------------------------------
    # STEP 1: Search by client name to get the exact case ID
    # ---------------------------------------------------------

    encoded = urllib.parse.quote(f"{last_name}, {first_name}")

    try:
        data = await _request(
            "GET",
            f"/neos/search/KEYWORD?searchCriteria={encoded}"
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            print("[Neos] No search results found.")
            return None
        raise

    if data is None:
        return None

    results = (
        data
        if isinstance(data, list)
        else data.get("Results", [])
    )

    print(f"[Neos] Search returned {len(results)} results")

    for r in results:
        print(
            f"ResultType={r.get('ResultType')} | "
            f"CaseNumber={r.get('CaseNumber')} | "
            f"Id={r.get('Id')} | "
            f"CaseName={r.get('CaseName')} | "
            f"FirstName={r.get('FirstName')} | "
            f"LastName={r.get('LastName')}"
        )

    # ---------------------------------------------------------
    # STEP 2: Find exact CaseNumber
    # ---------------------------------------------------------

    matched_case_id = None

    for r in results:
        if (
            r.get("ResultType") in ("Case", "Intake")
            and str(r.get("CaseNumber")) == str(case_number)
            and r.get("Id")
        ):
            matched_case_id = r["Id"]

            print(
                f"[Neos] FOUND CASE "
                f"CaseNumber={r.get('CaseNumber')} "
                f"Id={matched_case_id}"
            )

            break

    if not matched_case_id:
        print(
            f"[Neos] CaseNumber {case_number} "
            f"was NOT found for {first_name} {last_name}"
        )
        return None

    # ---------------------------------------------------------
    # STEP 3: Get cases from Cases V2
    # ---------------------------------------------------------

    print("\n========== NEOS CASES V2 ==========")
    print("Fetching cases from /cases/v2/search")

    cases_data = await _request(
        "GET",
        "/cases/v2/search",
        params={
            "OrderBy": "CaseStatus",
            "SortOrder": "desc",
        },
    )

    cases = _extract_case_results(cases_data)

    print(f"[Neos] Cases V2 returned {len(cases)} cases")

    # ---------------------------------------------------------
    # STEP 4: Find the exact UUID
    # ---------------------------------------------------------

    for case in cases:

        current_id = str(
            case.get("Id") or ""
        ).strip().lower()

        target_id = str(
            matched_case_id
        ).strip().lower()

        if current_id == target_id:

            print("\n[Neos] FULL CASE FOUND")
            print(case)

            print(
                f"[Neos] FirstPartyIsOurClient="
                f"{case.get('FirstPartyIsOurClient')}"
            )

            return case

    print(
        f"[Neos] Case ID {matched_case_id} "
        f"was not found in /cases/v2/search results."
    )

    return None

# Temporary compatibility aliases. Remove after all callers use case wording.
get_all_intake_status = search_cases 


async def get_full_case(case_id: str) -> dict:
    print("\n========== NEOS FULL CASE ==========")
    print(f"Fetching full case for ID: {case_id}")

    data = await _request(
        "GET",
        f"/cases/{case_id}/header"
    )

    print("[Neos] Full case/header response:")
    print(data)

    return data
