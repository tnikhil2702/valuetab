
import json
import os
import re
from datetime import datetime, timezone

from db import LeadStatusHistory
import geonamescache


# ============================================================
# PART 1 - NAME VALIDATION AND LOCATION DETECTION
# ============================================================

_gc = geonamescache.GeonamesCache()
US_STATES = _gc.get_us_states()

STATE_CODE_TO_NAME = {}

for key, state in US_STATES.items():
    code = (
        state.get("code")
        or state.get("state")
        or state.get("adminCode1")
        or key.split("-")[-1]
    )

    STATE_CODE_TO_NAME[str(code).upper()] = state["name"]


STATE_NAMES = {
    state["name"].lower(): state["name"]
    for state in US_STATES.values()
}


# City -> set of state names
CITY_TO_STATES = {}

for city in _gc.get_cities().values():

    if city.get("countrycode") != "US":
        continue

    state_code = str(
        city.get("admin1code") or ""
    ).split("-")[-1].upper()

    state_name = STATE_CODE_TO_NAME.get(state_code)

    if state_name:
        CITY_TO_STATES.setdefault(
            city["name"].casefold(), set()
        ).add(state_name)


EMPTY_VALUES = {
    "NA",
    "N/A",
    "NONE",
    "NULL",
    "UNKNOWN",
    "CALLER DECLINED",
}


PLACEHOLDER_STRONG = {
    "unavailable",
    "anonymous",
    "unknown",
    "caller",
    "wireless",
    "blocked",
    "restricted",
    "withheld",
    "unidentified",
    "unspecified",
    "voicemail",
    "landline",
}


PLACEHOLDER_WEAK = {
    "private",
    "mobile",
    "cell",
    "cellular",
    "phone",
    "number",
    "name",
    "test",
    "testing",
    "none",
    "null",
    "na",
    "not",
    "provided",
    "available",
    "declined",
    "customer",
    "client",
    "lead",
    "no",
    "out",
    "of",
    "area",
    "toll",
    "free",
    "user",
    "person",
    "someone",
    "nobody",
}


_NAME_TOKEN_RE = re.compile(
    r"[^\W\d_]+(?:['’\-.][^\W\d_]+)*\.?",
    re.UNICODE,
)


def is_city_or_state_name(value: str) -> bool:
    """
    Detect location-like values, including:

        PHOENIX AZ
        PHOENIX, AZ
        AZ
        ARIZONA
        DENVER, CO
        CO DENVER
        FORT COLLINS
        CHEYENNE WY

    Uses GeoNamesCache for US city/state identification.
    """

    if not value:
        return False

    candidate = re.sub(
        r"\s+",
        " ",
        str(value),
    ).strip(" ,.-")

    low = candidate.casefold()
    upper = candidate.upper()

    # Standalone state abbreviation.
    if upper in STATE_CODE_TO_NAME:
        return True

    # Standalone state name.
    if low in STATE_NAMES:
        return True

    # City, ST or City ST.
    match = re.fullmatch(
        r"([A-Za-z .'-]+?)\s*,?\s+([A-Za-z]{2})",
        candidate,
    )

    if match:

        city = match.group(1).strip().casefold()
        code = match.group(2).upper()

        if code in STATE_CODE_TO_NAME:

            state_name = STATE_CODE_TO_NAME[code]

            if state_name in CITY_TO_STATES.get(city, set()):
                return True

    # Reversed ST CITY.
    rev = re.fullmatch(
        r"([A-Za-z]{2})\s+([A-Za-z .'-]+)",
        candidate,
    )

    if rev:

        code = rev.group(1).upper()
        city = rev.group(2).strip().casefold()

        if code in STATE_CODE_TO_NAME:

            state_name = STATE_CODE_TO_NAME[code]

            if state_name in CITY_TO_STATES.get(city, set()):
                return True

    # Standalone city name.
    if low in CITY_TO_STATES:
        return True

    return False


def is_placeholder_name(value: str) -> bool:
    """Detect placeholders such as Wireless Caller or No Name."""

    words = [
        word
        for word in re.split(
            r"[^A-Za-z]+",
            str(value or "").lower(),
        )
        if word
    ]

    if not words:
        return True

    if any(word in PLACEHOLDER_STRONG for word in words):
        return True

    return all(
        word in PLACEHOLDER_WEAK
        for word in words
    )


