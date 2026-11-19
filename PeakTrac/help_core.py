from __future__ import annotations

import json
import math
import re
from typing import Any

MAX_HISTORY = 12
MAX_MSG_CHARS = 2_000
REDACTED = "[REDACTED]"

_PERSONAL_PATTERNS = (
    # Explicitly labelled personal/client identifiers. These are heuristic safeguards.
    re.compile(r"(?i)\b(?:date of birth|birth date|dob)\s*(?:is|=|:)?\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"),
    re.compile(r"(?i:(?:\bclient|patient|claimant)\s+name\s*(?:is|=|:)?\s*)[A-Z][a-z'’-]+(?:\s+[A-Z][a-z'’-]+){0,2}"),
    re.compile(r"(?i:(?:\bmy name is|patient is|client is)\s+)[A-Z][a-z'’-]+(?:\s+[A-Z][a-z'’-]+){0,2}"),
    re.compile(r"(?i)\b(?:diagnosed with|diagnosis|medical condition|medical history|health condition)\s*(?:is|was|=|:)?\s*[^.!?\n]{1,180}"),
)

_SECRET_PATTERNS = (
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                         # SSN
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}=?"),       # bearer token
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),                    # common API key
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b(pass(?:word|wd|code)?|pwd)\s*(?:is|was|=|:)\s*[\"']?[^\s\"']{3,}"),
    re.compile(r"(?i)\b(otp|verification\s+code|pin)\s*(?:is|=|:)?\s*\d{4,8}\b"),
    re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"),               # payment-card-like number
    re.compile(r"(?<!\d)\d{9,17}(?!\d)"),                        # bank/routing-like number
)

_INJECTION_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"\bignore\s+(?:(?:all|any|your|the|previous|prior|above)\s+)+(?:instructions|rules|prompts?)\b",
    r"\bdisregard\s+(?:(?:all|your|the|previous)\s+)+(?:instructions|rules)\b",
    r"\b(?:admin|developer|debug|test|god|dan)\s+mode\b",
    r"\b(?:reveal|show|print|repeat|output)\b.{0,50}\b(?:system\s+prompt|hidden\s+(?:rules|instructions)|playbook)\b",
    r"\byou\s+are\s+now\b",
    r"\bact\s+as\b",
    r"\bjailbreak\b",
    r"\b(?:forget|override|bypass)\s+(?:(?:all|your|the)\s+)*(?:rules|instructions|restrictions|guardrails)\b",
    r"\bi\s*(?:am|'m)\s+the\s+(?:developer|admin(?:istrator)?|owner)\b",
))

CATEGORIES = {"bug", "unexpected_behavior", "how_to_question", "unanswered_question", "feature_request", "security_concern"}
SEVERITIES = {"critical", "high", "medium", "low"}
AREAS = {"login", "overview", "case_accounts", "open_neos_import", "draw_request_form", "draw_approval", "draw_history", "firm_line_of_credit", "firm_draw", "analytics", "export_csv", "projections", "tutorial", "mobile", "other"}
ROLES = {"attorney": "Attorney", "paralegal": "Paralegal", "accounting": "Accounting", "admin": "Admin", "other": "Other"}
FALLBACK_REPLY = ("Sorry, I had trouble with that. Could you rephrase your question? "
                  "If it keeps happening, please contact your PeakTrac administrator.")


# ---- language detection (done in code, so the model is TOLD the language instead of guessing it) ----------------
_ES_WORDS = frozenset("""el la los las un una unos unas del que qué como cómo para por sin es son está están puedo puede
quiero necesito hola gracias donde dónde cuál cuales cuándo cuando hacer hago pido tengo tiene pero muy más también
favor ayuda explícame explica sobre esto esta este ese eso porque cuenta cuentas caso casos hay dime muéstrame
sección pestaña haga clic elija escriba abogado solicitud""".split())
_EN_WORDS = frozenset("""the is are what how do does can could i my to of and in it you this that why where when please
with for on not have has was explain me about show tell need want help should would will we our your there here if
or but so an a be been""".split())
_ES_CHARS = re.compile(r"[¿¡ñáéíóúü]", re.I)
_WORD = re.compile(r"[a-záéíóúüñ]+", re.I)


def detect_language(text: str) -> str:
    """Return 'es' only when the text is clearly Spanish; otherwise 'en' (ambiguous or misspelled -> English)."""
    text = (text or "").lower()
    if "¿" in text or "¡" in text:
        return "es"
    words = _WORD.findall(text)
    es = sum(1 for w in words if w in _ES_WORDS) + (2 if _ES_CHARS.search(text) else 0)
    en = sum(1 for w in words if w in _EN_WORDS)
    return "es" if es >= 2 and es > en else "en"


INTENTS = {"how_to", "problem", "feature_request", "out_of_scope", "smalltalk", "security"}


