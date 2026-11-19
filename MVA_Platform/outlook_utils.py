# outlook_utils.py
import requests
import msal
import os
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from db import SessionLocal, EmailRaw, LeadIntake, IntakeSpecialist, Session
import re
import asyncio
import httpx
from datetime import datetime, timedelta, date, time
import langid
from MVA_Platform.llm_service import generate_nafi_answer, build_lead_input, classify_leads
from MVA_Platform.neos_service import update_neos_lead, get_lead_status
from MVA_Platform.neos_direct import neos_writable_clause, lead_neos_writes_blocked, not_neos_direct_clause
from MVA_Platform.lead_utils import ( split_full_name, finalize_lead_name,
    apply_agent_status, log_status_change, current_prompt_version,
    has_valid_name, sanitize_name,
)
import geonamescache
import json
import traceback
from db import get_neos_token
from zoneinfo import ZoneInfo
load_dotenv()

TENANT_ID = os.getenv("Tenant_ID")
CLIENT_ID = os.getenv("Application_ID")
CLIENT_SECRET = os.getenv("Secret_Value")
USER_EMAIL = os.getenv("MAILBOX_USER")

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPE = ["https://graph.microsoft.com/.default"]
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

# Global token cache
ACCESS_TOKEN = None 

CASE_TYPE_MAPPING = {
    # MVA
    "auto accident": "MVA",
    "car accident": "MVA",
    "motor vehicle accident": "MVA",
    "motor vehicle accident claim": "MVA",
    "mva": "MVA",
    "mva22": "MVA",
    "mva22 - car accident": "MVA",
    "mva22-car accident": "MVA",

    # Workers Compensation
    "injured while working": "WCC",
    "workers compensation": "WCC",
    "workers' compensation": "WCC",
    "worker compensation": "WCC",
    "work comp": "WCC",
    "workers comp": "WCC",
    "workplace injury": "WCC",

    # General Personal Injury
    "gpi": "GPI",
    "general personal injury": "GPI",
    "personal injury": "GPI",

    # Medical
    "med": "MED",
    "medical": "MED",
    "medical malpractice": "MED",
    "med - medical malpractice": "MED",

    # Consumer
    "consumer": "Consumer",
    "consumer law": "Consumer",
    "cpc - consumer law": "Consumer",

    # Aviation
    "avm - aviation": "AVM - Aviation",

    # Claims
    "claims": "Claims",

    # Elder abuse
    "nur - elder abuse": "NUR - Elder Abuse",
}

subject_keywords = [
    "new lead",
    "free case evaluation",
    "online form submission",
    "new mva22 intake",
    "new phone call lead - callrail",
    "new meta ad",
    "new meta ads lead",
    "callrail",
    "internal intake form",
    "potential client",
    "fcra",
    "efta",
]

EMAIL_PAGE_SIZE = 100
MAX_EMAIL_PAGES = 3

def get_access_token():
    global ACCESS_TOKEN

    app = msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET
    )

    result = app.acquire_token_for_client(scopes=SCOPE)

    if "access_token" not in result:
        raise Exception(f"Token error: {result}")

    ACCESS_TOKEN = result["access_token"]
    return ACCESS_TOKEN

async def graph_request(url, params=None):
    global ACCESS_TOKEN

    if not ACCESS_TOKEN:
        ACCESS_TOKEN = get_access_token()

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}"
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, headers=headers, params=params)

    if response.status_code == 401:
        print("Token expired, regenerating...")
        ACCESS_TOKEN = get_access_token()
        headers["Authorization"] = f"Bearer {ACCESS_TOKEN}"
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, params=params)

    if response.status_code != 200:
        raise Exception(f"Graph API Error: {response.text}")

    return response.json()

async def get_assigned_users(practice_area: str, group: int, db: Session):
    practice_area = practice_area.upper()

    start = 1 if group == 1 else 4
    end = 3 if group == 1 else 6

    users = db.query(IntakeSpecialist).filter(
        IntakeSpecialist.practice_area == practice_area,
        IntakeSpecialist.position.between(start, end)
    ).order_by(IntakeSpecialist.position).all()

    return [u.email for u in users]

async def fetch_emails():
    """Fetch message IDs first"""
    url = f"{GRAPH_BASE_URL}/users/{USER_EMAIL}/messages"

    params = {
        "$orderby": "receivedDateTime DESC", # or DESC = new to old/ ASC = old to new
        "$select": "id,subject,from,toRecipients,receivedDateTime,body,conversationId",
        "$top": 50
    }

    data = await graph_request(url, params)
    return data.get("value", [])

    # code block to fetch all the emails from the inbox
    # all_messages = []
    
    # # Pagination loop to get ALL emails
    # while url:
    #     data = graph_request(url, params)
    #     messages = data.get("value", [])
    #     all_messages.extend(messages)
        
    #     # Check if there are more pages
    #     if "@odata.nextLink" not in data:
    #         break
            
    #     url = data.get("@odata.nextLink")
    #     params = None  # nextLink already contains query params
    
    # print(f"Fetched {len(all_messages)} total emails from mailbox")
    # return all_messages


# state city function
gc = geonamescache.GeonamesCache()

US_STATES = gc.get_us_states()
CITIES = gc.get_cities()

STATE_NAMES = {s["name"].lower(): s["name"] for s in US_STATES.values()}

# city -> set of state names (built once instead of looping on every call)
CITY_TO_STATES = {}
for _c in CITIES.values():
    if _c.get("countrycode") == "US" and _c.get("admin1code") in US_STATES:
        CITY_TO_STATES.setdefault(_c["name"].lower(), set()).add(
            US_STATES[_c["admin1code"]]["name"]
        )

