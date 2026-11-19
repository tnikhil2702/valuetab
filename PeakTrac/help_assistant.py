from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from openai import AsyncOpenAI, APIConnectionError, APIError, APITimeoutError
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db import PeakTracHelpReport
import json
import re

from PeakTrac.help_core import detect_language, parse_model_output, sanitize_history
from PeakTrac.help_prompt import REMINDER, build_system_prompt

logger = logging.getLogger("peaktrac.help")
MODEL = "gpt-4o-mini"

def user_email(user: dict) -> str:
    """Identity comes only from the authenticated session, never chat content."""
    if not isinstance(user, dict):
        return "unknown"
    return str(user.get("email") or user.get("username") or user.get("sub") or "unknown")[:320]


def next_report_id(db: Session, now: datetime) -> str:
    prefix = f"PT-HELP-{now:%Y%m%d}-"
    last = (db.query(PeakTracHelpReport.report_id)
            .filter(PeakTracHelpReport.report_id.like(prefix + "%"))
            .order_by(PeakTracHelpReport.report_id.desc()).first())
    try:
        sequence = int(last[0].rsplit("-", 1)[1]) + 1 if last else 1
    except (TypeError, ValueError, IndexError):
        sequence = 1
    return f"{prefix}{sequence:04d}"


def save_report(db: Session, user: dict, conversation_id: str, action: str,
                log_data: dict, flags: dict | None = None) -> str | None:
    """Create one row per report; update only an eligible report in this conversation."""
    if not isinstance(log_data, dict):
        return None

    email = user_email(user).strip().lower()
    if not email or email == "unknown":
        logger.warning("Not saving help report because authenticated user email is missing")
        return None

    flags = flags if isinstance(flags, dict) else {}
    conversation_id = str(conversation_id or "")[:64] or None
    category = str(log_data.get("category") or "unanswered_question").strip().lower()[:32]
    severity = str(log_data.get("severity") or "low").strip().lower()[:16]
    allowed_categories = {
        "bug", "unexpected_behavior", "how_to_question",
        "unanswered_question", "feature_request", "security_concern",
    }
    allowed_severities = {"critical", "high", "medium", "low"}
    if category not in allowed_categories:
        category = "unanswered_question"
    if severity not in allowed_severities:
        severity = "low"

    # The JSON record has already passed through parse_model_output/normalize_log.
    # Never copy the original unredacted user message into the database.
    safe_record = dict(log_data)
    summary = str(safe_record.get("summary") or safe_record.get("what_happened") or "").strip()[:2000]
    if not summary:
        return None

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    safe_record.update({
        "category": category,
        "severity": severity,
        "conversation_id": conversation_id,
        "created_at": timestamp,
        "updated_at": timestamp,
        "contains_redactions": bool(safe_record.get("contains_redactions") or flags.get("contains_redactions")),
        "contains_secret": bool(safe_record.get("contains_secret") or flags.get("contains_secret")),
        "injection_attempt": bool(safe_record.get("injection_attempt") or flags.get("injection_attempt")),
    })

    try:
        row = None
        if action == "update" and category in {"bug", "unexpected_behavior", "feature_request"}:
            query = db.query(PeakTracHelpReport).filter(
                PeakTracHelpReport.user_email == email,
                PeakTracHelpReport.conversation_id == conversation_id,
                PeakTracHelpReport.category == category,
            )
            row = query.order_by(PeakTracHelpReport.id.desc()).first()

        if row is not None:
            existing = dict(row.record or {})
            # Preserve the original reference and creation time; merge newly collected details.
            original_created = existing.get("created_at", timestamp)
            existing.update(safe_record)
            existing["created_at"] = original_created
            existing["updated_at"] = timestamp
            row.record = existing
            row.severity = severity
            db.commit()
            return row.report_id

        report_id = next_report_id(db, now)
        safe_record.update({"report_id": report_id, "created_at": timestamp, "updated_at": timestamp})
        db.add(PeakTracHelpReport(
            report_id=report_id,
            conversation_id=conversation_id,
            created_at=now,
            user_email=email,
            category=category,
            severity=severity,
            status="new",
            record=safe_record,
        ))
        db.commit()
        return report_id
    except IntegrityError:
        db.rollback()
        logger.exception("Could not save help report because of a database integrity conflict")
        return None
    except Exception:
        db.rollback()
        logger.exception("Could not save help report")
        return None


_LANG_NAME = {"en": "English", "es": "Spanish"}


def _language_directive(language: str) -> str:
    name = _LANG_NAME.get(language, "English")
    return (f"LANGUAGE FOR THIS REPLY: {name}. The user's latest message is in {name}. Write the \"reply\" value "
            f"entirely in {name}, even if earlier messages, examples or your own earlier replies used another "
            f"language. Set \"language\" to \"{language}\". Keep PeakTrac tab, button and field names in English.")


def _reply_language(content: str) -> str | None:
    try:
        reply = json.loads(content).get("reply")
        return detect_language(reply) if isinstance(reply, str) else None
    except (ValueError, AttributeError):
        return None


async def _complete(client, messages: list[dict]) -> str:
    response = await client.chat.completions.create(
        model=MODEL, temperature=0.2, max_tokens=1000,
        response_format={"type": "json_object"}, messages=messages)
    content = response.choices[0].message.content if response.choices else None
    if not isinstance(content, str) or not content.strip():
        logger.error("OpenAI returned an empty assistant message")
        raise HTTPException(status_code=502, detail="The help assistant returned an invalid response. Please try again.")
    return content