def is_plausible_person_name(value: str) -> bool:
    """Conservative check that a value looks like a person's name."""

    if not value:
        return False

    value = re.sub(
        r"\s+",
        " ",
        str(value),
    ).strip(" ,;:.")

    if not value or len(value) > 80:
        return False

    if any(char.isdigit() for char in value):
        return False

    if value.upper() in EMPTY_VALUES:
        return False

    if is_city_or_state_name(value):
        return False

    if is_placeholder_name(value):
        return False

    tokens = value.replace(",", " ").split()

    return (
        1 <= len(tokens) <= 5
        and all(
            _NAME_TOKEN_RE.fullmatch(token)
            for token in tokens
        )
    )


def has_valid_name(first_name, last_name) -> bool:
    full = " ".join(
        str(part).strip()
        for part in (first_name, last_name)
        if part and str(part).strip()
    )

    return is_plausible_person_name(full)


def split_full_name(full):
    if not full:
        return None, None

    full = full.strip()

    if is_city_or_state_name(full):
        return None, None

    if "," in full:

        # "MEDINA,CORINA" -> first=CORINA, last=MEDINA
        last, _, first = full.partition(",")

        return (
            first.strip() or None,
            last.strip() or None,
        )

    parts = full.split()

    if len(parts) == 1:
        return parts[0], None

    return parts[0], " ".join(parts[1:])


# ============================================================
# PART 2 - LLM NAME EXTRACTION
# ============================================================

NAME_MODEL = "gpt-4.1-mini"

MAX_DESCRIPTION_CHARS_FOR_NAME = 6000


NAME_EXTRACTION_SYSTEM = """
You extract the full name of the actual prospective client or claimant
from a law-firm intake summary or call summary.

Your task is to identify WHO NEEDS LEGAL REPRESENTATION.

IMPORTANT RULES:

1. If the caller explicitly says they are seeking representation for
   themselves, the caller is the client.

2. If someone calls on behalf of another person, identify that other
   person as the client when their full name is stated.

3. Do not automatically assume that the caller is the client.

4. If the caller is a relative, spouse, parent, friend, or representative,
   distinguish that person from the actual client.

5. Never return a defendant, other driver, seller, witness, employer,
   doctor, hospital employee, police officer, insurance adjuster,
   attorney, or Ramos Law employee as the client.

6. Only return a first and last name literally written in the description.

7. Never guess or construct a name from an email address or phone number.

8. Never return a city, state, ZIP code, business name, or placeholder.

9. If the actual client's identity cannot be established, return null
   for both names.

EXAMPLES:

Description:
"Kerry Ratliff called seeking legal representation for herself."

Return:
{"first_name": "Kerry", "last_name": "Ratliff"}

Description:
"Chalmer Wren called on behalf of his client Kylee Wasechek."

Return:
{"first_name": "Kylee", "last_name": "Wasechek"}

Description:
"John Smith called regarding an accident involving another person.
The actual client's name is Mary Jones."

Return:
{"first_name": "Mary", "last_name": "Jones"}

Description:
"John called regarding an accident. The description does not identify
who needs legal representation."

Return:
{"first_name": null, "last_name": null}

Return JSON only:
{"first_name": string or null, "last_name": string or null}
"""


_name_client = None


def _get_name_client():
    global _name_client

    if _name_client is None:

        from openai import AsyncOpenAI

        api_key = os.getenv("OPENAI_API_KEY")

        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not configured"
            )

        _name_client = AsyncOpenAI(
            api_key=api_key
        )

    return _name_client


def _clean_part(value):

    if not value or not isinstance(value, str):
        return None

    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip(" ,;:")

    return value or None


def sanitize_name(first_name, last_name):

    first = _clean_part(first_name)
    last = _clean_part(last_name)

    if (first or last) and has_valid_name(first, last):
        return first, last

    return None, None


def _name_appears_in_text(
    first,
    last,
    text: str,
) -> bool:

    low = text.casefold()

    for part in (first, last):

        for token in re.split(
            r"\s+",
            part or "",
        ):

            token = token.strip(" .,").casefold()

            if token and not re.search(
                r"(?<!\w)"
                + re.escape(token)
                + r"(?!\w)",
                low,
            ):
                return False

    return True


def _parse_llm_name(raw: str):

    raw = (raw or "").strip()

    raw = re.sub(
        r"^```(?:json)?|```$",
        "",
        raw,
        flags=re.I | re.M,
    ).strip()

    data = json.loads(raw)

    if not isinstance(data, dict):
        return None, None

    return (
        _clean_part(data.get("first_name")),
        _clean_part(data.get("last_name")),
    )