def redact(value: Any) -> tuple[str, bool]:
    """Redact common secrets while preserving explicitly labelled PeakTrac record IDs.

    This is heuristic redaction, not a substitute for a proper DLP control.
    """
    if value is None:
        return "", False
    text = str(value)
    found = False
    # The playbook explicitly asks users for these identifiers. Protect labelled references
    # from the broad long-number heuristic, then restore the sanitized reference text.
    references: list[str] = []
    reference_pattern = re.compile(
        r"(?i)\b(?:case\s*#?|account\s*id|draw\s*id)\s*[:#-]?\s*[A-Za-z0-9][A-Za-z0-9_-]{0,31}"
    )
    def protect(match: re.Match) -> str:
        references.append(match.group(0))
        return f"__PEAKTRAC_REF_{len(references) - 1}__"
    text = reference_pattern.sub(protect, text)
    for pattern in (*_SECRET_PATTERNS, *_PERSONAL_PATTERNS):
        text, count = pattern.subn(REDACTED, text)
        found = found or count > 0
    for index, reference in enumerate(references):
        text = text.replace(f"__PEAKTRAC_REF_{index}__", reference)
    return text, found


def looks_like_injection(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _INJECTION_PATTERNS)


def sanitize_history(messages: Any) -> tuple[list[dict[str, str]], dict[str, bool]]:
    """Allow only user/assistant text; redact before sending any history to the model."""
    flags = {"contains_secret": False, "contains_redactions": False, "injection_attempt": False}
    if not isinstance(messages, (list, tuple)):
        return [], flags

    clean: list[dict[str, str]] = []
    for message in messages[-MAX_HISTORY:]:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        content = content.strip()[:MAX_MSG_CHARS]
        if not content:
            continue
        if message["role"] == "user":
            # This flag describes the newest user turn, not any earlier turn replayed in history.
            flags["injection_attempt"] = looks_like_injection(content)
        content, found = redact(content)
        flags["contains_secret"] |= found
        flags["contains_redactions"] |= found
        clean.append({"role": message["role"], "content": content})

    # Discard leading assistant messages so the submitted history starts with a user turn.
    while clean and clean[0]["role"] != "user":
        clean.pop(0)
    return clean, flags


def _text(value: Any, limit: int = 1000) -> str:
    text, _ = redact(value)
    return text[:limit]


def _string_list(value: Any, limit: int = 20) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item, 300) for item in value[:limit] if item not in (None, "")]


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return False


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").replace("$", "").strip())
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def normalize_log(log: Any, user_email: str | None = None) -> dict | None:
    if not isinstance(log, dict):
        return None

    def choice(key: str, allowed: set[str], default: str) -> str:
        value = log.get(key)
        value = value.strip().lower() if isinstance(value, str) else ""
        return value if value in allowed else default

    category = choice("category", CATEGORIES, "unanswered_question")
    summary = _text(log.get("summary"), 300)
    what = _text(log.get("what_happened"), 2000)

    if not summary and not what and category != "how_to_question":
        return None

    language = choice("language", {"en", "es"}, "en")

    return {
        "user_role": ROLES.get(str(log.get("user_role", "")).strip().lower(), "Other"),
        "language": language,
        "category": category,
        "severity": choice("severity", SEVERITIES, "low"),
        "area": choice("area", AREAS, "other"),
        "record_reference": _text(log.get("record_reference"), 100) or None,
        "amount_shown": _number(log.get("amount_shown")),
        "amount_expected": _number(log.get("amount_expected")),
        "summary": summary,
        "what_happened": what,
        "expected": _text(log.get("expected"), 2000),
        "steps": _string_list(log.get("steps")),
        "error_message": _text(log.get("error_message"), 1000),
        "device_browser": _text(log.get("device_browser"), 200),
        "user_blocked": _boolean(log.get("user_blocked")),
        "troubleshooting_tried": _string_list(
            log.get("troubleshooting_tried")
        ),
        "assistant_answer_given": _text(
            log.get("assistant_answer_given"), 1000
        ),
        "contains_redactions": (
            _boolean(log.get("contains_redactions"))
            or REDACTED in json.dumps(log, default=str)
        ),
        "contains_secret": (
            _boolean(log.get("contains_secret"))
            or REDACTED in json.dumps(log, default=str)
        ),
        "injection_attempt": _boolean(log.get("injection_attempt")),
    }

def parse_model_output(raw: Any, user_email: str | None = None,) -> dict:
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, dict):
            raise ValueError("Expected JSON object")
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"reply": FALLBACK_REPLY, "log_action": "none", "log": None, "intent": None}

    reply = data.get("reply")
    reply = reply.strip() if isinstance(reply, str) and reply.strip() else FALLBACK_REPLY
    reply, _ = redact(reply)
    requested_action = data.get("log_action")
    action = requested_action if requested_action in {"create", "update"} else "none"
    report = (
        normalize_log(data.get("log"), user_email=user_email)
        if action != "none"
        else None
    )
    if report is None:
        action = "none"
    intent = data.get("intent")
    intent = intent.strip().lower() if isinstance(intent, str) else None
    return {"reply": reply[:4000], "log_action": action, "log": report,
            "intent": intent if intent in INTENTS else None}