def normalize_case_type(value):
    if not value:
        return None

    value = re.sub(r"\s+", " ", str(value)).strip()
    key = value.lower()

    # Exact mapping first
    if key in CASE_TYPE_MAPPING:
        return CASE_TYPE_MAPPING[key]

    # Handle contaminated values defensively
    if "incident date" in key:
        value = re.split(r"\bincident date\b", value, flags=re.IGNORECASE)[0].strip()

    if "incident location" in value.lower():
        value = re.split(
            r"\bincident location\b",
            value,
            flags=re.IGNORECASE
        )[0].strip()

    if "reference number" in value.lower():
        value = re.split(
            r"\breference number\b",
            value,
            flags=re.IGNORECASE
        )[0].strip()

    key = value.lower()

    if key in CASE_TYPE_MAPPING:
        return CASE_TYPE_MAPPING[key]

    return value

def normalize_state_of_incident(value, strict=False):
    if not value:
        return None
    value = str(value).strip()
    if not value:
        return None

    if value.lower() in STATE_NAMES:
        return STATE_NAMES[value.lower()]

    code = value.upper()
    if len(code) == 2 and code in US_STATES:
        return US_STATES[code]["name"]

    states = CITY_TO_STATES.get(value.lower())
    if states and len(states) == 1:
        return next(iter(states))

    return None if strict else value


EMPTY_VALUES = {"NA", "N/A", "NONE", "NULL", "UNKNOWN", "CALLER DECLINED"}

KNOWN_LABELS = {
    # names
    "name", "first name", "last name", "client name",
    "caller first name", "caller last name",
    "client first name", "client last name",

    # contact / location
    "email", "phone", "phone number", "tel#", "alt#", "best time to reach",
    "address", "city", "state", "zip", "city/state", "state of incident",
    "case state", "location", "language", "relation to client",

    # case info
    "main reason for contacting ramos law",
    "case type",
    "type of accident",
    "case description & questions",
    "case description",
    "case details",
    "case information",
    "synopsis",
    "call summary",
    "brief regarding",
    "message",
    "facts",
    "additional details, if applicable",
    "additional notes",

    # incident information
    "incident date",
    "incident location",
    "incident city",
    "incident state",
    "reference number",
    "claim number",
    "date of incident",

    "when did the accident happen",
    "dol",
    "who was at fault",
    "how bad were your injuries",
    "did you get medical treatment",
    "do you have insurance",

    # source
    "source", "name of source", "source details", "referral source",
    "referred by", "how did you hear about us",
    "if other, please specify",
    "name of personal family/friends/relatives/someone i know",
    "what tv station",

    # misc
    "form submitted by",
    "call date",
    "same state",
    "for",
    "co",

    # meta ads
    "campaign name",
    "platform",
    "adset name",
    "ad name",
    "lead id",
    "form name",
    "created at", 

    "spanish only",
    "transferred to",
}

STOP_EXACT = {
    "marketing information", "metadata", "customer information", "call details",
    "call leg details", "collected data", "landing page details",
}
STOP_PREFIXES = (
    "sincerely", "kind regards", "confidentiality notice", "we take care of clients",
    "also, is this where", "this email was sent to you",
    "access your page details","we take care of clients",
)

META_PLATFORMS = {"fb": "Facebook", "ig": "Instagram"}

def _norm_label(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower().rstrip(":?").strip())


def parse_labeled_fields(body_text: str) -> dict:
    lines = [l.strip() for l in body_text.splitlines() if l.strip()]
    fields = {}
    current = None
    i = 0

    while i < len(lines):
        line = lines[i]
        low = line.lower()

        if _norm_label(line) in STOP_EXACT or low.startswith(STOP_PREFIXES):
            current = None
            i += 1
            continue

        nl = _norm_label(line)
        if nl in KNOWN_LABELS:
            value = ""
            if i + 1 < len(lines) and lines[i + 1].startswith(":"):
                value = lines[i + 1][1:].strip()
                i += 1
            fields.setdefault(nl, []).append(value)
            current = nl
            i += 1
            continue

        # "Label: value" on the same line
        if ":" in line:
            label, _, rest = line.partition(":")
            nl = _norm_label(label)
            if nl in KNOWN_LABELS:
                fields.setdefault(nl, []).append(rest.strip())
                current = nl
                i += 1
                continue

        if current is not None:
            fields[current][-1] = (fields[current][-1] + "\n" + line).strip()

        i += 1

    return fields


def _first(fields: dict, *labels):
    """First non-empty value among the given labels (in priority order)."""
    for label in labels:
        for v in fields.get(label, []):
            v = (v or "").strip()
            if v and v.upper() not in EMPTY_VALUES:
                return v
    return None


def get_number(val):
    if not val:
        return None
    m = re.search(r"\d+", val)
    return int(m.group()) if m else None


# ---------------- Fallback for emails we can't parse into fields ----------------
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
VOICEMAIL_PHONE_RE = re.compile(
    r"voice\s*mail from\s*(\+?1?[\s\-.(]*\d[\d\s\-.()]{8,})", re.IGNORECASE
)
MAX_DESCRIPTION_CHARS = 20000


def _strip_signature_lines(text: str) -> str:
    """Remove the 'd: / f:' signature phone lines so they aren't picked as the lead's phone."""
    lines = text.splitlines()
    out, skip_next = [], False
    for line in lines:
        s = line.strip().lower()
        if skip_next:
            skip_next = False
            continue
        if s in {"d:", "f:", "o:"}:
            skip_next = True
            continue
        out.append(line)
    return "\n".join(out)


