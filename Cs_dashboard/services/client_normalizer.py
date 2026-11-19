import re
from datetime import date, datetime
from typing import Any, List, Optional
from db import ExitInterview

COL_NAME         = 0   
COL_DATE         = 1  
COL_CASE_NUM     = 2   
COL_PHONE        = 3   
COL_CALL_TYPE    = 4   
COL_ATTORNEY     = 5   
COL_PARALEGAL    = 6   
COL_RAT_ATTY     = 7   
COL_RAT_PARA     = 8   
COL_RAT_OUTCOME  = 9   
COL_RAT_EXP      = 10  
COL_WILL_REVIEW  = 11  
COL_ACTUAL_REVIEW = 12 

def safe_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()

def _safe_int(val: Any) -> Optional[int]:
    if val in (None, "", "N/A"):
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None

def _safe_bool_yn(val: Any) -> Optional[bool]:
    s = safe_str(val).lower()
    if s in ("y", "yes", "true", "1"):
        return True
    if s in ("n", "no", "false", "0"):
        return False
    return None

def _parse_dt(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day)
    s = safe_str(val)
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def _compute_fields(ratings_tuple: tuple) -> dict:
    ratings = [v for v in ratings_tuple if v is not None]
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
    """
    row: list of cell values (positional, 0-based).
    Returns ExitInterview ORM object or None if row looks empty.
    """
    def cell(idx: int) -> Any:
        try:
            v = row[idx]
            return v if v not in (None, "", "N/A") else None
        except IndexError:
            return None
    name = cell(COL_NAME)
    if name is None:
        return None
    name_str = safe_str(name)
    if name_str.lower() in ("name", "success team reviews", ""):
        return None
    submitted_at = _parse_dt(cell(COL_DATE))
    case_num_raw = cell(COL_CASE_NUM)
    case_num_int = _safe_int(case_num_raw)
    attorney_code_raw  = safe_str(cell(COL_ATTORNEY)) or None
    paralegal_code_raw = safe_str(cell(COL_PARALEGAL)) or None
    rat_a = _safe_int(cell(COL_RAT_ATTY))
    rat_p = _safe_int(cell(COL_RAT_PARA))
    rat_c = _safe_int(cell(COL_RAT_OUTCOME))
    rat_o = _safe_int(cell(COL_RAT_EXP))
    computed = _compute_fields((rat_a, rat_p, rat_c, rat_o))
    will_review   = _safe_bool_yn(cell(COL_WILL_REVIEW))
    actual_review = _safe_bool_yn(cell(COL_ACTUAL_REVIEW))

    return ExitInterview(
        source_row_id          = case_num_int,          
        submitted_at           = submitted_at,
        submitter_email        = "",                     
        submitter_name         = name_str,
        case_name              = safe_str(case_num_raw),
        neos_id                = case_num_int,
        lead_attorney          = attorney_code_raw,       
        attorney_code          = attorney_code_raw,
        paralegal               = paralegal_code_raw,
        paralegal_code          = paralegal_code_raw,
        rating_attorney_comm    = rat_a,
        rating_paralegal_comm   = rat_p,
        rating_case_outcome     = rat_c,
        rating_overall          = rat_o,
        willing_to_refer        = will_review,
        suggestions              = None,                  
        google_review_status     = None,                  
        month                    = str(submitted_at.month) if submitted_at else "",
        year                     = str(submitted_at.year) if submitted_at else "",
        avg_rating               = computed["avg_rating"],
        sentiment_flag           = computed["sentiment_flag"],
        referral_flag            = bool(will_review) if will_review is not None else False,
        review_sent              = bool(actual_review) if actual_review is not None else False,
        review_declined          = will_review is False,
        review_withheld          = False,
    )


def normalize_rows(raw_rows: List[List[Any]]) -> List[ExitInterview]:
    """Convert list of raw row arrays into ExitInterview objects, skipping bad rows."""
    results = []
    for row in raw_rows:
        obj = row_to_exit_interview(row)
        if obj:
            results.append(obj)
    return results