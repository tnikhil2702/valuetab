# services/normalizer.py
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from db import ExitInterview

# Column indices (0-based)
COL_ID        = 0   # A
COL_SUBMITTED = 2   # C
COL_SUB_EMAIL = 3   # D
COL_SUB_NAME  = 4   # E
COL_CASE_NAME = 5   # F
COL_NEOS_ID   = 6   # G
COL_ATTORNEY  = 7   # H
COL_PARALEGAL = 8   # I
COL_RAT_ATTY  = 9   # J
COL_RAT_PARA  = 10  # K
COL_RAT_CASE  = 11  # L
COL_RAT_OVR   = 12  # M
COL_REFER     = 13  # N
COL_SUGGEST   = 14  # O
COL_GREVIEW   = 15  # P
COL_MONTH     = 18  # S
COL_YEAR      = 19  # T

GOOGLE_REVIEW_VALUES = {
    "yes - link sent":                        "Yes - Link sent",
    "no - client response (link not sent)":   "No - Client Response (Link not sent)",
    "no - you determined not to send the link": "No - You determined not to send the link",
}


def _safe_int(val: Any) -> Optional[int]:
    try:
        return int(val) if val not in (None, "", "N/A") else None
    except (ValueError, TypeError):
        return None


def _safe_float(val: Any) -> Optional[float]:
    try:
        return float(val) if val not in (None, "", "N/A") else None
    except (ValueError, TypeError):
        return None


def _extract_code(full_name: Optional[str]) -> Optional[str]:
    """Extract code from 'First Last (CODE)' pattern."""
    if not full_name:
        return None
    m = re.search(r'\(([^)]+)\)\s*$', full_name.strip())
    return m.group(1).strip() if m else None


def _parse_dt(val: Any) -> Optional[datetime]:
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(val).strip(), fmt)
        except ValueError:
            continue
    return None


def _normalize_google_review(val: Any) -> Optional[str]:
    if not val:
        return None
    return GOOGLE_REVIEW_VALUES.get(str(val).strip().lower())


def _compute_fields(r4: tuple) -> dict:
    ratings = [v for v in r4 if v is not None]
    if not ratings:
        return {"avg_rating": None, "sentiment_flag": None}
    avg = round(sum(ratings) / len(ratings), 2)
    if avg >= 4.5:
        sentiment = "positive"
    elif avg >= 3.5:
        sentiment = "neutral"
    else:
        sentiment = "negative"
    return {"avg_rating": avg, "sentiment_flag": sentiment}


def row_to_exit_interview(row: List[Any]) -> Optional[ExitInterview]:

    def cell(idx: int) -> Any:
        try:
            v = row[idx]
            return v if v not in (None, "", "N/A") else None
        except IndexError:
            return None

    # Skip header or empty rows
    raw_id = cell(COL_ID)

    if raw_id is None or str(raw_id).lower() in ("id", ""):
        return None
    submitted_at = _parse_dt(cell(COL_SUBMITTED))

    attorney_full = cell(COL_ATTORNEY)
    paralegal_full = cell(COL_PARALEGAL)

    rat_a = _safe_int(cell(COL_RAT_ATTY))
    rat_p = _safe_int(cell(COL_RAT_PARA))
    rat_c = _safe_int(cell(COL_RAT_CASE))
    rat_o = _safe_int(cell(COL_RAT_OVR))

    computed = _compute_fields(
        (rat_a, rat_p, rat_c, rat_o)
    )

    refer_val = cell(COL_REFER)

    referral_flag = (
        str(refer_val).strip().lower() == "yes"
        if refer_val
        else False
    )

    gr_raw = cell(COL_GREVIEW)
    gr_norm = _normalize_google_review(gr_raw)

    month_value = (
        submitted_at.strftime("%B")
        if submitted_at
        else ""
    )

    year_value = (
        str(submitted_at.year)
        if submitted_at
        else ""
    )

    return ExitInterview(
        source_row_id=_safe_int(raw_id),

        submitted_at=submitted_at,

        submitter_email=str(
            cell(COL_SUB_EMAIL) or ""
        ),

        submitter_name=str(
            cell(COL_SUB_NAME) or ""
        ),

        case_name=str(
            cell(COL_CASE_NAME) or ""
        ),

        neos_id=_safe_int(
            cell(COL_NEOS_ID)
        ),

        lead_attorney=attorney_full,

        attorney_code=_extract_code(
            attorney_full
        ),

        paralegal=paralegal_full,

        paralegal_code=_extract_code(
            paralegal_full
        ),

        rating_attorney_comm=rat_a,

        rating_paralegal_comm=rat_p,

        rating_case_outcome=rat_c,

        rating_overall=rat_o,

        willing_to_refer=refer_val,

        suggestions=cell(COL_SUGGEST),

        google_review_status=gr_norm,

        # FIXED
        month=month_value,
        year=year_value,

        avg_rating=computed["avg_rating"],

        sentiment_flag=computed["sentiment_flag"],

        referral_flag=referral_flag,

        review_sent=(
            gr_norm == "Yes - Link sent"
        ),

        review_declined=(
            gr_norm ==
            "No - Client Response (Link not sent)"
        ),

        review_withheld=(
            gr_norm ==
            "No - You determined not to send the link"
        ),
    )

def normalize_rows(raw_rows: List[List[Any]]) -> List[ExitInterview]:
    """Convert list of raw row arrays into ExitInterview objects, skipping bad rows."""
    results = []
    for row in raw_rows:
        obj = row_to_exit_interview(row)
        if obj:
            results.append(obj)
    return results