async def ask_model(history: list[dict], language: str = "en") -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.error("OPENAI_API_KEY is not configured")
        raise HTTPException(status_code=503, detail="The help assistant is not available right now.")

    messages = [{"role": "system", "content": build_system_prompt()}, *history,
                {"role": "system", "content": REMINDER},
                {"role": "system", "content": _language_directive(language)}]  # LAST message = strongest
    try:
        async with AsyncOpenAI(api_key=api_key) as client:
            content = await _complete(client, messages)
            # Safety net: if the model still answered in the wrong language, ask once more.
            if _reply_language(content) not in (None, language):
                logger.warning("Reply language mismatch (expected %s); retrying once", language)
                content = await _complete(client, messages + [{"role": "system", "content": (
                    f"Your previous reply was in the wrong language. Rewrite it entirely in "
                    f"{_LANG_NAME.get(language, 'English')} and return the same JSON structure.")}])
        return content
    except HTTPException:
        raise
    except APITimeoutError:
        logger.warning("OpenAI request timed out")
    except APIConnectionError:
        logger.warning("Could not connect to OpenAI")
    except APIError as exc:
        logger.error("OpenAI API error (status=%s)", getattr(exc, "status_code", None))
    except Exception:
        logger.exception("Unexpected failure calling OpenAI")
    raise HTTPException(status_code=502, detail="The help assistant is temporarily unavailable. Please try again shortly.")


_AREA_HINTS = (("neos", "open_neos_import"), ("import", "open_neos_import"), ("draw request", "draw_request_form"),
               ("approve", "draw_approval"), ("deny", "draw_approval"), ("history", "draw_history"),
               ("firm", "firm_line_of_credit"), ("export", "export_csv"), ("analytic", "analytics"),
               ("projection", "projections"), ("tutorial", "tutorial"), ("overview", "overview"),
               ("password", "login"), ("sign in", "login"), ("login", "login"), ("otp", "login"),
               ("case account", "case_accounts"), ("draw", "draw_request_form"))


def how_to_log(question: str, reply: str, language: str) -> dict:
    """Deterministic how-to record, used when the model classified the turn as how_to but forgot to emit a log."""
    q = (question or "").strip()
    lowered = q.lower()
    area = next((a for hint, a in _AREA_HINTS if hint in lowered), "other")
    return {"user_role": "Other", "language": language, "category": "how_to_question", "severity": "low",
            "area": area, "record_reference": None, "amount_shown": None, "amount_expected": None,
            "summary": q[:200], "what_happened": q[:500], "expected": "", "steps": [], "error_message": "",
            "device_browser": "", "user_blocked": False, "troubleshooting_tried": [],
            "assistant_answer_given": (reply or "")[:500], "contains_redactions": False,
            "contains_secret": False, "injection_attempt": False}


def _override_attempt_log() -> dict:
    return {"user_role": "Other", "language": "en", "category": "security_concern", "severity": "low",
            "area": "other", "record_reference": None, "amount_shown": None, "amount_expected": None,
            "summary": "User message tried to override assistant rules.", "what_happened": "", "expected": "",
            "steps": [], "error_message": "", "device_browser": "", "user_blocked": False,
            "troubleshooting_tried": [], "assistant_answer_given": "Declined and redirected.",
            "contains_redactions": False, "contains_secret": False, "injection_attempt": True}


async def run_chat_turn(db: Session, user: dict, conversation_id: str, history: list[dict], flags: dict) -> dict:
    """Answer the latest sanitized question and persist structured reports when requested."""
    if not history:
        raise HTTPException(status_code=400, detail="A user question is required.")

    question = str(history[-1].get("content") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="A user question is required.")

    flags = flags if isinstance(flags, dict) else {}
    language = detect_language(question)
    result = parse_model_output(await ask_model(history, language), user_email=user_email(user))

    action = result.get("log_action", "none")
    log_data = result.get("log")

    # Deterministic safety override: if the sanitizer detected a real override phrase,
    # create a minimal security report even if the model omitted the structured log.
    if flags.get("injection_attempt"):
        action = "create"
        log_data = _override_attempt_log()
        log_data["language"] = language

    # Preserve how-to logging when the model correctly classifies the question but omits its log.
    if action == "none" and result.get("intent") == "how_to":
        action = "create"
        log_data = how_to_log(question, result.get("reply", ""), language)

    report_id = None
    if action in {"create", "update"} and isinstance(log_data, dict):
        log_data = dict(log_data)
        log_data["contains_secret"] = bool(log_data.get("contains_secret") or flags.get("contains_secret"))
        log_data["contains_redactions"] = bool(log_data.get("contains_redactions") or flags.get("contains_redactions"))
        log_data["injection_attempt"] = bool(log_data.get("injection_attempt") or flags.get("injection_attempt"))
        report_id = save_report(db, user, conversation_id, action, log_data, flags)

    logger.info("help turn processed: language=%s action=%s report=%s user=%s",
                language, action, report_id, user_email(user))

    reply = result["reply"]
    if "{{REPORT_ID}}" in reply:
        reply = reply.replace("{{REPORT_ID}}", report_id or "(unavailable, please tell your administrator)")
    return {"reply": reply, "report_id": report_id}


def process_history(messages: list[dict]):
    return sanitize_history(messages)


def parse_assistant_response(content: str) -> dict:
    return parse_model_output(content)