def extract_contact_fallback(body_text: str):
    """Best-effort (email, phone) from free text."""
    text = _strip_signature_lines(body_text or "")

    phone = None
    m = VOICEMAIL_PHONE_RE.search(text)
    if m:
        phone = m.group(1).strip()
    else:
        m = PHONE_RE.search(text)
        if m:
            phone = m.group(0).strip()

    email_addr = None
    for cand in EMAIL_RE.findall(text):
        low = cand.lower()
        if low.endswith("@ramoslaw.com") or low.startswith(("noreply@", "no-reply@", "donotreply@")):
            continue
        email_addr = cand
        break

    return email_addr, phone


def build_unstructured_lead(body_text: str, subject: str = None, sender: str = None) -> dict:
    email_addr, phone = extract_contact_fallback(body_text)
    description = f"Subject: {subject or ''}\n\n{body_text or ''}".strip()

    return {
        "first_name": None,
        "last_name": None,
        "email": email_addr,
        "phone": phone,
        "state_of_incident": None,
        "case_type": None,
        "accident_date": None,
        "fault": None,
        "injury_severity": None,
        "medical_treatment": None,
        "insurance": None,
        "source": f"Email - {sender}" if sender else None,
        "case_description": description[:MAX_DESCRIPTION_CHARS],
    }


# ---------------- Unbounce ----------------
UNBOUNCE_KEYS = {"email", "state", "city", "phone"}


def parse_unbounce_lead(body_text: str) -> dict:
    lines = [l.strip() for l in body_text.splitlines() if l.strip()]

    page_name = None
    for idx, l in enumerate(lines):
        if l == "Page Name" and idx + 1 < len(lines):
            page_name = lines[idx + 1]
            break

    start = next((i + 1 for i, l in enumerate(lines) if "Submitted Form Data" in l), len(lines))

    data, current = {}, None
    for line in lines[start:]:
        if "Access your page details" in line:
            break
        key = line.lower()
        if key in UNBOUNCE_KEYS or re.fullmatch(r"[a-z][a-z0-9]*(_[a-z0-9]+)+", line):
            current = key
            data[current] = ""
        elif current:
            data[current] = (data[current] + "\n" + line).strip()

    state = normalize_state_of_incident(data.get("state"))
    if not state and data.get("city"):
        state = normalize_state_of_incident(data["city"], strict=True)
    if not state and page_name:          # e.g. "Google Ads | Arizona - Personal Injury"
        for name_lc, name in STATE_NAMES.items():
            if name_lc in page_name.lower():
                state = name
                break

    return {
        "first_name": data.get("first_name") or None,
        "last_name": data.get("last_name") or None,
        "email": data.get("email") or None,
        "phone": data.get("phone_number") or data.get("phone") or None,
        "state_of_incident": state,
        "case_type": data.get("main_reason_for_contacting_ramos_law")
                     or data.get("case_type") or page_name,
        "accident_date": None,
        "fault": None,
        "injury_severity": None,
        "medical_treatment": None,
        "insurance": None,
        "source": "Unbounce Landing Page",
        "case_description": data.get("case_information") or data.get("case_description") or None,
    }


# ---------------- Name clean-up ----------------
async def clean_lead_name(lead):
    """Apply name cleanup after a status exists. Never raises."""
    try:
        before = (lead.first_name, lead.last_name)
        action = await finalize_lead_name(lead)

        if action != "kept" and action != "skipped":
            print(
                f"[name-clean] lead {getattr(lead, 'id', '?')}: "
                f"{action} {before} -> "
                f"{(lead.first_name, lead.last_name)}"
            )

    except Exception as exc:
        print(
            f"[name-clean] lead {getattr(lead, 'id', '?')} failed: {exc}"
        )

# ---------------- Everything else ----------------
async def parse_lead(body_text, subject=None, sender=None):
    low = (body_text or "").lower()
    if "unbounce" in low or "a new lead has been captured from your unbounce page" in low:
        return parse_unbounce_lead(body_text)

    f = parse_labeled_fields(body_text or "")

    name_candidates = [
        (_first(f, "client first name"), _first(f, "client last name")),
        (_first(f, "caller first name"), _first(f, "caller last name")),
        (_first(f, "first name"), _first(f, "last name")),
        split_full_name(_first(f, "client name")),
        split_full_name(_first(f, "name")),
    ]
    had_name_field = any(any(p) for p in name_candidates)
    first_name, last_name = next(
        (p for p in name_candidates if has_valid_name(*p)), (None, None)
    )

    email_value = _first(f, "email")
    phone_value = _first(f, "phone number", "phone", "tel#")

    if not any([had_name_field, email_value, phone_value]):
        return build_unstructured_lead(body_text, subject, sender)

    state = normalize_state_of_incident(_first(f, "state of incident", "state", "case state"))
    if not state:
        state = normalize_state_of_incident(
            _first(f, "city/state", "location", "city"), strict=True
        )

    source = _first(f, "source", "referral source", "referred by", "how did you hear about us")
    detail = _first(f, "source details", "name of source")
    if detail and (not source or source.lower() in {"other", "unknown"}):
        source = detail
    if not source and _first(f, "campaign name"):
        platform = _first(f, "platform") or ""
        source = f"Meta Ads - {META_PLATFORMS.get(platform.lower(), platform)}".strip(" -")

    description = _first(
        f, "case description & questions", "case description", "case details",
        "case information", "synopsis", "brief regarding", "call summary",
        "message", "facts",
    )
    extra = _first(f, "additional details, if applicable", "additional notes")
    if extra and extra != description:
        description = f"{description}\n\nAdditional details: {extra}" if description else extra

    return {
        "first_name": first_name,
        "last_name": last_name,
        "email": email_value,
        "phone": phone_value,
        "state_of_incident": state,
        "case_type": normalize_case_type(
            _first(
                f,
                "type of accident",
                "case type",
                "main reason for contacting ramos law",
            )
        ),
        "accident_date": _first(f,"incident date","date of incident","when did the accident happen","dol"),
        "fault": _first(f, "who was at fault"),
        "injury_severity": get_number(_first(f, "how bad were your injuries")),
        "medical_treatment": _first(f, "did you get medical treatment"),
        "insurance": _first(f, "do you have insurance"),
        "source": source,
        "case_description": description,
    }


