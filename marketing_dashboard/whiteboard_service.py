# whiteboard_service.py
"""
Whiteboard tab backend: pull Intakes / Cases from Neos, store them by CASE NUMBER ONLY
(no names, synopsis, notes or contact fields), build monthly rollups, serve them to the UI.

Neos  ->  this module (server only, holds the Neos login)  ->  /api/whiteboard/*  ->  Whiteboard tab
"""
import asyncio
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from db import SessionLocal, WhiteboardCase, WhiteboardImportLog
from MVA_Platform.neos_service import fetch_whiteboard_cases

DENVER = ZoneInfo("America/Denver")

# Rows before this month are dropped on the server (handoff section 3, "First backfill").
WINDOW_FLOOR = date(2024, 9, 1)
INCREMENTAL_MONTHS = 2          # monthly refresh: "Last 2 Months", upsert on Case Number
UPSERT_BATCH = 500              # stays under SQL parameter limits

_refresh_lock = asyncio.Lock()


# ===================================================================== parsing helpers
_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%y")


def parse_neos_date(value) -> date | None:
    """
    Neos returns dates as MM/DD/YYYY text (e.g. 09/30/2026). They are calendar dates, so they
    are parsed as LOCAL dates and never converted through UTC (that causes the off-by-one-day bug).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)      # ISO: take the date part as written
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def normalize_case_number(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text or None


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _clean_text(value, limit=255) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:limit] if text else None


# ===================================================================== source mapping
# PLACEHOLDER RULES. The Whiteboard-Dash-Dev-Note-v2 that defines source_clean / source_group
# was not part of the handoff, so this is a starting point. Edit these two dicts to match it.
SOURCE_ALIASES = {
    "facebook ads": "Facebook Ads", "fb ads": "Facebook Ads", "facebook": "Facebook Ads",
    "meta ads": "Facebook Ads", "meta": "Facebook Ads",
    "instagram ads": "Instagram Ads", "instagram": "Instagram Ads",
    "google ads": "Google Ads", "google adwords": "Google Ads", "adwords": "Google Ads",
    "google": "Google Ads", "bing ads": "Bing Ads",
}
SOURCE_GROUPS = {
    "Google Ads": "Digital", "Facebook Ads": "Digital",
    "Instagram Ads": "Digital", "Bing Ads": "Digital",
}
UNKNOWN_SOURCE = "Unknown"


def clean_source(raw: str | None) -> str:
    if not raw:
        return UNKNOWN_SOURCE
    return SOURCE_ALIASES.get(raw.strip().lower(), raw.strip())


def source_group(clean: str) -> str:
    return SOURCE_GROUPS.get(clean, "Other")


def is_mva(case_type: str | None) -> bool:
    """ASSUMPTION: any case type code starting with MVA (MVA, MVA22 ...) is a motor vehicle case."""
    return bool(case_type) and case_type.strip().upper().startswith("MVA")


# ===================================================================== import (Neos -> table)
def _in_window(d: date | None) -> bool:
    return d is not None and d >= WINDOW_FLOOR


def _source_raw(row: dict) -> str | None:
    return (_clean_text(row.get("ReferredBy_LastFirstFullName"))
            or _clean_text(row.get("ReferredBy_AKACompanyName")))


def _merge(rec, **values):
    """Set a field only when the new value is not empty, so a partial row never wipes data."""
    for field, value in values.items():
        if value is not None and value != "":
            setattr(rec, field, value)


def _upsert(db, rows: list[dict], kind: str) -> dict:
    stats = {"received": len(rows), "inserted": 0, "updated": 0,
             "dropped_before_window": 0, "dropped_no_case_number": 0}
    prepared: dict[str, dict] = {}

    for r in rows:
        number = normalize_case_number(r.get("CaseNumber"))
        if not number:
            stats["dropped_no_case_number"] += 1
            continue
        intake_date = parse_neos_date(r.get("IntakeDate"))
        date_opened = parse_neos_date(r.get("DateOpened")) if kind == "case" else None

        if kind == "intake" and not _in_window(intake_date):
            stats["dropped_before_window"] += 1
            continue
        if kind == "case":
            anchor = date_opened or intake_date
            if not _in_window(anchor):
                stats["dropped_before_window"] += 1
                continue

        prepared[number] = {
            "intake_date": intake_date,
            "date_opened": date_opened,
            "case_type": _clean_text(r.get("CaseType_Code"), 64),
            "office": _clean_text(r.get("Office"), 128),
            "source_raw": _source_raw(r),
            "stage": _clean_text(r.get("CaseDetails_CurrentStage_Name"), 128),
            "status": _clean_text(r.get("CaseDetails_CurrentStatus_Name"), 128),
        }

    numbers = list(prepared)
    now = datetime.utcnow()
    for i in range(0, len(numbers), UPSERT_BATCH):
        batch = numbers[i:i + UPSERT_BATCH]
        existing = {
            rec.case_number: rec
            for rec in db.query(WhiteboardCase).filter(WhiteboardCase.case_number.in_(batch)).all()
        }
        for number in batch:
            values = prepared[number]
            rec = existing.get(number)
            if rec is None:
                rec = WhiteboardCase(case_number=number, seen_as_intake=False,
                                     seen_as_case=False, created_at=now)
                db.add(rec)
                stats["inserted"] += 1
            else:
                stats["updated"] += 1
            _merge(rec, **values)
            if kind == "intake":
                rec.seen_as_intake = True
            else:
                rec.seen_as_case = True
            rec.updated_at = now
        db.flush()
    return stats


def backfill_months(today: date | None = None) -> int:
    today = today or date.today()
    diff = (
    (today.year - WINDOW_FLOOR.year) * 12
        + (today.month - WINDOW_FLOOR.month)
    )
    return max(diff + 1, INCREMENTAL_MONTHS)


async def refresh_whiteboard(mode: str = "incremental") -> dict:
    if mode not in ("incremental", "backfill"):
        raise ValueError("mode must be 'incremental' or 'backfill'")
    if _refresh_lock.locked():
        return {"status": "already_running"}

    async with _refresh_lock:
        months = backfill_months() if mode == "backfill" else INCREMENTAL_MONTHS
        db = SessionLocal()
        log = WhiteboardImportLog(started_at=datetime.utcnow(), mode=mode,
                                  months=months, status="running", source="neos_api")
        db.add(log)
        db.commit()
        log_id = log.id
        try:
            data = await fetch_whiteboard_cases(months)
            intake_stats = _upsert(db, data["intakes"], "intake")
            case_stats = _upsert(db, data["cases"], "case")
            log = db.get(WhiteboardImportLog, log_id)
            log.status = "ok"
            log.finished_at = datetime.utcnow()
            log.intakes_received = intake_stats["received"]
            log.cases_received = case_stats["received"]
            log.intake_total_reported = data.get("intake_total")
            log.case_total_reported = data.get("case_total")
            db.commit()
            return {"status": "ok", "mode": mode, "months": months,
                    "intakes": intake_stats, "cases": case_stats,
                    "neos_reported": {"intakes": data.get("intake_total"),
                                      "cases": data.get("case_total")}}
        except Exception as exc:
            db.rollback()
            log = db.get(WhiteboardImportLog, log_id)
            if log:
                log.status = "failed"
                log.finished_at = datetime.utcnow()
                log.error = str(exc)[:1000]
                db.commit()
            raise
        finally:
            db.close()


_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def parse_month_param(value: str | None, default: date) -> date:
    if not value:
        return default
    if not _MONTH_RE.match(value):
        raise ValueError(f"'{value}' is not a YYYY-MM month")
    return date(int(value[:4]), int(value[5:7]), 1)


def _month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def build_rollups(records, month_from: date, month_to: date) -> dict:
    lo, hi = _month_start(month_from), _month_start(month_to)
    buckets = defaultdict(lambda: {"intakes": 0, "cases_opened": 0, "cases_from_period_intakes": 0})
    flags = {"no_status": 0, "opened_before_intake": 0, "case_no_date_opened": 0}
    data_through = None

    for r in records:
        if not (r.status or "").strip():
            flags["no_status"] += 1
        if r.date_opened and r.intake_date and r.date_opened < r.intake_date:
            flags["opened_before_intake"] += 1
        if r.seen_as_case and not r.date_opened:
            flags["case_no_date_opened"] += 1

        for d in (r.intake_date, r.date_opened):
            if d and (data_through is None or d > data_through):
                data_through = d

        dims = (r.case_type or "Unknown", r.office or "Unknown", r.source_raw or UNKNOWN_SOURCE)

        if r.intake_date and lo <= _month_start(r.intake_date) <= hi:
            key = (month_key(r.intake_date),) + dims
            buckets[key]["intakes"] += 1
            if r.date_opened:
                buckets[key]["cases_from_period_intakes"] += 1
        if r.date_opened and lo <= _month_start(r.date_opened) <= hi:
            key = (month_key(r.date_opened),) + dims
            buckets[key]["cases_opened"] += 1

    rows = []
    for (month, case_type, office, source_raw), counts in sorted(buckets.items()):
        clean = clean_source(source_raw)
        rows.append({
            "month": month, "case_type": case_type, "mva": is_mva(case_type),
            "office": office, "source_raw": source_raw,
            "source_clean": clean, "source_group": source_group(clean),
            **counts,
        })
    return {"rows": rows, "flags": flags, "data_through": data_through}


def get_rollups(db, month_from: str | None, month_to: str | None) -> dict:
    today = date.today()
    lo = parse_month_param(month_from, WINDOW_FLOOR)
    hi = parse_month_param(month_to, _month_start(today))
    if lo < WINDOW_FLOOR:
        lo = WINDOW_FLOOR
    if hi < lo:
        raise ValueError("'to' month is before 'from' month")

    records = db.query(WhiteboardCase).all()
    built = build_rollups(records, lo, hi)

    last = (db.query(WhiteboardImportLog)
            .filter(WhiteboardImportLog.status == "ok")
            .order_by(WhiteboardImportLog.finished_at.desc()).first())
    imported_at = None
    source = None
    if last and last.finished_at:
        imported_at = last.finished_at.replace(tzinfo=timezone.utc).astimezone(DENVER).isoformat()
        source = last.source

    return {
        "meta": {
            "data_through": built["data_through"].isoformat() if built["data_through"] else None,
            "imported_at": imported_at,
            "source": source,                 # "neos_api" or "export_file"
            "flags": built["flags"],
        },
        "rows": built["rows"],
    }


def qa_totals(db, month_from: str = "2024-09", month_to: str | None = None) -> dict:
    lo = parse_month_param(month_from, WINDOW_FLOOR)
    hi = parse_month_param(month_to, _month_start(date.today()))
    end = (date(hi.year + (hi.month // 12), (hi.month % 12) + 1, 1))   # first day after hi's month
    records = db.query(WhiteboardCase).all()

    def in_range(d):
        return d is not None and lo <= d < end

    intakes = sum(1 for r in records if in_range(r.intake_date))
    cases_by_intake_month = sum(1 for r in records if r.seen_as_case and in_range(r.intake_date))
    cases_opened_in_window = sum(1 for r in records if in_range(r.date_opened))
    return {
        "window": f"{lo.isoformat()[:7]} to {hi.isoformat()[:7]}",
        "intakes": intakes,
        "cases_with_intake_in_window": cases_by_intake_month,
        "cases_with_date_opened_in_window": cases_opened_in_window,
        "expected_for_sep2024_to_sep2026": {"intakes": 78955, "cases": 8157, "cases_opened_in_window": 8085},
    }


REFRESH_HOUR_DENVER = 6   

async def whiteboard_refresh_worker():
    while True:
        now = datetime.now(DENVER)
        nxt = now.replace(hour=REFRESH_HOUR_DENVER, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            result = await refresh_whiteboard("incremental")
            print(f"[Whiteboard] refresh: {result.get('status')}")
        except Exception as exc:
            print(f"[Whiteboard] refresh failed: {exc}")