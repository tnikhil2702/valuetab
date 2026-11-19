from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import requests
from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.dialects.mysql import insert as mysql_insert

from auth_utils import get_current_user
from db import (GoogleOAuthToken, NeosWbImportLog, NeosWbMonthConfirmation, NeosWbProviderReview,
                NeosWbRecord, NeosWbReferrer, NeosWbReminderSent, NeosWbSourceMap, SessionLocal)

log = logging.getLogger("marketing.whiteboard")
router = APIRouter()

# ----------------------------------------------------------------- settings
WINDOW_START = date.fromisoformat(os.getenv("WB_WINDOW_START", "2024-09-01"))
AUTO_SYNC_HOURS = float(os.getenv("WB_AUTO_SYNC_HOURS", "24"))
MAPPING_MODEL = os.getenv("WB_MAPPING_MODEL", "gpt-4o-mini")
PAGE_SIZE = 2000
STALE_RUNNING_AFTER = timedelta(minutes=90)   # first sync resolves ~1,000 referrer names
REFERRER_LOOKUP_CONCURRENCY = 4

# Brian's source groups plus the three holding groups (dev note v2, item 1).
SOURCE_GROUPS = [
    "Digital", "Traditional Marketing", "Attorney Referral", "Medical Provider",
    "Client / Past-Present", "Community / Family", "Employee", "Event / Sponsor / Venue",
    "Non-Medical Business Relations", "Individual (needs cleanup)",
    "Other / unclassified", "Unknown / blank",
]
UNMAPPED_GROUP = "Other / unclassified"
BLANK_GROUP = "Unknown / blank"
BLANK_SOURCE = "(blank)"
PENDING_REFERRER = "(referrer name lookup pending)"

CASE_STATUS = "Case"
REJECTED_PREFIX = "rejected"
EXTRA_RESOLVED_STATUSES: set[str] = set()
CASE_DEFINITION = "any record with a Date Opened, counted in the month of Date Opened, any status (per Brian)"
INTAKE_DEFINITION = "resolved intakes only: status Case or any Rejected status, or any record with a Date Opened"
LATEST_MONTHS_MAY_RISE = 2     # latest months still rising as open intakes resolve


def is_resolved(status: Optional[str], date_opened: Optional[date]) -> bool:
    """Brian's intake rule: count a record only once it is resolved."""
    if date_opened is not None:
        return True
    s = (status or "").strip()
    return s == CASE_STATUS or s.lower().startswith(REJECTED_PREFIX) or s in EXTRA_RESOLVED_STATUSES


def resolved_filter():
    """SQL version of is_resolved() for NeosWbRecord queries."""
    conds = [NeosWbRecord.date_opened.isnot(None), NeosWbRecord.status == CASE_STATUS,
             NeosWbRecord.status.like("Rejected%")]
    if EXTRA_RESOLVED_STATUSES:
        conds.append(NeosWbRecord.status.in_(sorted(EXTRA_RESOLVED_STATUSES)))
    return or_(*conds)

# Case types shown as their own filter; anything else rolls into "Other",
# matching the prototype. Neos appends year suffixes (MVA22, PRM23).
KNOWN_TYPES = ["MVA", "Med Mal", "AVM", "PRM", "CPC", "GPI", "WCC", "CRM", "BUS", "EMP"]
TYPE_ALIASES = {"MED": "Med Mal"}            # Bradford, Nov 2026: MED is Med Mal

OFFICE_PREFIX_RE = re.compile(r"^ramos\s+law\s*[-–—]\s*", re.IGNORECASE)
OFFICES = ["Northglenn", "Colorado Springs", "Cheyenne", "Grand Junction", "Phoenix"]
OFFICE_ALIASES = {"denver": "Northglenn", "downtown denver": "Northglenn", "wheat ridge": "Northglenn",
                  "dtc": "Northglenn", "centennial": "Northglenn"}
OFFICE_NOT_RECORDED = "Not recorded"
OFFICE_ERROR = "Unlisted office (data error)"

COLUMNS = ["IntakeDate", "CaseNumber", "DateOpened", "CaseDetails_CurrentStatus_Name",
           "CaseType_Code", "UserCaseData[Office Location]", "Source", "ReferredBy_Id"]

SEED_FILE = Path(__file__).with_name("whiteboard_seed_map.json")

PROVIDER_GROUP = "Medical Provider"
REMINDER_TO = [a.strip() for a in os.getenv("WB_PROVIDER_REMINDER_TO", "").split(",") if a.strip()]
REMINDER_DAY = int(os.getenv("WB_PROVIDER_REMINDER_DAY", "6"))   # reports land on the 1st, confirmed by the 5th
DASHBOARD_URL = os.getenv("WB_DASHBOARD_URL", "")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))

_payload_cache: dict[str, Any] = {}
_sync_lock = asyncio.Lock()