async def extract_name_with_llm(description: str):
    """
    Extract the actual client's full name.

    Returns:
        (first_name, last_name)

    Returns (None, None) when no verified client name is available.
    """

    text = (description or "").strip()

    if len(text) < 5:
        return None, None

    text = text[:MAX_DESCRIPTION_CHARS_FOR_NAME]

    try:

        response = await _get_name_client().chat.completions.create(
            model=NAME_MODEL,
            temperature=0,
            timeout=30,
            response_format={
                "type": "json_object"
            },
            messages=[
                {
                    "role": "system",
                    "content": NAME_EXTRACTION_SYSTEM,
                },
                {
                    "role": "user",
                    "content": text,
                },
            ],
        )

        first, last = _parse_llm_name(
            response.choices[0].message.content
        )

    except Exception as exc:

        print(
            f"[name-llm] extraction failed: {exc}"
        )

        return None, None

    if not first or not last:
        return None, None

    first, last = sanitize_name(
        first,
        last,
    )

    if not first or not last:

        print(
            "[name-llm] rejected invalid extracted name"
        )

        return None, None

    if not _name_appears_in_text(
        first,
        last,
        text,
    ):

        print(
            "[name-llm] rejected name not found in description: "
            f"{first!r} {last!r}"
        )

        return None, None

    print(
        f"[name-llm] verified client name: {first} {last}"
    )

    return first, last


# ============================================================
# PART 2B - AUTOMATIC NAME MISMATCH SCREENING
# ============================================================

def _normalize_person_name(first_name, last_name=None):
    """
    Normalize names for comparison.

    Example:
        "KERRY", "RATLIFF" -> "kerryratliff"
        "Kerry", "Ratliff" -> "kerryratliff"
    """

    full = " ".join(
        str(part).strip()
        for part in (first_name, last_name)
        if part and str(part).strip()
    )

    return re.sub(
        r"[^a-z]",
        "",
        full.casefold(),
    )


def _existing_name_is_invalid(first, last):
    """
    Detect invalid existing database names.

    Examples:
        Phoenix AZ
        Arizona
        Denver CO
        Wireless Caller
        Unknown
    """

    full = " ".join(
        part
        for part in (first, last)
        if part
    ).strip()

    if not full:
        return True

    if is_city_or_state_name(full):
        return True

    if is_placeholder_name(full):
        return True

    if not has_valid_name(first, last):
        return True

    return False


def _store_name_screen_result(
    lead,
    result: dict,
    original_name: str = "",
) -> None:
    """
    Save screening information on mapped lead fields.

    Manual review is always disabled.
    """

    if hasattr(lead, "name_check"):
        lead.name_check = result["action"]

    if hasattr(lead, "name_check_reason"):
        lead.name_check_reason = result["reason"]

    if hasattr(lead, "needs_name_review"):
        lead.needs_name_review = False


async def screen_lead_name_mismatch(lead) -> dict:
    """Extract the client's name from the case description.

    A verified name from the description always takes priority over the
    current database name. If extraction is unsuccessful, the existing
    first_name and last_name are left exactly as they are.
    """

    original_first = getattr(lead, "first_name", None)
    original_last = getattr(lead, "last_name", None)
    description = str(
        getattr(lead, "case_description", None) or ""
    ).strip()
    lead_id = getattr(lead, "id", None)

    original_name = " ".join(
        str(part).strip()
        for part in (original_first, original_last)
        if part is not None and str(part).strip()
    )

    def finish(action: str, reason: str) -> dict:
        result = {
            "action": action,
            "first_name": getattr(lead, "first_name", None),
            "last_name": getattr(lead, "last_name", None),
            "caller_name": None,
            "reason": reason,
        }
        _store_name_screen_result(lead, result, original_name)
        return result

    if not description:
        print(
            "[name-screen][KEEP] "
            f"lead_id={lead_id} name={original_name!r}; "
            "case description is blank"
        )
        return finish(
            "skip",
            "Case description is blank. Existing first and last names were preserved.",
        )

    # Always attempt extraction from the case description. Do not use the
    # existing first_name/last_name to decide whether extraction is needed.
    extracted_first, extracted_last = await extract_name_with_llm(description)

    if not extracted_first or not extracted_last:
        print(
            "[name-screen][KEEP] "
            f"lead_id={lead_id} name={original_name!r}; "
            "no verified client name found in description"
        )
        return finish(
            "ok",
            "No verified client name was identified in the case description. "
            "Existing first and last names were preserved exactly as they were.",
        )

    # extract_name_with_llm already validates the result, but keep this
    # defensive validation before writing to the lead record.
    extracted_first, extracted_last = sanitize_name(
        extracted_first,
        extracted_last,
    )

    if not extracted_first or not extracted_last:
        return finish(
            "ok",
            "The extracted name did not pass validation. Existing first and last names were preserved.",
        )

    if not _name_appears_in_text(
        extracted_first,
        extracted_last,
        description,
    ):
        return finish(
            "ok",
            "The extracted name could not be verified in the case description. "
            "Existing first and last names were preserved.",
        )

    # Replace both fields whenever the description provides a verified name.
    lead.first_name = extracted_first
    lead.last_name = extracted_last

    new_name = f"{extracted_first} {extracted_last}".strip()
    old_normalized = _normalize_person_name(original_first, original_last)
    new_normalized = _normalize_person_name(extracted_first, extracted_last)

    if old_normalized != new_normalized:
        print(
            "[name-screen][REPLACE] "
            f"lead_id={lead_id} old={original_name!r} new={new_name!r}"
        )
        return finish(
            "replace_name",
            f"Existing name '{original_name or 'blank'}' was replaced with "
            f"'{new_name}', verified from the case description.",
        )

    print(
        "[name-screen][MATCH] "
        f"lead_id={lead_id} name={new_name!r}"
    )
    return finish(
        "ok",
        f"The existing name matches the verified client name '{new_name}' "
        "in the case description.",
    )