def parse_datetime(dt_str):
    if not dt_str:
        return None
    try:
        return datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        # e.g. fractional seconds
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00")).replace(tzinfo=None)


async def html_to_text(html_content):
    soup = BeautifulSoup(html_content or "", "lxml")

    for tag in soup(["script", "style"]):
        tag.decompose()

    table_lines = []

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"], recursive=False)

            values = [
                cell.get_text(" ", strip=True)
                for cell in cells
            ]

            for i in range(0, len(values) - 1, 2):
                label = values[i].strip().rstrip(":")
                value = values[i + 1].strip()

                if label:
                    table_lines.append(f"{label}: {value}")

        table.decompose()

    remaining_text = soup.get_text(separator="\n")

    lines = [
        line.strip()
        for line in remaining_text.splitlines()
        if line.strip()
    ]

    return "\n".join(lines + table_lines)

def _sender_address(msg) -> str:
    return ((msg.get("from") or {}).get("emailAddress") or {}).get("address")

async def save_email(db, msg, body_text):
    email = EmailRaw(
        message_id=msg.get("id"),
        subject=msg.get("subject"),
        sender=_sender_address(msg),
        recipients=[
            (r.get("emailAddress") or {}).get("address")
            for r in (msg.get("toRecipients") or [])
        ],
        received_at=parse_datetime(msg.get("receivedDateTime")),
        sent_at=parse_datetime(msg.get("sentDateTime")),
        conversation_id=msg.get("conversationId"),
        body_text=body_text,
        body_html=(msg.get("body") or {}).get("content"),
    )

    db.add(email)
    db.flush()

    return email

async def save_lead(db, email_obj, data, tool_recommendation=None, tool_reason=None, status="pending", decline_type=None, reviewed_by=None,
    reviewer_note=None, reviewed_at=None, language="en"):
    first_name, last_name = sanitize_name(data.get("first_name"), data.get("last_name"))

    lead = LeadIntake(
        email_record=email_obj,   
        first_name=first_name,
        last_name=last_name,
        email=data.get("email"),  
        phone=data.get("phone"),
        state_of_incident=data.get("state_of_incident"),
        case_type=data.get("case_type"),
        accident_date=data.get("accident_date"),
        fault=data.get("fault"),
        injury_severity=data.get("injury_severity"),
        medical_treatment=data.get("medical_treatment"),
        insurance=data.get("insurance"),
        source=data.get("source"),
        case_description=data.get("case_description"),
        nafi_tool_recommendation=tool_recommendation,
        nafi_tool_reason=tool_reason,
        status=status,
        decline_type=decline_type,
        reviewed_by=reviewed_by,
        reviewer_note=reviewer_note,
        reviewed_at=reviewed_at,
        received_at=email_obj.received_at,
        language=language,
    )

    db.add(lead)
    db.flush()   
    log_status_change(db, lead.id, None, lead.status, changed_by=reviewed_by)
    # db.commit()
    return lead

def is_valid_lead_subject(subject: str) -> bool:
    subject = re.sub(r"\s+", " ", (subject or "").lower()).strip()
    return any(k in subject for k in subject_keywords)

def detect_lead_language(text: str):
    if not text:
        return {
            "language": "en",
            "confidence": 0
        }

    language, confidence = langid.classify(text)

    return {
        "language": language,
        "confidence": confidence
    }

def get_readable_error(error_str: str) -> str:
    return "The NAFI Screening tool was unable to process this request because the case details contain sensitive content, including sexual, hateful, violent, or self-harm-related material."

def is_no_reply_email(email: str) -> bool:
    if not email:
        return False

    NO_REPLY_PATTERNS = [
        "noreply@",
        "noreplay@",
        "no-reply@",
        "donotreply@",
        "do-not-reply@"
    ]

    email = email.strip().lower()

    return any(
        pattern in email
        for pattern in NO_REPLY_PATTERNS
    )

AVIATION_KEYWORDS = [
    "faa",
    "aviation",
    "pilot",
    "cessna"
]

def is_aviation_case(lead_data: dict) -> bool:
    searchable_text = " ".join(
        filter(
            None,
            [
                lead_data.get("case_type"),
                lead_data.get("case_description")
            ]
        )
    ).lower()

    return any(
        keyword in searchable_text
        for keyword in AVIATION_KEYWORDS
    )


NAFI_MAPPING = {
    "Likely NAFI": "likely_nafi",
    "Not NAFI": "not_nafi"
}