# ----------------------------------------------------------------- helpers
def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    text = str(value).strip()
    for fmt, cut in (("%m/%d/%Y", None), ("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(text[:cut] if cut else text, fmt).date()
        except ValueError:
            continue
    return None


def _clip(value, n):
    if value is None:
        return None
    text = str(value).strip()
    return text[:n] if text else None


def normalize_type(raw: Optional[str]) -> str:
    code = (raw or "").strip().upper()
    # MVA22 is the active MVA type; MVA and MVA-PWL are legacy MVA types (Bradford).
    if re.sub(r"[\s\-]", "", code).startswith("MVA"):
        return "MVA"
    code = re.sub(r"[\s\d]+$", "", code)
    code = TYPE_ALIASES.get(code, code)
    return code if code in KNOWN_TYPES else "Other"


def normalize_office(raw: Optional[str]) -> str:
    text = (raw or "").strip()
    if not text:
        return OFFICE_NOT_RECORDED          # only a blank field is "Not recorded"
    name = OFFICE_PREFIX_RE.sub("", text).strip()
    if not name:
        return OFFICE_ERROR
    for office in OFFICES:
        if name.lower() == office.lower():
            return office
    return OFFICE_ALIASES.get(name.lower(), OFFICE_ERROR)


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _months_between(start: date, end: date) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


# ----------------------------------------------------------------- Neos pull
def _search_body(entity: str, filters: list[dict], order_by: str, skip: int) -> dict:
    return {
        "SearchObject": entity, "PrimaryRecordTypeId": None,
        "SearchQuery": {"EntityName": entity, "QueryElements": filters},
        "Columns": COLUMNS, "Aggregations": [], "SummaryFields": [], "CalculatedFields": [],
        "OrderByBinding": order_by, "Descending": False,
        "Take": PAGE_SIZE, "Skip": skip, "GetTotalResultsCount": True, "GetSummary": False,
    }


def _filter(entity: str, binding: str, value, op: str) -> dict:
    return {"QueryOperatorString": op, "QueryConjunctionString": "And", "QueryElements": [],
            "EntityName": entity, "PropertyBinding": binding, "FilterValue": value}


async def _pull(client, entity: str, binding: str) -> list[dict]:
    filters = [_filter(entity, binding, WINDOW_START.isoformat(), "GreaterThanOrEqual")]
    rows, skip = [], 0
    while True:
        data = await client.advanced_search(_search_body(entity, filters, binding, skip))
        page = (data or {}).get("Results") or []
        total = int((data or {}).get("TotalResultsCount") or 0)
        rows.extend(page)
        skip += PAGE_SIZE
        if not page or skip >= total:
            return rows


def _to_record(row: dict, kind: str) -> Optional[dict]:
    case_number = _clip(row.get("CaseNumber"), 32)
    if not case_number:
        return None
    return {
        "case_number": case_number,
        "record_kind": kind,
        "intake_date": _parse_date(row.get("IntakeDate")),
        "date_opened": _parse_date(row.get("DateOpened")),
        "status": _clip(row.get("CaseDetails_CurrentStatus_Name"), 120),
        "case_type_raw": _clip(row.get("CaseType_Code"), 40),
        "office_raw": _clip(row.get("UserCaseData[Office Location]"), 120),
        "source_raw": _clip(row.get("Source"), 255),     # replaced by the referrer name below
        "referred_by_id": _clip(row.get("ReferredBy_Id"), 64),
    }


async def fetch_neos_records() -> list[dict]:
    """Intakes (not yet cases) + cases whose intake OR opened date is in the window."""
    from PeakTrac.neos_session import get_neos_client  # shared read-only client

    client = get_neos_client()
    intakes = await _pull(client, "Intake", "IntakeDate")
    cases_by_intake = await _pull(client, "Case", "IntakeDate")
    # Cases opened in the window from intakes taken earlier (the export missed these).
    cases_by_opened = await _pull(client, "Case", "DateOpened")

    merged: dict[str, dict] = {}
    for kind, rows in (("intake", intakes), ("case", cases_by_intake), ("case", cases_by_opened)):
        for row in rows:
            rec = _to_record(row, kind)
            if rec and (kind == "case" or rec["case_number"] not in merged):
                merged[rec["case_number"]] = rec
    records = list(merged.values())
    names = await resolve_referrer_names(client, {r["referred_by_id"] for r in records if r["referred_by_id"]})
    for r in records:
        rid = r["referred_by_id"]
        if rid:
            r["source_raw"] = names.get(rid) or PENDING_REFERRER
    return records


async def _lookup_referrer(client, rid: str) -> Optional[str]:
    """One record that carries this id, asking for the (slow) ReferredBy name column."""
    for entity in ("Intake", "Case"):
        body = {**_search_body(entity, [_filter(entity, "ReferredBy_Id", rid, "Equal")],
                               "IntakeDate", 0),
                "Columns": ["IntakeDate", "ReferredBy"], "Take": 1, "GetTotalResultsCount": False}
        data = await client.advanced_search(body)
        rows = (data or {}).get("Results") or []
        if rows and rows[0].get("ReferredBy"):
            return str(rows[0]["ReferredBy"]).strip()[:255]
    return None


async def resolve_referrer_names(client, ids: set[str]) -> dict[str, str]:
    """Cached id -> name; looks up only ids never seen before. Failures are retried next sync."""
    def load():
        db = SessionLocal()
        try:
            return {r.referrer_id: r.name for r in db.query(NeosWbReferrer).all()}
        finally:
            db.close()

    known = await asyncio.to_thread(load)
    missing = [i for i in ids if i not in known]
    sem = asyncio.Semaphore(REFERRER_LOOKUP_CONCURRENCY)
    found: dict[str, str] = {}

    async def one(rid: str):
        async with sem:
            try:
                name = await _lookup_referrer(client, rid)
            except Exception as e:
                log.warning("referrer lookup failed (%s)", type(e).__name__)
                return
            if name:
                found[rid] = name

    await asyncio.gather(*(one(i) for i in missing))

    def save():
        db = SessionLocal()
        try:
            for rid, name in found.items():
                db.add(NeosWbReferrer(referrer_id=rid, name=name))
            db.commit()
        finally:
            db.close()

    if found:
        await asyncio.to_thread(save)
    return {**known, **found}


# ----------------------------------------------------------------- storage
def _upsert_records(records: list[dict], sync_id: int) -> None:
    db = SessionLocal()
    try:
        for i in range(0, len(records), 1000):
            chunk = [{**r, "last_sync_id": sync_id} for r in records[i:i + 1000]]
            stmt = mysql_insert(NeosWbRecord).values(chunk)
            stmt = stmt.on_duplicate_key_update(
                {c: stmt.inserted[c] for c in (
                    "record_kind", "intake_date", "date_opened", "status", "case_type_raw",
                    "office_raw", "source_raw", "referred_by_id", "last_sync_id")}
            )
            db.execute(stmt)
        db.commit()
    finally:
        db.close()


def _ensure_seed_mapping(db) -> None:
    if db.query(NeosWbSourceMap.id).first() or not SEED_FILE.exists():
        return
    for item in json.loads(SEED_FILE.read_text(encoding="utf-8")):
        db.add(NeosWbSourceMap(raw_source=item["raw"][:255], clean_source=item["clean"][:255],
                               source_group=item["group"], status="approved",
                               origin="prototype_seed", reviewed_by=None))   # counts, but shown as "not yet confirmed"
    db.commit()


def _flags(records: list[dict]) -> dict:
    in_window = [r for r in records if r["intake_date"] and r["intake_date"] >= WINDOW_START]
    return {
        "flag_no_status": sum(1 for r in in_window if not r["status"]),
        "flag_opened_before_intake": sum(
            1 for r in records
            if r["date_opened"] and r["intake_date"] and r["date_opened"] < r["intake_date"]),
        "flag_case_no_date_opened": sum(
            1 for r in records if r["status"] == CASE_STATUS and not r["date_opened"]),
    }


# ----------------------------------------------------------------- AI mapping
def _mapping_prompt(items: list[dict], examples: list[dict]) -> str:
    ex = "\n".join(f'- "{e["raw"]}" -> "{e["clean"]}" | {e["group"]}' for e in examples)
    todo = "\n".join(f'- "{i["raw"]}" ({i["intakes"]} intakes)' for i in items)
    return f"""You map raw lead-source values from a personal-injury law firm's case system
(Neos) to a clean source name and a source group.

Allowed groups (use exactly one, spelled exactly):
{json.dumps(SOURCE_GROUPS)}

Guidance:
- Digital: Google Ads, Google Search, Local Services, Facebook/Instagram/Meta, TikTok, YouTube,
  ChatGPT/AI tools, legal directories (FindLaw, Avvo, Super Lawyers, Lawyers.com), "Internet".
- Traditional Marketing: TV, radio, billboard, print, mail.
- Attorney Referral: law firms or attorneys.
- Medical Provider: chiropractic, medical, pain, spine, injury centers, physical therapy, clinics.
- Individual (needs cleanup): a single person's name with no category.
- Unknown / blank: empty or meaningless values.
- Other / unclassified: anything you cannot place with confidence.
- clean_source: fix spelling, spacing and case. If the raw value is a variant of an
  existing clean name from the examples, reuse that exact clean name.

Existing approved mappings (examples):
{ex or "- none yet"}

Map these raw values:
{todo}

Reply as JSON: {{"items": [{{"raw": "<exact raw value>", "clean": "...", "group": "...", "reason": "<short>"}}]}}"""


async def _ai_propose(unmapped: list[dict], examples: list[dict]) -> list[dict]:
    from openai import AsyncOpenAI

    if not unmapped or not os.getenv("OPENAI_API_KEY"):
        return []
    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    sem = asyncio.Semaphore(4)

    async def one(chunk: list[dict]) -> list[dict]:
        async with sem:
            try:
                resp = await client.chat.completions.create(
                    model=MAPPING_MODEL, temperature=0, response_format={"type": "json_object"},
                    messages=[{"role": "user", "content": _mapping_prompt(chunk, examples)}],
                )
                items = json.loads(resp.choices[0].message.content).get("items") or []
            except Exception as e:  # one bad chunk must not stop the import
                log.warning("whiteboard AI mapping chunk failed: %s", type(e).__name__)
                return []
        wanted = {c["raw"] for c in chunk}
        out = []
        for it in items:
            raw = it.get("raw")
            if raw not in wanted:
                continue
            group = it.get("group") if it.get("group") in SOURCE_GROUPS else UNMAPPED_GROUP
            out.append({"raw": raw, "clean": (it.get("clean") or raw).strip()[:255],
                        "group": group, "reason": (it.get("reason") or "")[:500]})
        return out

    chunks = [unmapped[i:i + 50] for i in range(0, len(unmapped), 50)]
    results = await asyncio.gather(*(one(c) for c in chunks))
    return [r for part in results for r in part]


def _unmapped_sources(db, sync_id: int) -> list[dict]:
    known = {r[0] for r in db.query(NeosWbSourceMap.raw_source).all()}
    counts = (db.query(NeosWbRecord.source_raw, func.count(NeosWbRecord.id))
              .filter(NeosWbRecord.last_sync_id == sync_id, NeosWbRecord.source_raw.isnot(None))
              .group_by(NeosWbRecord.source_raw).all())
    return sorted(({"raw": s, "intakes": n} for s, n in counts
                   if s and s not in known and s != PENDING_REFERRER),
                  key=lambda x: -x["intakes"])


def _approved_examples(db, limit: int = 150) -> list[dict]:
    rows = (db.query(NeosWbSourceMap).filter(NeosWbSourceMap.status == "approved")
            .order_by(NeosWbSourceMap.id).limit(limit).all())
    return [{"raw": r.raw_source, "clean": r.clean_source, "group": r.source_group} for r in rows]


# ----------------------------------------------------------------- sync job
def _latest_success(db) -> Optional[NeosWbImportLog]:
    """Current data = records stamped with this sync id or a later one. Rows are
    stamped as they are written, so a running or failed later sync still holds
    current rows; rows Neos stopped returning keep an older id and drop out."""
    return (db.query(NeosWbImportLog).filter(NeosWbImportLog.status == "success")
            .order_by(NeosWbImportLog.id.desc()).first())


def _running(db) -> Optional[NeosWbImportLog]:
    row = (db.query(NeosWbImportLog).filter(NeosWbImportLog.status == "running")
           .order_by(NeosWbImportLog.id.desc()).first())
    if row and row.started_at and datetime.now() - row.started_at > STALE_RUNNING_AFTER:
        row.status, row.error, row.finished_at = "failed", "Timed out (stale run).", datetime.now()
        db.commit()
        return None
    return row


def _start_log(triggered_by: str) -> Optional[int]:
    db = SessionLocal()
    try:
        if _running(db):
            return None
        row = NeosWbImportLog(status="running", triggered_by=triggered_by, window_start=WINDOW_START)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _finish_log(sync_id: int, **fields) -> None:
    db = SessionLocal()
    try:
        row = db.get(NeosWbImportLog, sync_id)
        for k, v in fields.items():
            setattr(row, k, v)
        row.finished_at = datetime.now()
        db.commit()
    finally:
        db.close()


async def run_sync(sync_id: int) -> None:
    async with _sync_lock:
        try:
            records = await fetch_neos_records()
            if not records:
                raise RuntimeError("Neos returned no rows for the window.")
            await asyncio.to_thread(_upsert_records, records, sync_id)

            db = SessionLocal()
            try:
                _ensure_seed_mapping(db)
                unmapped = _unmapped_sources(db, sync_id)
                examples = _approved_examples(db)
            finally:
                db.close()

            proposals = await _ai_propose(unmapped, examples)
            db = SessionLocal()
            try:
                for p in proposals:
                    if not db.query(NeosWbSourceMap.id).filter_by(raw_source=p["raw"]).first():
                        db.add(NeosWbSourceMap(raw_source=p["raw"], clean_source=p["clean"],
                                               source_group=p["group"], status="proposed",
                                               origin="ai", ai_reason=p["reason"]))
                db.commit()
            finally:
                db.close()

            today = date.today()
            through = max((d for r in records for d in (r["intake_date"], r["date_opened"])
                           if d and d <= today), default=None)
            _finish_log(
                sync_id, status="success", data_through=through,
                intake_rows=sum(1 for r in records if r["intake_date"] and r["intake_date"] >= WINDOW_START
                                and is_resolved(r["status"], r["date_opened"])),
                case_rows=sum(1 for r in records if r["date_opened"]),
                new_unmapped_sources=len(unmapped), ai_proposed=len(proposals), **_flags(records),
            )
        except Exception as e:
            log.exception("whiteboard sync failed")
            _finish_log(sync_id, status="failed", error=f"{type(e).__name__}: {str(e)[:500]}")
        finally:
            _payload_cache.clear()


# ----------------------------------------------------------------- rollup
def _log_dict(row: Optional[NeosWbImportLog]) -> Optional[dict]:
    if not row:
        return None
    return {c: (v.isoformat() if isinstance(v, (date, datetime)) else v)
            for c, v in ((c.name, getattr(row, c.name)) for c in row.__table__.columns)}


def build_payload() -> dict:
    db = SessionLocal()
    try:
        latest = _latest_success(db)
        running = _running(db)
        if not latest:
            return {"ready": False, "running": _log_dict(running), "last_import": None}
        # Mapping version is part of the key so an approval made through another
        # worker process is picked up too.
        map_version = db.query(func.count(NeosWbSourceMap.id), func.max(NeosWbSourceMap.reviewed_at),
                               func.max(NeosWbSourceMap.id)).one()
        cache_key = (latest.id, *map_version)
        if _payload_cache.get("key") == cache_key:
            return {**_payload_cache["payload"], "running": _log_dict(running)}

        mapping = {m.raw_source: m for m in db.query(NeosWbSourceMap).all()}
        recs = (db.query(NeosWbRecord.intake_date, NeosWbRecord.date_opened, NeosWbRecord.status,
                         NeosWbRecord.case_type_raw, NeosWbRecord.office_raw, NeosWbRecord.source_raw)
                .filter(NeosWbRecord.last_sync_id >= latest.id).all())
        recent_logs = db.query(NeosWbImportLog).order_by(NeosWbImportLog.id.desc()).limit(12).all()
    finally:
        db.close()

    through = latest.data_through or date.today()
    months = _months_between(WINDOW_START, through)
    m_idx = {m: i for i, m in enumerate(months)}
    types, offices, sources, groups = [], [], [], []
    t_idx, o_idx, s_idx = {}, {}, {}

    def idx(table: dict, lst: list, key, group=None):
        if key not in table:
            table[key] = len(lst)
            lst.append(key)
            if group is not None:
                groups.append(group)
        return table[key]

    pending = defaultdict(int)
    unresolved = defaultdict(int)       # open intakes left out, by Intake Date month
    intake_rows, open_rows = defaultdict(lambda: [0, 0]), defaultdict(int)
    for intake_d, opened_d, status, type_raw, office_raw, source_raw in recs:
        resolved = is_resolved(status, opened_d)
        if not resolved:
            if intake_d and _month_key(intake_d) in m_idx:
                unresolved[_month_key(intake_d)] += 1
            continue                    # not an intake yet and, with no Date Opened, not a case
        raw = (source_raw or "").strip()
        m = mapping.get(raw) if raw else None
        if not raw:
            clean, group = BLANK_SOURCE, BLANK_GROUP
        elif m and m.status == "approved":
            clean, group = m.clean_source or raw, m.source_group or UNMAPPED_GROUP
        else:
            clean, group = raw, UNMAPPED_GROUP
            if intake_d and intake_d >= WINDOW_START and raw != PENDING_REFERRER:
                pending[raw] += 1
        t = idx(t_idx, types, normalize_type(type_raw))
        o = idx(o_idx, offices, normalize_office(office_raw))
        s = idx(s_idx, sources, clean, group)
        is_case = opened_d is not None      # any status (Brian)
        if intake_d and _month_key(intake_d) in m_idx:
            cell = intake_rows[(m_idx[_month_key(intake_d)], t, o, s)]
            cell[0] += 1
            cell[1] += 1 if is_case else 0
        if is_case and _month_key(opened_d) in m_idx:
            open_rows[(m_idx[_month_key(opened_d)], t, o, s)] += 1

    last_full = months[-1]
    if through.month == (through + timedelta(days=1)).month and len(months) > 1:
        last_full = months[-2]       # latest month is still partial

    payload = {
        "ready": True,
        "months": months, "types": types, "offices": offices,
        "sources": sources, "srcGroup": groups,
        "rows": [[*k, v[0], v[1]] for k, v in intake_rows.items()],
        "orows": [[*k, v] for k, v in open_rows.items()],
        "groupOrder": SOURCE_GROUPS,
        "meta": {
            "window_start": WINDOW_START.isoformat(),
            "data_through": through.isoformat(),
            "default_month": last_full,
            "partial_month": months[-1] if last_full != months[-1] else None,
            "pending_sources": len(pending),
            "pending_intakes": sum(pending.values()),
            "case_definition": CASE_DEFINITION,
            "intake_definition": INTAKE_DEFINITION,
            "unresolved_by_month": dict(unresolved),
            "unresolved_total": sum(unresolved.values()),
            "may_rise_months": months[-LATEST_MONTHS_MAY_RISE:],
        },
        "last_import": _log_dict(latest),
        "import_log": [_log_dict(r) for r in recent_logs],
    }
    _payload_cache.update(key=cache_key, payload=payload)
    return {**payload, "running": _log_dict(running)}


def _needs_auto_sync() -> bool:
    db = SessionLocal()
    try:
        if _running(db):
            return False
        latest = _latest_success(db)
        return not latest or (datetime.now() - latest.finished_at) > timedelta(hours=AUTO_SYNC_HOURS)
    finally:
        db.close()


# ----------------------------------------------------------------- endpoints
@router.on_event("startup")
async def _start_whiteboard_scheduler():
    from marketing_dashboard import whiteboard_scheduler
    whiteboard_scheduler.start()


@router.on_event("shutdown")
async def _stop_whiteboard_scheduler():
    from marketing_dashboard import whiteboard_scheduler
    whiteboard_scheduler.stop()


def _confirmations() -> dict:
    db = SessionLocal()
    try:
        return {c.month: {"by": c.confirmed_by, "at": c.confirmed_at.isoformat() if c.confirmed_at else None,
                          "intakes": c.intakes, "cases": c.cases}
                for c in db.query(NeosWbMonthConfirmation).all()}
    finally:
        db.close()


@router.get("/data")
async def whiteboard_data(background: BackgroundTasks, user: dict = Depends(get_current_user)):
    if AUTO_SYNC_HOURS > 0 and await asyncio.to_thread(_needs_auto_sync):
        sync_id = await asyncio.to_thread(_start_log, "auto")
        if sync_id:
            background.add_task(run_sync, sync_id)
    payload = await asyncio.to_thread(build_payload)
    payload["confirmations"] = await asyncio.to_thread(_confirmations)
    return payload


def _month_totals(payload: dict, month: str) -> tuple[int, int]:
    """Firmwide intakes (by Intake Date) and cases opened (by Date Opened) for one month."""
    i = payload["months"].index(month)
    return (sum(r[4] for r in payload["rows"] if r[0] == i),
            sum(r[4] for r in payload["orows"] if r[0] == i))


@router.post("/months/{month}/confirm")
def whiteboard_confirm_month(month: str, user: dict = Depends(get_current_user)):
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(status_code=400, detail="Month must be YYYY-MM.")
    payload = build_payload()
    if not payload.get("ready") or month not in payload["months"]:
        raise HTTPException(status_code=400, detail="No Neos data for that month.")
    if month == payload["meta"].get("partial_month"):
        raise HTTPException(status_code=400, detail="This month is still in progress and cannot be confirmed yet.")
    intakes, cases = _month_totals(payload, month)
    db = SessionLocal()
    try:
        row = db.query(NeosWbMonthConfirmation).filter_by(month=month).first()
        if not row:
            row = NeosWbMonthConfirmation(month=month)
            db.add(row)
        row.confirmed_by, row.confirmed_at = user.get("email"), datetime.now()
        row.intakes, row.cases = intakes, cases
        db.commit()
        return {"month": month, "confirmed_by": row.confirmed_by, "intakes": intakes, "cases": cases}
    finally:
        db.close()


@router.delete("/months/{month}/confirm")
def whiteboard_unconfirm_month(month: str, user: dict = Depends(get_current_user)):
    db = SessionLocal()
    try:
        row = db.query(NeosWbMonthConfirmation).filter_by(month=month).first()
        if row:
            db.delete(row)
            db.commit()
        return {"month": month, "confirmed": False}
    finally:
        db.close()


@router.get("/scheduler")
def whiteboard_scheduler_status(user: dict = Depends(get_current_user)):
    from marketing_dashboard import whiteboard_scheduler
    return whiteboard_scheduler.status()


@router.post("/sync")
async def whiteboard_sync(background: BackgroundTasks, user: dict = Depends(get_current_user)):
    sync_id = await asyncio.to_thread(_start_log, user.get("email"))
    if not sync_id:
        raise HTTPException(status_code=409, detail="A Neos sync is already running.")
    background.add_task(run_sync, sync_id)
    return {"started": True, "sync_id": sync_id}


@router.get("/sync/status")
def whiteboard_sync_status(user: dict = Depends(get_current_user)):
    db = SessionLocal()
    try:
        row = db.query(NeosWbImportLog).order_by(NeosWbImportLog.id.desc()).first()
        return {"latest": _log_dict(row)}
    finally:
        db.close()


@router.get("/mapping")
def whiteboard_mapping(status: str = Query("proposed", pattern="^(proposed|approved|all)$"),
                       user: dict = Depends(get_current_user)):
    db = SessionLocal()
    try:
        latest = _latest_success(db)
        q = db.query(NeosWbSourceMap)
        if status != "all":
            q = q.filter(NeosWbSourceMap.status == status)
        rows = q.all()
        counts = {}
        if latest:
            counts = dict(db.query(NeosWbRecord.source_raw, func.count(NeosWbRecord.id))
                          .filter(NeosWbRecord.last_sync_id >= latest.id,
                                  NeosWbRecord.intake_date >= WINDOW_START)
                          .group_by(NeosWbRecord.source_raw).all())
        items = [{
            "raw_source": r.raw_source, "clean_source": r.clean_source,
            "source_group": r.source_group, "status": r.status, "origin": r.origin,
            "ai_reason": r.ai_reason, "reviewed_by": r.reviewed_by,
            "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
            "intakes": int(counts.get(r.raw_source, 0)),
        } for r in rows]
        items.sort(key=lambda x: -x["intakes"])
        clean_names = sorted({r.clean_source for r in rows if r.status == "approved" and r.clean_source})
        return {"items": items, "groups": SOURCE_GROUPS, "clean_sources": clean_names}
    finally:
        db.close()


@router.post("/mapping")
def whiteboard_mapping_save(payload: dict = Body(...), user: dict = Depends(get_current_user)):
    """Approve or edit mappings. Body: {"items": [{raw_source, clean_source, source_group}]}.
    The raw value is never changed; only its clean name and group."""
    items = payload.get("items") or []
    if not items:
        raise HTTPException(status_code=400, detail="No mapping items supplied.")
    db = SessionLocal()
    try:
        saved = 0
        for it in items:
            raw = (it.get("raw_source") or "").strip()
            clean = (it.get("clean_source") or "").strip()
            group = it.get("source_group")
            if not raw or not clean or group not in SOURCE_GROUPS:
                raise HTTPException(status_code=400, detail=f"Invalid mapping for '{raw[:60]}'.")
            row = db.query(NeosWbSourceMap).filter_by(raw_source=raw).first()
            if not row:
                row = NeosWbSourceMap(raw_source=raw[:255], origin="manual")
                db.add(row)
            row.clean_source, row.source_group, row.status = clean[:255], group, "approved"
            row.reviewed_by, row.reviewed_at = user.get("email"), datetime.now()
            saved += 1
        db.commit()
        _payload_cache.clear()
        return {"saved": saved}
    finally:
        db.close()


@router.get("/google-intakes")
def whiteboard_google_intakes(start_date: str = Query(...), end_date: str = Query(...),
                              user: dict = Depends(get_current_user)):
    """Neos intakes whose source maps to the Google clean sources, by Intake Date."""
    start, end = _parse_date(start_date), _parse_date(end_date)
    if not start or not end:
        raise HTTPException(status_code=400, detail="start_date and end_date are required (YYYY-MM-DD).")
    db = SessionLocal()
    try:
        latest = _latest_success(db)
        if not latest:
            return {"ready": False}
        clean_of = {m.raw_source: m.clean_source for m in
                    db.query(NeosWbSourceMap).filter(NeosWbSourceMap.status == "approved").all()}
        rows = (db.query(NeosWbRecord.source_raw, func.count(NeosWbRecord.id))
                .filter(NeosWbRecord.last_sync_id >= latest.id,
                        NeosWbRecord.intake_date >= start, NeosWbRecord.intake_date <= end,
                        resolved_filter())       # resolved intakes only (Brian)
                .group_by(NeosWbRecord.source_raw).all())
        totals = defaultdict(int)
        for raw, n in rows:
            totals[clean_of.get(raw, raw) if raw else BLANK_SOURCE] += n
        return {
            "ready": True, "start_date": start.isoformat(), "end_date": end.isoformat(),
            "data_through": latest.data_through.isoformat() if latest.data_through else None,
            "google_ads": totals.get("Google Ads", 0),
            "google_search": totals.get("Google Search", 0),
            "google_local_services": totals.get("Google Local Services", 0),
        }
    finally:
        db.close()


# ----------------------------------------------------------------- spend + reconciliation
def _google_ads_search(query: str) -> list[dict]:
    from marketing_dashboard import router as md
    from marketing_dashboard.generate_token import get_valid_access_token

    access = get_valid_access_token()
    db = SessionLocal()
    try:
        token = db.query(GoogleOAuthToken).first()
        dev = token.google_ads_developer_token if token else None
    finally:
        db.close()
    if not dev:
        raise RuntimeError("Google Ads developer token not found.")
    url = (f"https://googleads.googleapis.com/{md.GOOGLE_ADS_API_VERSION}/"
           f"customers/{md.GOOGLE_ADS_CUSTOMER_ID}/googleAds:search")
    headers = {"Authorization": f"Bearer {access}", "developer-token": dev,
               "Content-Type": "application/json"}
    out, body = [], {"query": query}
    while True:
        resp = requests.post(url, headers=headers, json=body, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"Google Ads {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        out.extend(data.get("results") or [])
        if not data.get("nextPageToken"):
            return out
        body = {"query": query, "pageToken": data["nextPageToken"]}


def _meta_monthly_spend(start: date, end: date) -> dict[str, float]:
    from marketing_dashboard import router as md

    db = SessionLocal()
    try:
        token = db.query(GoogleOAuthToken).first()
        access = os.getenv("META_ACCESS_TOKEN") or (token.meta_access_token if token else None)
    finally:
        db.close()
    if not access:
        raise RuntimeError("Meta access token not found.")
    url = f"https://graph.facebook.com/v22.0/act_{md.AD_ACCOUNT_ID}/insights"
    params = {"access_token": access, "level": "account", "fields": "spend",
              "time_increment": "monthly",
              "time_range": json.dumps({"since": start.isoformat(), "until": end.isoformat()})}
    out: dict[str, float] = {}
    while url:
        resp = requests.get(url, params=params, timeout=60)
        data = resp.json() if resp.content else {}
        if resp.status_code != 200:
            raise RuntimeError((data.get("error") or {}).get("message") or f"Meta {resp.status_code}")
        for row in data.get("data") or []:
            out[row["date_start"][:7]] = out.get(row["date_start"][:7], 0.0) + float(row.get("spend") or 0)
        url, params = (data.get("paging") or {}).get("next"), None
    return out


@router.get("/digital-spend")
def whiteboard_digital_spend(start_month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
                             end_month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
                             user: dict = Depends(get_current_user)):
    start = date.fromisoformat(start_month + "-01")
    end_first = date.fromisoformat(end_month + "-01")
    end = (end_first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    end = min(end, date.today())
    out = {"start_month": start_month, "end_month": end_month,
           "google_ads": {}, "meta": {}, "errors": {}}
    try:
        rows = _google_ads_search(
            "SELECT segments.month, metrics.cost_micros FROM customer "
            f"WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'")
        for r in rows:
            m = (r.get("segments") or {}).get("month", "")[:7]
            out["google_ads"][m] = round(out["google_ads"].get(m, 0)
                                         + int((r.get("metrics") or {}).get("costMicros", 0)) / 1e6, 2)
    except Exception as e:
        out["errors"]["google_ads"] = str(e)[:300]
    try:
        out["meta"] = {k: round(v, 2) for k, v in _meta_monthly_spend(start, end).items()}
    except Exception as e:
        out["errors"]["meta"] = str(e)[:300]
    return out


@router.get("/google-conversion-actions")
def whiteboard_google_conversion_actions(start_date: str = Query(...), end_date: str = Query(...),
                                         user: dict = Depends(get_current_user)):
    """Section F: which conversion actions make up the "Google Leads" number, and how
    each one counts (ONE_PER_CLICK vs MANY_PER_CLICK)."""
    start, end = _parse_date(start_date), _parse_date(end_date)
    if not start or not end:
        raise HTTPException(status_code=400, detail="start_date and end_date are required (YYYY-MM-DD).")
    try:
        actions = _google_ads_search(
            "SELECT conversion_action.id, conversion_action.name, conversion_action.type, "
            "conversion_action.category, conversion_action.status, conversion_action.counting_type, "
            "conversion_action.primary_for_goal, conversion_action.include_in_conversions_metric "
            "FROM conversion_action")
        by_action = _google_ads_search(
            "SELECT segments.conversion_action_name, segments.conversion_action_category, "
            "metrics.conversions, metrics.all_conversions FROM customer "
            f"WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)[:400])

    meta = {}
    for a in actions:
        ca = a.get("conversionAction") or {}
        meta[ca.get("name")] = {
            "type": ca.get("type"), "category": ca.get("category"), "status": ca.get("status"),
            "counting_type": ca.get("countingType"), "primary_for_goal": ca.get("primaryForGoal"),
            "include_in_conversions_metric": ca.get("includeInConversionsMetric"),
        }
    totals = defaultdict(lambda: {"conversions": 0.0, "all_conversions": 0.0})
    for r in by_action:
        seg, met = r.get("segments") or {}, r.get("metrics") or {}
        name = seg.get("conversionActionName") or "(unknown)"
        totals[name]["conversions"] += float(met.get("conversions") or 0)
        totals[name]["all_conversions"] += float(met.get("allConversions") or 0)
    items = [{"name": n, **meta.get(n, {}), "conversions": round(v["conversions"], 2),
              "all_conversions": round(v["all_conversions"], 2)} for n, v in totals.items()]
    items.sort(key=lambda x: -x["conversions"])
    return {"start_date": start.isoformat(), "end_date": end.isoformat(),
            "total_conversions": round(sum(i["conversions"] for i in items), 2),
            "items": items}


# ----------------------------------------------------------------- new-provider review (item C)
def new_providers(db) -> dict:
    """Medical Provider sources added to the mapping since the last review.
    Before the first review, everything except the prototype seed list counts as new."""
    last = db.query(NeosWbProviderReview).order_by(NeosWbProviderReview.id.desc()).first()
    q = db.query(NeosWbSourceMap).filter(NeosWbSourceMap.source_group == PROVIDER_GROUP)
    if last:   # created, or mapped/approved into the group, since the last review
        q = q.filter(or_(NeosWbSourceMap.created_at > last.reviewed_at,
                         NeosWbSourceMap.reviewed_at > last.reviewed_at))
    else:
        q = q.filter(NeosWbSourceMap.origin != "prototype_seed")
    rows = q.all()
    latest = _latest_success(db)
    counts = {}
    if latest and rows:
        counts = dict(db.query(NeosWbRecord.source_raw, func.count(NeosWbRecord.id))
                      .filter(NeosWbRecord.last_sync_id >= latest.id,
                              NeosWbRecord.source_raw.in_([r.raw_source for r in rows]))
                      .group_by(NeosWbRecord.source_raw).all())
    items = sorted(({
        "raw_source": r.raw_source, "clean_source": r.clean_source, "status": r.status,
        "added": r.created_at.date().isoformat() if r.created_at else None,
        "intakes": int(counts.get(r.raw_source, 0)),
    } for r in rows), key=lambda x: -x["intakes"])
    return {
        "items": items,
        "awaiting_approval": sum(1 for i in items if i["status"] != "approved"),
        "last_review": ({"by": last.reviewed_by, "at": last.reviewed_at.isoformat()} if last else None),
    }


def _reminder_body(data: dict) -> str:
    lines = [
        "Hello,",
        "",
        f"{len(data['items'])} new medical provider referral source(s) were added in Neos since the last review"
        + (f" ({data['awaiting_approval']} still awaiting mapping approval)." if data["awaiting_approval"] else "."),
        "",
    ]
    for i in data["items"][:50]:
        flag = "" if i["status"] == "approved" else "  [awaiting approval]"
        lines.append(f"  - {i['clean_source'] or i['raw_source']}  ({i['intakes']} intakes){flag}")
    if len(data["items"]) > 50:
        lines.append(f"  ... and {len(data['items']) - 50} more")
    lines += ["", "Please review them on the Marketing Dashboard: Whiteboard Dash > 4 Provider Referrals",
              "(approve or correct the mapping, then click \"Mark reviewed\")."]
    if DASHBOARD_URL:
        lines.append(DASHBOARD_URL)
    lines += ["", "This is an automatic monthly reminder from the Marketing Dashboard."]
    return "\n".join(lines)


def send_provider_reminder(force: bool = False) -> dict:
    """Send the monthly email once per month, on or after REMINDER_DAY. Safe to schedule daily.
    The month is claimed in neos_wb_reminders_sent before sending, so it never goes twice."""
    import smtplib
    from email.message import EmailMessage
    from sqlalchemy.exc import IntegrityError

    today = date.today()
    period = _month_key(today)
    if not force and today.day < REMINDER_DAY:
        return {"sent": False, "reason": f"Before day {REMINDER_DAY} of the month."}
    if not REMINDER_TO:
        return {"sent": False, "reason": "WB_PROVIDER_REMINDER_TO is not set."}
    user, password = os.getenv("SMTP_USERNAME"), os.getenv("SMTP_PASSWORD")
    if not (user and password):
        return {"sent": False, "reason": "SMTP_USERNAME / SMTP_PASSWORD are not set."}

    db = SessionLocal()
    try:
        data = new_providers(db)
        if not data["items"]:
            return {"sent": False, "reason": "No new providers since the last review."}
        claim = NeosWbReminderSent(period=period, recipients=", ".join(REMINDER_TO),
                                   provider_count=len(data["items"]))
        db.add(claim)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return {"sent": False, "reason": f"Already sent for {period}."}
        msg = EmailMessage()
        msg["Subject"] = f"Monthly review: {len(data['items'])} new provider referral source(s)"
        msg["From"], msg["To"] = user, ", ".join(REMINDER_TO)
        msg.set_content(_reminder_body(data))
        try:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(user, password)
                smtp.send_message(msg)
        except Exception as e:
            db.delete(claim)          # release the month so the next run retries
            db.commit()
            return {"sent": False, "reason": f"SMTP error: {type(e).__name__}"}
        return {"sent": True, "period": period, "recipients": REMINDER_TO, "providers": len(data["items"])}
    finally:
        db.close()


@router.get("/providers/new")
def whiteboard_new_providers(user: dict = Depends(get_current_user)):
    db = SessionLocal()
    try:
        data = new_providers(db)
        last_sent = db.query(NeosWbReminderSent).order_by(NeosWbReminderSent.id.desc()).first()
        data["reminder"] = {
            "recipients_configured": bool(REMINDER_TO), "day": REMINDER_DAY,
            "last_sent": ({"period": last_sent.period, "at": last_sent.sent_at.isoformat()} if last_sent else None),
        }
        return data
    finally:
        db.close()


@router.post("/providers/reviewed")
def whiteboard_providers_reviewed(user: dict = Depends(get_current_user)):
    db = SessionLocal()
    try:
        count = len(new_providers(db)["items"])
        db.add(NeosWbProviderReview(reviewed_by=user.get("email"), provider_count=count))
        db.commit()
        return {"reviewed": count}
    finally:
        db.close()