# PART 3 - NORMAL NAME CLEANUP

# Declined leads remain excluded from the normal live extraction workflow.
LLM_NAME_STATUSES = (
    "proceed",
    "escalated",
)


async def finalize_lead_name(
    lead,
    allow_declined=False,
) -> str:
    """Use the case description as the source of truth for the client name.

    If a verified client name is found, replace the database name. If no
    verified name is found, preserve the existing values without clearing,
    sanitizing, or otherwise modifying them.

    allow_declined is retained for compatibility with existing callers.
    Name extraction now follows the same rule for every lead status.
    """

    original_first = getattr(lead, "first_name", None)
    original_last = getattr(lead, "last_name", None)
    description = str(
        getattr(lead, "case_description", None) or ""
    ).strip()
    had_existing_name = bool(
        (str(original_first).strip() if original_first is not None else "")
        or (str(original_last).strip() if original_last is not None else "")
    )

    if not description:
        print(
            "[name-finalize][KEEP] "
            f"lead_id={getattr(lead, 'id', None)}; "
            "case description is blank"
        )
        return "kept" if had_existing_name else "skipped"

    first, last = await extract_name_with_llm(description)

    if not first or not last:
        print(
            "[name-finalize][KEEP] "
            f"lead_id={getattr(lead, 'id', None)}; "
            "no verified name found in case description"
        )
        return "kept" if had_existing_name else "llm_no_name"

    # Defensive validation; extraction also performs these checks.
    first, last = sanitize_name(first, last)
    if not first or not last:
        return "kept" if had_existing_name else "llm_no_name"

    if not _name_appears_in_text(first, last, description):
        return "kept" if had_existing_name else "llm_no_name"

    lead.first_name = first
    lead.last_name = last

    print(
        "[name-finalize][REPLACE] "
        f"lead_id={getattr(lead, 'id', None)} "
        f"old={original_first!r} {original_last!r} "
        f"new={first!r} {last!r}"
    )
    return "llm_extracted"


# ============================================================
# PART 4 - REVIEWER DECISIONS
# ============================================================

DECISIONS = (
    "proceed",
    "declined",
)

RE_REVIEW = "Re-review"

BACKFILL_VERSION = "v2.10"


def current_prompt_version() -> str:
    return os.getenv(
        "LEAD_PROMPT_VERSION",
        "v2.11",
    )


def opposite(status: str) -> str:
    return (
        "declined"
        if status == "proceed"
        else "proceed"
    )


def _anchor(lead):
    return lead.status


def derive_agree_disagree(lead):

    decision = getattr(
        lead,
        "reviewer_decision",
        None,
    )

    if decision not in DECISIONS:
        return None

    anchor = _anchor(lead)

    if anchor not in DECISIONS:
        return RE_REVIEW

    return (
        "Agree"
        if decision == anchor
        else "Disagree"
    )


def derive_agree_disagree_at_answer(lead):

    decision = getattr(
        lead,
        "reviewer_decision",
        None,
    )

    at_answer = getattr(
        lead,
        "agent_status_at_answer",
        None,
    )

    if (
        decision not in DECISIONS
        or at_answer not in DECISIONS
    ):
        return None

    return (
        "Agree"
        if decision == at_answer
        else "Disagree"
    )