async def run_nafi_screening(lead_data: dict) -> dict:
    result = {
        "tool_recommendation": None,
        "tool_reason": None,
        "status": "pending",
        "decline_type": None,
        "reviewed_by": None,
        "reviewer_note": None,
        "reviewed_at": None,
    }

    try:
        nafi_input = build_lead_input(lead_data)
        nafi_result, _ = await generate_nafi_answer(nafi_input)

        result["tool_recommendation"] = NAFI_MAPPING.get(nafi_result["result"], "likely_nafi")
        result["tool_reason"] = nafi_result.get("why")

    except Exception as ai_error:
        error_str = str(ai_error)

        if "content_filter" in error_str or "ResponsibleAIPolicyViolation" in error_str:
            reason = (
                "The NAFI Screening tool was unable to process this "
                "request because the case details contain sensitive "
                "content, including sexual, hateful, violent, or "
                "self-harm-related material."
            )
            result.update({
                "tool_recommendation": "rejected",
                "tool_reason": reason,
                "status": "declined",
                "decline_type": "declined_no_referral",
                "reviewed_by": "nafi screening tool",
                "reviewer_note": reason,
                "reviewed_at": datetime.utcnow(),
            })
        else:
            print(f"NAFI screening failed, saving lead anyway: {ai_error}")
            result["tool_reason"] = "NAFI screening was unavailable for this lead."

    return result


async def process_single_email(db, email: dict, body_text: str | None = None) -> dict:
    m_id = email.get("id")
    subject = email.get("subject") or ""
    sender = _sender_address(email)

    if body_text is None:
        body_text = await html_to_text((email.get("body") or {}).get("content") or "")

    lead_language = detect_lead_language(body_text)["language"]
    result = {"outcome": "failed", "email_id": None, "lead_id": None, "error": None}

    try:
        lead_data = await parse_lead(body_text, subject=subject, sender=sender)
    except Exception as parse_error:
        print(f"Parser error for {m_id}, using fallback: {parse_error}")
        lead_data = build_unstructured_lead(body_text, subject, sender)

    try:
        raw_email = await save_email(db, email, body_text)

        if is_aviation_case(lead_data):
            lead_data["case_type"] = "AVM | Aviation"

        nafi = await run_nafi_screening(lead_data)

        saved_lead = await save_lead(
            db,
            raw_email,
            lead_data,
            nafi["tool_recommendation"],
            nafi["tool_reason"],
            status=nafi["status"],
            decline_type=nafi["decline_type"],
            reviewed_by=nafi["reviewed_by"],
            reviewer_note=nafi["reviewer_note"],
            reviewed_at=nafi["reviewed_at"],
            language=lead_language,
        )

        await clean_lead_name(saved_lead)

        result.update(
            outcome="saved",
            email_id=getattr(raw_email, "id", None),
            lead_id=saved_lead.id,
        )
        db.commit()
        return result

    except Exception as e:
        print(f"Error saving {m_id}: {e}")
        db.rollback()
        result.update(outcome="failed", email_id=None, lead_id=None, error=str(e))

        try:
            raw_email = await save_email(db, email, body_text)
            minimal_lead = await save_lead(
                db,
                raw_email,
                build_unstructured_lead(body_text, subject, sender),
                status="pending",
                language=lead_language,
            )
            result.update(
                outcome="saved_minimal",
                email_id=getattr(raw_email, "id", None),
                lead_id=minimal_lead.id,
            )
            db.commit()
        except Exception as e2:
            db.rollback()
            result.update(outcome="failed", email_id=None, lead_id=None,
                          error=f"{e} | fallback also failed: {e2}")
            print(f"Fallback save also failed for {m_id}: {e2}")

    return result

RAW_LOG_WIDTH = 100
RAW_EMAIL_BODY_LIMIT = int(os.getenv("RAW_EMAIL_BODY_LIMIT", "0") or 0)

REPLY_PREFIX_RE = re.compile(r"^\s*(re|fw|fwd)\s*:", re.IGNORECASE)

_logged_message_ids: set = set()      
_raw_email_counter = 0              
_sync_totals = {"processed": 0, "saved": 0, "skipped": 0, "failed": 0}


def _fmt_addr(entry) -> str:
    ea = (entry or {}).get("emailAddress") or {}
    name, addr = ea.get("name"), ea.get("address")
    if name and addr and name.strip().lower() != addr.strip().lower():
        return f"{name} <{addr}>"
    return addr or name or "-"


def format_raw_email(number: int, msg: dict, body_text: str) -> str:
    bar, thin = "=" * RAW_LOG_WIDTH, "-" * RAW_LOG_WIDTH
    subject = msg.get("subject") or ""
    recipients = ", ".join(_fmt_addr(r) for r in (msg.get("toRecipients") or [])) or "-"

    body = (body_text or "").strip() or "(empty body)"
    if RAW_EMAIL_BODY_LIMIT and len(body) > RAW_EMAIL_BODY_LIMIT:
        body = body[:RAW_EMAIL_BODY_LIMIT] + f"\n... [truncated, {len(body)} chars total]"

    return "\n".join([
        bar,
        f"OUTLOOK RAW EMAIL #{number}",
        thin,
        f"Subject         : {subject or '-'}",
        f"From            : {_fmt_addr(msg.get('from'))}",
        f"To              : {recipients}",
        f"Received (UTC)  : {msg.get('receivedDateTime') or '-'}",
        f"Sent (UTC)      : {msg.get('sentDateTime') or '-'}",
        f"Message ID      : {msg.get('id') or '-'}",
        f"Conversation ID : {msg.get('conversationId') or '-'}",
        f"Known subject   : {'YES' if is_valid_lead_subject(subject) else 'NO (not in subject_keywords)'}",
        thin,
        "BODY",
        thin,
        body,
        thin,
    ])