def record_reviewer_answer(
    lead,
    choice: str,
    now: datetime | None = None,
    answered_by: str | None = None,
) -> None:
    anchor = _anchor(lead)

    if anchor not in DECISIONS:
        raise ValueError(
            f"Agent status is '{lead.status}'. "
            "Agree/Disagree only applies to proceed or declined."
        )

    if choice not in ("Agree", "Disagree"):
        raise ValueError("choice must be 'Agree' or 'Disagree'")

    lead.reviewer_decision = (
        anchor if choice == "Agree" else opposite(anchor)
    )
    lead.reviewer_answer_at = now or datetime.now(timezone.utc)
    lead.agent_status_at_answer = anchor
    lead.agent_version_at_answer = current_prompt_version()

    # NEW: Save Agree/Disagree in the database column.
    lead.agree_disagree = derive_agree_disagree_at_answer(lead)

    if answered_by and hasattr(lead, "reviewer_answered_by"):
        lead.reviewer_answered_by = answered_by

def backfill_reviewer_decision(
    lead,
    version: str = BACKFILL_VERSION,
) -> str:

    stored = getattr(
        lead,
        "agree_disagree",
        None,
    )

    if getattr(
        lead,
        "reviewer_decision",
        None,
    ) in DECISIONS:

        return "already_done"

    if stored not in (
        "Agree",
        "Disagree",
    ):

        return "no_answer"

    anchor = _anchor(lead)

    if anchor not in DECISIONS:
        return "cannot_convert"

    lead.reviewer_decision = (
        anchor
        if stored == "Agree"
        else opposite(anchor)
    )

    lead.agent_status_at_answer = anchor
    lead.agent_version_at_answer = version

    return "converted"


# ============================================================
# PART 5 - STATUS HISTORY
# ============================================================

def log_status_change(
    db,
    lead_id,
    old_status,
    new_status,
    prompt_version=None,
    changed_by=None,
    now=None,
):

    db.add(
        LeadStatusHistory(
            lead_id=lead_id,
            old_status=old_status,
            new_status=new_status,
            prompt_version=(
                prompt_version or current_prompt_version()
            ),
            changed_by=changed_by,
            changed_at=(
                now or datetime.now(timezone.utc)
            ),
        )
    )


def apply_agent_status(
    db,
    lead,
    status,
    decline_type,
    reason,
    reviewed_by,
    prompt_version=None,
    now=None,
) -> bool:

    now = now or datetime.now(timezone.utc)

    old_status = lead.status

    lead.status = status

    lead.decline_type = (
        decline_type
        if status == "declined"
        else None
    )

    lead.reviewer_note = reason
    lead.reviewed_by = reviewed_by
    lead.reviewed_at = now

    changed = old_status != status

    if changed:

        log_status_change(
            db,
            lead.id,
            old_status,
            status,
            prompt_version=prompt_version,
            changed_by=reviewed_by,
            now=now,
        )

    return changed


# ============================================================
# PART 6 - AGREEMENT SUMMARY
# ============================================================

def _rate(agree: int, total: int):

    return (
        round(agree / total * 100, 1)
        if total
        else None
    )


def agreement_summary(leads) -> dict:

    answered = [
        lead
        for lead in leads
        if getattr(
            lead,
            "reviewer_decision",
            None,
        ) in DECISIONS
    ]

    by_version = {}

    for lead in answered:

        if lead.agent_status_at_answer not in DECISIONS:
            continue

        version = (
            lead.agent_version_at_answer
            or "unknown"
        )

        row = by_version.setdefault(
            version,
            {
                "answered": 0,
                "agree": 0,
            },
        )

        row["answered"] += 1

        row["agree"] += int(
            lead.reviewer_decision
            == lead.agent_status_at_answer
        )

    for row in by_version.values():

        row["agreement_pct"] = _rate(
            row["agree"],
            row["answered"],
        )

    at_total = sum(
        row["answered"]
        for row in by_version.values()
    )

    at_agree = sum(
        row["agree"]
        for row in by_version.values()
    )

    comparable = [
        lead
        for lead in answered
        if lead.status in DECISIONS
    ]

    cur_agree = sum(
        1
        for lead in comparable
        if lead.reviewer_decision == lead.status
    )

    return {
        "current_prompt_version": current_prompt_version(),

        "answered": len(answered),

        "against_agent_at_answer": {
            "answered": at_total,
            "agree": at_agree,
            "agreement_pct": _rate(
                at_agree,
                at_total,
            ),
            "by_prompt_version": by_version,
        },

        "against_current_agent": {
            "prompt_version": current_prompt_version(),
            "compared": len(comparable),
            "agree": cur_agree,
            "agreement_pct": _rate(
                cur_agree,
                len(comparable),
            ),
            "re_review": len(answered) - len(comparable),
        },

        "status_flipped_since_answer": sum(
            1
            for lead in answered
            if (
                lead.agent_status_at_answer in DECISIONS
                and lead.status != lead.agent_status_at_answer
            )
        ),
    }