def format_db_result(result: dict) -> str:
    outcome = result.get("outcome")
    if outcome == "saved":
        text = f"SAVED TO DATABASE  (email_id={result.get('email_id')}, lead_id={result.get('lead_id')})"
    elif outcome == "saved_minimal":
        text = (f"SAVED TO DATABASE with minimal data after an error  "
                f"(email_id={result.get('email_id')}, lead_id={result.get('lead_id')})\n"
                f"                  first error: {result.get('error')}")
    elif outcome == "skipped":
        text = f"NOT SAVED (skipped): {result.get('error')}"
    else:
        text = f"NOT SAVED - FAILED: {result.get('error')}"
    return f"DB RESULT       : {text}\n" + "=" * RAW_LOG_WIDTH


def _skip_reason(msg: dict, saved_by_conversation: dict) -> str | None:
    conv_id = msg.get("conversationId")
    if not conv_id or conv_id not in saved_by_conversation:
        return None

    received = parse_datetime(msg.get("receivedDateTime"))
    if received is not None and received in saved_by_conversation[conv_id]:
        return "same email is already in the database under another message id (moved/duplicate copy)"

    if REPLY_PREFIX_RE.match(msg.get("subject") or ""):
        return "reply/forward inside a conversation that is already in the database"

    return None


def _print_sync_summary(stats: dict, confirmed_emails, confirmed_leads, pages: int):
    bar = "#" * RAW_LOG_WIDTH
    lines = [
        bar,
        "OUTLOOK -> DATABASE SYNC SUMMARY   " + datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "-" * RAW_LOG_WIDTH,
        f"Fetched from Outlook (this cycle)  : {stats['fetched']}   ({pages} page(s))",
        f"  Already in database (earlier)    : {stats['already_in_db']}",
        f"  Skipped, not a new lead          : {stats['skipped']}",
        f"  New emails processed             : {stats['processed']}",
        f"     Saved to database             : {stats['saved']}"
        f"   (re-checked in DB: {confirmed_emails} email rows, {confirmed_leads} lead rows)",
        f"     Failed, NOT in database       : {stats['failed']}",
        "-" * RAW_LOG_WIDTH,
        "Running total since server start   : "
        f"processed={_sync_totals['processed']}  saved={_sync_totals['saved']}  "
        f"skipped={_sync_totals['skipped']}  failed={_sync_totals['failed']}",
        bar,
    ]
    print("\n".join(lines))


async def process_emails():
    global _raw_email_counter
    db = SessionLocal()

    try:
        url = f"{GRAPH_BASE_URL}/users/{USER_EMAIL}/messages"

        params = {
            "$orderby": "receivedDateTime DESC",
            "$select": (
                "id,subject,from,toRecipients,"
                "receivedDateTime,sentDateTime,body,conversationId"
            ),
            "$top": EMAIL_PAGE_SIZE,
        }

        pages = 0
        stats = {"fetched": 0, "already_in_db": 0, "skipped": 0,
                 "processed": 0, "saved": 0, "failed": 0}
        printed_something = False
        processed_message_ids = []
        saved_lead_ids = []
        hit_page_limit_with_more = False

        while url and pages < MAX_EMAIL_PAGES:

            data = await graph_request(url, params)
            messages = data.get("value", [])
            pages += 1
            stats["fetched"] += len(messages)

            ids = [m.get("id") for m in messages if m.get("id")]
            conversation_ids = [
                m.get("conversationId") for m in messages if m.get("conversationId")
            ]

            existing_message_ids = set()
            if ids:
                existing_message_ids = {
                    row[0]
                    for row in db.query(EmailRaw.message_id)
                    .filter(EmailRaw.message_id.in_(ids))
                    .all()
                }

            # conversation_id -> received times already saved for it
            saved_by_conversation = {}
            if conversation_ids:
                for conv_id, received_at in (
                    db.query(EmailRaw.conversation_id, EmailRaw.received_at)
                    .filter(EmailRaw.conversation_id.in_(conversation_ids))
                    .all()
                ):
                    if conv_id:
                        saved_by_conversation.setdefault(conv_id, set()).add(received_at)

            candidates = []
            for msg in messages:
                if msg.get("id") in existing_message_ids:
                    stats["already_in_db"] += 1
                else:
                    candidates.append(msg)

            processed_on_page = 0

            for msg in reversed(candidates):
                m_id = msg.get("id")
                conv_id = msg.get("conversationId")
                body_text = await html_to_text((msg.get("body") or {}).get("content"))

                reason = _skip_reason(msg, saved_by_conversation)
                if reason:
                    stats["skipped"] += 1
                    if m_id not in _logged_message_ids:
                        _logged_message_ids.add(m_id)
                        _raw_email_counter += 1
                        _sync_totals["skipped"] += 1
                        print(format_raw_email(_raw_email_counter, msg, body_text))
                        print(format_db_result({"outcome": "skipped", "error": reason}))
                        printed_something = True
                    continue

                is_retry = m_id in _logged_message_ids   # failed earlier, tried again now
                if is_retry:
                    print(f"[retry] Outlook email failed to save earlier, trying again: "
                          f"{msg.get('subject') or '-'!r} (message id {m_id})")
                else:
                    _raw_email_counter += 1
                    print(format_raw_email(_raw_email_counter, msg, body_text))

                result = await process_single_email(db, msg, body_text=body_text)
                if is_retry:
                    print(f"[retry] {format_db_result(result).splitlines()[0]}")
                else:
                    print(format_db_result(result))

                printed_something = True
                processed_on_page += 1
                stats["processed"] += 1
                if not is_retry:
                    _sync_totals["processed"] += 1
                if result["outcome"] in ("saved", "saved_minimal"):
                    stats["saved"] += 1
                    _sync_totals["saved"] += 1
                    processed_message_ids.append(m_id)
                    saved_lead_ids.append(result["lead_id"])
                    if conv_id:
                        saved_by_conversation.setdefault(conv_id, set()).add(
                            parse_datetime(msg.get("receivedDateTime"))
                        )
                else:
                    stats["failed"] += 1
                    if not is_retry:
                        _sync_totals["failed"] += 1

                _logged_message_ids.add(m_id)

            if not processed_on_page:
                break

            url = data.get("@odata.nextLink")
            params = None

            if url and pages >= MAX_EMAIL_PAGES and processed_on_page == len(messages):
                hit_page_limit_with_more = True

        if printed_something:
            # Re-read the database so the summary shows what is REALLY stored.
            confirmed_emails = confirmed_leads = 0
            if processed_message_ids:
                confirmed_emails = (
                    db.query(EmailRaw.message_id)
                    .filter(EmailRaw.message_id.in_(processed_message_ids))
                    .count()
                )
            if saved_lead_ids:
                confirmed_leads = (
                    db.query(LeadIntake.id)
                    .filter(LeadIntake.id.in_(saved_lead_ids))
                    .count()
                )
            _print_sync_summary(stats, confirmed_emails, confirmed_leads, pages)

        if hit_page_limit_with_more:
            print(
                f"[WARNING] Every email on the last {MAX_EMAIL_PAGES} pages was new and Outlook has "
                f"more pages. Emails older than these {MAX_EMAIL_PAGES * EMAIL_PAGE_SIZE} are not "
                f"fetched; raise MAX_EMAIL_PAGES to catch up."
            )

        if len(_logged_message_ids) > 20000:
            _logged_message_ids.clear()

    finally:
        db.close()

def send_email(from_email, to_email, subject, body, cc_email=None):
    global ACCESS_TOKEN

    if not ACCESS_TOKEN:
        ACCESS_TOKEN = get_access_token()

    url = f"{GRAPH_BASE_URL}/users/{from_email}/sendMail"

    payload = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "HTML",
                "content": body
            },
            "toRecipients": [
                {"emailAddress": {"address": to_email}}
            ],
            # "ccRecipients": [
            #     {"emailAddress": {"address": cc_email}}
            # ]
        }
    }

    if cc_email:
        payload["message"]["ccRecipients"] = [
            {
                "emailAddress": {
                    "address": cc_email
                }
            }
        ]

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    response = requests.post(url, headers=headers, json=payload, timeout=30)

    if response.status_code == 401:
        ACCESS_TOKEN = get_access_token()
        headers["Authorization"] = f"Bearer {ACCESS_TOKEN}"
        response = requests.post(url, headers=headers, json=payload, timeout=30)

    if response.status_code != 202:
        raise Exception(
            f"Email send failed (HTTP {response.status_code}) from {from_email} "
            f"to {to_email}: {response.text}"
        )


def build_classifier_summary(lead) -> str:
    """Single place that builds the classifier input (used by both classification paths)."""
    return json.dumps(
        {
            "state_of_incident": lead.state_of_incident,
            "case_type": lead.case_type,
            "accident_date": lead.accident_date,
            "fault": lead.fault,
            "injury_severity": lead.injury_severity,
            "medical_treatment": lead.medical_treatment,
            "insurance": lead.insurance,
            "case_description": lead.case_description,
            "nafi_tool_recommendation": lead.nafi_tool_recommendation,
            "nafi_tool_reason": lead.nafi_tool_reason,
            # used by the medical-malpractice time-limit screen (prompt v2.6+)
            "received_date": lead.received_at,
        },
        indent=2,
        default=str,
    )

CLASSIFIER_REVIEWER = "Ramos Law Lead Classification Agent"

async def process_pending_lead_reviews():
    db = SessionLocal()

    try:
        pending_leads = (db.query(LeadIntake).filter(LeadIntake.status == "pending").order_by(LeadIntake.received_at.desc()).all())
        for lead in pending_leads:
            try:
                result = await classify_leads(build_classifier_summary(lead))
                apply_agent_status(
                    db, lead, result["status"], result["decline_type"], result["reason"],
                    CLASSIFIER_REVIEWER,
                )
                await clean_lead_name(lead)

            except Exception as e:
                print(f"Lead {lead.id} review failed: {e}")
        db.commit()

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()

MAX_NEOS_ATTEMPTS = 5
async def process_neos_update_for_leads(lead_ids=None):
    db = SessionLocal()
    processed_count = 0
    failed_count = 0

    try:
        query = db.query(LeadIntake).filter(
            LeadIntake.status.in_(["proceed", "declined"]),
            LeadIntake.neos_updated.is_(False),
            # Neos Direct leads already live in Neos; never write notes back for them.
            neos_writable_clause(LeadIntake),
        )
        if lead_ids is not None:
            query = query.filter(LeadIntake.id.in_(lead_ids))

        leads = query.order_by(LeadIntake.received_at.desc()).all()
        print(f"[Neos] Found {len(leads)} pending leads to process")

        for lead in leads:
            try:
                if lead_neos_writes_blocked(lead):  # belt-and-braces with the query filter
                    continue
                first_name = (lead.first_name or "").strip()
                last_name = (lead.last_name or "").strip()

                if not first_name or not last_name:
                    failed_count += 1
                    print(
                        f"[Neos] Skipping lead {lead.id}: "
                        "first_name or last_name is missing."
                    )
                    continue

                neos_result = await update_neos_lead(
                    first_name=first_name,
                    last_name=last_name,
                    status=lead.status,
                    decline_type=lead.decline_type,
                    reason=lead.reviewer_note,
                )

                lead.neos_updated = True
                lead.neos_case_number = neos_result["case_number"]
                db.commit()
                processed_count += 1
                print(f"[Neos] Updated lead: {lead.id} and Neos case number: {lead.neos_case_number}")

            except Exception as e:
                db.rollback()
                failed_count += 1
                # Persist retry count so it survives closing this session.
                retry_lead = db.query(LeadIntake).filter(LeadIntake.id == lead.id).first()
                if retry_lead:
                    retry_lead.neos_attempt_count = (retry_lead.neos_attempt_count or 0) + 1
                    db.commit()
                    if retry_lead.neos_attempt_count >= MAX_NEOS_ATTEMPTS:
                        print(f"[Neos] Lead {retry_lead.id} hit max retry attempts — needs manual review in Neos.")
                print(f"[Neos] Update failed for lead {lead.id}: {e}")
                traceback.print_exc()

        return {
            "total_found": len(leads),
            "processed_count": processed_count,
            "failed_count": failed_count,
            "skipped_count": len(leads) - processed_count - failed_count,
        }

    finally:
        db.close()

async def process_neos_status_updates():
    db = SessionLocal()
    try:
        leads = (
            db.query(LeadIntake)
            .filter(LeadIntake.neos_updated == True)
            .all()
        )
        print(f"[Neos Status] Found {len(leads)} leads to update")

        for lead in leads:
            try:
                neos_data = await get_lead_status(
                    lead.first_name,
                    lead.last_name
                )

                if not neos_data:
                    continue

                new_status = (
                    f"{neos_data['intake_stage']} | "
                    f"{neos_data['intake_status']}"
                )

                new_assigned = neos_data.get("assigned")

                updated = False

                # Update status 
                if lead.neos_status != new_status:
                    lead.neos_status = new_status
                    updated = True

                # Update assigned
                if lead.assigned_to != new_assigned:
                    lead.assigned_to = new_assigned
                    updated = True

                if updated:
                    db.commit()
                    print(f"[Neos] Lead {lead.id} updated successfully.")
                else:
                    print(f"[Neos] Lead {lead.id} already up to date.")

            except Exception as e:
                db.rollback()
                print(f"[Neos] Failed for lead {lead.id}: {e}")

    finally:
        db.close()

# Background workers to periodically check for new emails every 30 seconds
async def get_latest_leads():
    while True:

        print("Checking for new emails in outlook...")
        try:
            await process_emails()
        except Exception as e:
            print("Worker error:", e)

        print("classifying for pending leads in intakeops...")
        try:
            await process_pending_lead_reviews()
        except Exception as e:
            print("Lead review worker error:", e)

        await asyncio.sleep(30) 

# background worker: runs the NEOS sync twice a day (midnight and afternoon, Denver time)
MST = ZoneInfo("America/Denver")

# Edit this list to change when the sync runs (24-hour clock, Denver time).
NEOS_RUN_TIMES = [time(0, 0), time(12, 0)]     # 12:00 AM (midnight) and 12:00 PM (noon)


def _next_neos_run(after: datetime) -> datetime:
    """Earliest scheduled run time strictly after `after` (timezone-aware, Denver)."""
    for day_offset in (0, 1):
        day = (after + timedelta(days=day_offset)).date()
        for t in sorted(NEOS_RUN_TIMES):
            candidate = datetime.combine(day, t, tzinfo=MST)
            if candidate > after:
                return candidate
    raise RuntimeError("NEOS_RUN_TIMES is empty")


async def neos_daily_status():
    last_run = None
    while True:
        now = datetime.now(MST)
        next_run = _next_neos_run(max(now, last_run) if last_run else now)

        sleep_seconds = max((next_run - now).total_seconds(), 0)

        print(
            f"[Neos Daily Worker] "
            f"Sleeping until {next_run.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )

        await asyncio.sleep(sleep_seconds)
        last_run = next_run

        print(f"[Neos Daily Worker] Running scheduled sync ({next_run.strftime('%H:%M %Z')})...")

        try:
            await process_neos_update_for_leads()
        except Exception as e:
            print(f"[Neos Worker] {e}")

        try:
            await process_neos_status_updates()
        except Exception as e:
            print(f"[Neos Status Worker] {e}")


async def process_lead_reviews(
    start_date: date,
    end_date: date,
):
    db = SessionLocal()

    try:
        start_datetime = datetime.combine(start_date, time.min)
        end_datetime = datetime.combine(end_date, time.max)

        leads = (
            db.query(LeadIntake)
            .filter(
                LeadIntake.received_at >= start_datetime,
                LeadIntake.received_at <= end_datetime,
                # Neos Direct leads stay not_scored until scoring is decided for them.
                not_neos_direct_clause(LeadIntake),
            )
            .order_by(LeadIntake.received_at.desc())
            .all()
        )

        processed = 0
        flipped = 0

        for lead in leads:
            try:
                result = await classify_leads(build_classifier_summary(lead))
                if apply_agent_status(
                    db, lead, result["status"], result["decline_type"], result["reason"],
                    CLASSIFIER_REVIEWER,
                ):
                    flipped += 1
                await clean_lead_name(lead)

                processed += 1

            except Exception as e:
                print(f"Lead {lead.id} review failed: {e}")

        db.commit()

        return {
            "success": True,
            "processed": processed,
            "status_changed": flipped,
            "prompt_version": current_prompt_version(),
            "total": len(leads),
        }

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()

def apply_agreement_sent(lead, neos_status: str | None) -> bool:
    if lead.agreement_sent:
        return False
    if neos_status and "agreement sent" in neos_status.lower():
        lead.agreement_sent = True
        lead.agreement_sent_at = datetime.utcnow()
        return True
    return False