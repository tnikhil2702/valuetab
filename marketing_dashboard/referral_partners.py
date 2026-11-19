from __future__ import annotations
import asyncio
import logging
import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from typing import Any, Optional

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, HTTPException, UploadFile
from sqlalchemy import func
from sqlalchemy.dialects.mysql import insert as mysql_insert

from auth_utils import get_current_user
from db import (NeosRpCase, NeosWbReferrer, NeosWbSourceMap, RpLedgerRow, RpLedgerUpload,
                RpPartnerFlag, RpSyncLog, SessionLocal)
from marketing_dashboard import whiteboard as wb

log = logging.getLogger("marketing.referral_partners")
router = APIRouter()

# ----------------------------------------------------------------- settings
CASES_FROM = date.fromisoformat(os.getenv("RP_CASES_FROM", "2024-01-01"))   # dev note 2: 2024+ only
AUTO_SYNC_HOURS = float(os.getenv("RP_AUTO_SYNC_HOURS", "24"))
STALE_RUNNING_AFTER = timedelta(minutes=60)
PAGE_SIZE = 2000
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
USE_PROPOSED_MAPPING = os.getenv("RP_USE_PROPOSED_MAPPING", "1") != "0"


def _emails(var: str) -> set[str]:
    return {e.strip().lower() for e in os.getenv(var, "").split(",") if e.strip()}


VIEW_EMAILS = _emails("RP_VIEW_EMAILS")
ADMIN_EMAILS = _emails("RP_ADMIN_EMAILS")

COLUMNS = ["CaseNumber", "DateOpened", "CaseType_Code", "IsClosed", "CaseName",
           "ReferredBy_Id", "Source"]

PARTNER_GROUPS = ["Attorney Referral", "Medical Provider", "Non-Medical Business Relations",
                  "Community / Family", "Client / Past-Present", "Event / Sponsor / Venue",
                  "Individual (needs cleanup)"]

TYPE_ALIASES = {"MED": "Med Mal", "PWLMM": "Med Mal"}
NAME_TITLES = {"mr", "mrs", "ms", "miss", "dr", "esq", "md", "jd", "phd", "dc", "jr", "sr", "ii", "iii"}

_payload_cache: dict[str, Any] = {}
_sync_lock = asyncio.Lock()


# ----------------------------------------------------------------- access
async def rp_user(user: dict = Depends(get_current_user)) -> dict:
    if VIEW_EMAILS and user["email"].lower() not in VIEW_EMAILS:
        raise HTTPException(status_code=403, detail="Referral Partners is limited to marketing and leadership.")
    return user


def _is_admin(email: str) -> bool:
    return not ADMIN_EMAILS or email.lower() in ADMIN_EMAILS


async def rp_admin(user: dict = Depends(rp_user)) -> dict:
    if not _is_admin(user["email"]):
        raise HTTPException(status_code=403, detail="Not allowed to change Referral Partners data.")
    return user


# ----------------------------------------------------------------- normalisers
def norm_case_number(value) -> Optional[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    m = re.fullmatch(r"(\d+)(?:\.0+)?", text)
    if m:
        return m.group(1)
    return text[:32] or None


def norm_name(text: Optional[str]) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]", "", text)


def last_name(full: Optional[str]) -> Optional[str]:
    """'Last, First' -> 'last'; 'First Last' -> 'last'. Ledger names carry ':casenum'."""
    if not full:
        return None
    text = str(full).split(":")[0].strip()
    if not text:
        return None
    part = text.split(",")[0] if "," in text else text.split()[-1]
    return norm_name(part)[:120] or None


def names_agree(ledger_last: Optional[str], neos_last: Optional[str]) -> bool:
    if not ledger_last or not neos_last:
        return False
    if ledger_last == neos_last:
        return True
    # Hyphenated / compound surnames: "Smith" vs "Smith-Jones".
    short, long_ = sorted((ledger_last, neos_last), key=len)
    return len(short) >= 4 and short in long_


def type_group(raw: Optional[str]) -> str:
    code = re.sub(r"\s+", "", (raw or "").upper())
    if code.startswith("MVA"):
        return "MVA"
    code = re.sub(r"-?PWL$", "", code)
    code = re.sub(r"\d+$", "", code)
    code = TYPE_ALIASES.get(code, code)
    return code if code in wb.KNOWN_TYPES else "Other"


def _name_tokens(name: str) -> list[str]:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return [t for t in re.split(r"[^a-z]+", text.replace(".", "")) if t and t not in NAME_TITLES]


def likely_internal(partner: str, attorneys: list[str]) -> bool:
    if "ramos" in partner.lower():
        return True
    tokens = _name_tokens(partner)
    for atty in attorneys:
        parts = _name_tokens(atty)
        if len(parts) < 2:
            continue
        initial, surname = parts[0][0], parts[-1]
        if surname in tokens and any(t != surname and t.startswith(initial) for t in tokens):
            return True
    return False


def _truthy(v) -> Optional[bool]:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes")


async def fetch_cases() -> tuple[list[dict], dict[str, str]]:
    from PeakTrac.neos_session import get_neos_client  # shared read-only client

    client = get_neos_client()
    filters = [wb._filter("Case", "DateOpened", CASES_FROM.isoformat(), "GreaterThanOrEqual")]
    rows, skip = [], 0
    while True:
        body = {**wb._search_body("Case", filters, "DateOpened", skip), "Columns": COLUMNS, "Take": PAGE_SIZE}
        data = await client.advanced_search(body)
        page = (data or {}).get("Results") or []
        total = int((data or {}).get("TotalResultsCount") or 0)
        rows.extend(page)
        skip += PAGE_SIZE
        if not page or skip >= total:
            break

    records: dict[str, dict] = {}
    for row in rows:
        number = norm_case_number(row.get("CaseNumber"))
        opened = wb._parse_date(row.get("DateOpened"))
        if not number or not opened:
            continue
        records[number] = {
            "case_number": number,
            "date_opened": opened,
            "case_type_raw": wb._clip(row.get("CaseType_Code"), 40),
            "is_closed": _truthy(row.get("IsClosed")),
            "referred_by_id": wb._clip(row.get("ReferredBy_Id"), 64),
            "source_raw": wb._clip(row.get("Source"), 255),
            "client_last": last_name(row.get("CaseName")),
        }
    ids = {r["referred_by_id"] for r in records.values() if r["referred_by_id"]}
    names = await wb.resolve_referrer_names(client, ids)   # cached in neos_wb_referrers
    return list(records.values()), names


def _upsert_cases(records: list[dict], sync_id: int) -> None:
    db = SessionLocal()
    try:
        for i in range(0, len(records), 1000):
            chunk = [{**r, "last_sync_id": sync_id} for r in records[i:i + 1000]]
            stmt = mysql_insert(NeosRpCase).values(chunk)
            stmt = stmt.on_duplicate_key_update({c: stmt.inserted[c] for c in (
                "date_opened", "case_type_raw", "is_closed", "referred_by_id", "source_raw",
                "client_last", "last_sync_id")})
            db.execute(stmt)
        db.commit()
    finally:
        db.close()


async def _queue_unmapped(names: list[str]) -> int:
    def load():
        db = SessionLocal()
        try:
            known = {r[0] for r in db.query(NeosWbSourceMap.raw_source).all()}
            return known, wb._approved_examples(db)
        finally:
            db.close()

    known, examples = await asyncio.to_thread(load)
    counts = Counter(n for n in names if n and n not in known and n != wb.PENDING_REFERRER)
    if not counts:
        return 0
    proposals = await wb._ai_propose([{"raw": n, "intakes": c} for n, c in counts.most_common()], examples)

    def save():
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

    if proposals:
        await asyncio.to_thread(save)
    return len(proposals)


def _latest_success(db) -> Optional[RpSyncLog]:
    return db.query(RpSyncLog).filter(RpSyncLog.status == "success").order_by(RpSyncLog.id.desc()).first()


def _running(db) -> Optional[RpSyncLog]:
    row = db.query(RpSyncLog).filter(RpSyncLog.status == "running").order_by(RpSyncLog.id.desc()).first()
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
        row = RpSyncLog(status="running", triggered_by=triggered_by)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _finish_log(sync_id: int, **fields) -> None:
    db = SessionLocal()
    try:
        row = db.get(RpSyncLog, sync_id)
        for k, v in fields.items():
            setattr(row, k, v)
        row.finished_at = datetime.now()
        db.commit()
    finally:
        db.close()


async def run_sync(sync_id: int) -> None:
    async with _sync_lock, wb._sync_lock:
        try:
            records, names = await fetch_cases()
            if not records:
                raise RuntimeError("Neos returned no cases for the window.")
            await asyncio.to_thread(_upsert_cases, records, sync_id)
            await _queue_unmapped([names.get(r["referred_by_id"]) for r in records if r["referred_by_id"]])
            _finish_log(sync_id, status="success", case_rows=len(records))
        except Exception as e:
            log.exception("referral partners sync failed")
            _finish_log(sync_id, status="failed", error=f"{type(e).__name__}: {str(e)[:500]}")
        finally:
            _payload_cache.clear()


def _needs_auto_sync() -> bool:
    db = SessionLocal()
    try:
        if _running(db):
            return False
        latest = _latest_success(db)
        return not latest or (datetime.now() - latest.finished_at) > timedelta(hours=AUTO_SYNC_HOURS)
    finally:
        db.close()


LEDGER_SIGNATURE = {"type", "date", "num", "name", "memo", "class", "clr", "split",
                    "debit", "credit", "amount", "balance", "case number"}


def _read_frames(contents: bytes, filename: str) -> list[pd.DataFrame]:
    if filename.lower().endswith(".csv"):
        return [pd.read_csv(BytesIO(contents), header=None, dtype=object, encoding_errors="replace",
                            skip_blank_lines=False)]
    book = pd.ExcelFile(BytesIO(contents))
    return [pd.read_excel(book, sheet_name=s, header=None, dtype=object) for s in book.sheet_names]


def _cell(v) -> Optional[str]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    text = str(v).strip()
    return text or None


def _money(v) -> float:
    text = _cell(v)
    if not text:
        return 0.0
    neg = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace("$", "").replace(",", "")
    try:
        val = float(text)
    except ValueError:
        return 0.0
    return -val if neg else val


def _date(v) -> Optional[date]:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    text = _cell(v)
    if not text:
        return None
    parsed = pd.to_datetime(text, errors="coerce", dayfirst=False)
    return parsed.date() if pd.notna(parsed) else None


def _header_index(df: pd.DataFrame) -> Optional[int]:
    for i in range(min(len(df), 40)):
        cells = {str(v).strip().lower() for v in df.iloc[i] if _cell(v)}
        if len(cells & LEDGER_SIGNATURE) >= 4 and ({"credit", "debit", "amount"} & cells):
            return i
    return None


def parse_ledger(contents: bytes, filename: str) -> list[dict]:
    out: list[dict] = []
    for df in _read_frames(contents, filename):
        h = _header_index(df)
        if h is None:
            continue
        cols = [str(v).strip().lower() if _cell(v) else "" for v in df.iloc[h]]
        pos = {name: cols.index(name) for name in LEDGER_SIGNATURE if name in cols}
        first_col = min(pos.values())
        account = attorney = None

        for raw in df.iloc[h + 1:].itertuples(index=False):
            vals = list(raw)
            get = lambda k: vals[pos[k]] if k in pos else None   # noqa: E731
            txn_type = _cell(get("type"))
            txn_date = _date(get("date"))
            if not txn_type or not txn_date:
                texts = [t for t in (_cell(v) for v in vals) if t and isinstance(t, str)]
                lfi = next((t for t in texts if "legal fee income" in t.lower()), None)
                if lfi:
                    if lfi.lower().startswith("total"):
                        account = attorney = None
                    else:
                        account = lfi[:255]
                        attorney = lfi.rsplit("-", 1)[-1].strip()[:120] if "-" in lfi else None
                    continue
                lead = [_cell(v) for v in vals[:first_col + 1] if _cell(v)]
                if lead and not re.match(r"(beginning|opening|ending)\s+balance", lead[0].lower()):
                    account = attorney = None          
                continue
            if account is None:
                continue
            if "credit" in pos or "debit" in pos:
                credit, debit = _money(get("credit")), _money(get("debit"))
                amount = round(credit - debit, 2)
            else:
                amount = round(_money(get("amount")), 2)
                credit, debit = (amount, 0.0) if amount >= 0 else (0.0, -amount)
            if not credit and not debit:
                continue
            name = _cell(get("name"))
            case_number = norm_case_number(get("case number"))
            if not case_number and name and ":" in name:
                m = re.search(r":\s*(\d{4,})\s*$", name)
                case_number = m.group(1) if m else None
            out.append({
                "account": account, "attorney": attorney or "(no attorney account)",
                "txn_type": txn_type[:60], "txn_date": txn_date, "num": (_cell(get("num")) or "")[:60] or None,
                "case_number": case_number, "name_last": last_name(name),
                "debit": round(debit, 2), "credit": round(credit, 2), "amount": amount,
            })
    return out


def _store_ledger(rows: list[dict], file_name: str, email: str) -> dict:
    d_from = min(r["txn_date"] for r in rows)
    d_to = max(r["txn_date"] for r in rows)
    db = SessionLocal()
    try:
        replaced = (db.query(RpLedgerRow)
                    .filter(RpLedgerRow.txn_date >= d_from, RpLedgerRow.txn_date <= d_to)
                    .delete(synchronize_session=False))
        up = RpLedgerUpload(file_name=file_name[:255], uploaded_by=email, date_from=d_from, date_to=d_to,
                            row_count=len(rows), total_amount=round(sum(r["amount"] for r in rows), 2),
                            replaced_rows=replaced)
        db.add(up)
        db.flush()
        db.bulk_insert_mappings(RpLedgerRow, [{**r, "upload_id": up.id} for r in rows])
        for old in db.query(RpLedgerUpload).filter(RpLedgerUpload.id != up.id).all():
            if not db.query(RpLedgerRow.id).filter(RpLedgerRow.upload_id == old.id).first():
                db.delete(old)
        db.commit()
        return {"id": up.id, "file_name": up.file_name, "rows": len(rows), "replaced_rows": replaced,
                "date_from": d_from.isoformat(), "date_to": d_to.isoformat(),
                "total": float(up.total_amount),
                "accounts": sorted({r["attorney"] for r in rows})}
    finally:
        db.close()


# ----------------------------------------------------------------- payload
def _uploads(db) -> list[dict]:
    rows = (db.query(RpLedgerUpload, func.count(RpLedgerRow.id))
            .outerjoin(RpLedgerRow, RpLedgerRow.upload_id == RpLedgerUpload.id)
            .group_by(RpLedgerUpload.id).order_by(RpLedgerUpload.date_from.desc()).all())
    return [{"id": u.id, "file_name": u.file_name, "uploaded_by": u.uploaded_by,
             "date_from": u.date_from.isoformat() if u.date_from else None,
             "date_to": u.date_to.isoformat() if u.date_to else None,
             "rows": n, "created_at": u.created_at.isoformat() if u.created_at else None}
            for u, n in rows]


def _log_dict(row: Optional[RpSyncLog]) -> Optional[dict]:
    if not row:
        return None
    return {c.name: (v.isoformat() if isinstance(v, (date, datetime)) else v)
            for c, v in ((c, getattr(row, c.name)) for c in row.__table__.columns)}


def _default_year(fees_from: Optional[date], fees_to: Optional[date], cases_to: Optional[date]) -> str:
    if fees_from and fees_to:
        full = [y for y in range(fees_from.year, fees_to.year + 1)
                if fees_from <= date(y, 1, 31) and fees_to >= date(y, 12, 1)]
        return str(full[-1] if full else fees_to.year)
    return str((cases_to or date.today()).year)


def build_payload() -> dict:
    db = SessionLocal()
    try:
        latest = _latest_success(db)
        running = _running(db)
        if not latest:
            return {"ready": False, "running": _log_dict(running), "last_sync": None}
        map_version = db.query(func.count(NeosWbSourceMap.id), func.max(NeosWbSourceMap.reviewed_at),
                               func.max(NeosWbSourceMap.id)).one()
        ledger_version = db.query(func.count(RpLedgerRow.id), func.max(RpLedgerRow.id)).one()
        flag_version = db.query(func.count(RpPartnerFlag.id), func.max(RpPartnerFlag.set_at)).one()
        cache_key = (latest.id, *map_version, *ledger_version, *flag_version)
        if _payload_cache.get("key") == cache_key:
            return {**_payload_cache["payload"], "running": _log_dict(running)}

        cases = (db.query(NeosRpCase.case_number, NeosRpCase.date_opened, NeosRpCase.case_type_raw,
                          NeosRpCase.is_closed, NeosRpCase.referred_by_id, NeosRpCase.source_raw,
                          NeosRpCase.client_last)
                 .filter(NeosRpCase.last_sync_id >= latest.id, NeosRpCase.date_opened >= CASES_FROM)
                 .order_by(NeosRpCase.date_opened, NeosRpCase.case_number).all())
        ref_names = {r.referrer_id: r.name for r in db.query(NeosWbReferrer).all()}
        mapping = {m.raw_source: m for m in db.query(NeosWbSourceMap).all()}
        ledger = (db.query(RpLedgerRow.case_number, RpLedgerRow.txn_date, RpLedgerRow.amount,
                           RpLedgerRow.attorney, RpLedgerRow.name_last, RpLedgerRow.txn_type,
                           RpLedgerRow.account)
                  .order_by(RpLedgerRow.txn_date, RpLedgerRow.id).all())
        flags = {f.partner: f.internal for f in db.query(RpPartnerFlag).all()}
        uploads = _uploads(db)
    finally:
        db.close()

    # Partners: contact id -> contact name -> approved clean name + group.
    partners, p_idx, p_groups = [], {}, defaultdict(Counter)
    types, t_idx = [], {}
    tgroups = sorted({type_group(c.case_type_raw) for c in cases})
    tg_idx = {t: i for i, t in enumerate(tgroups)}
    refs, r_idx = [], {}

    def resolve(rid: Optional[str], source: Optional[str]) -> tuple[str, str, bool]:
        """-> (partner name, source group, mapping still pending review)"""
        raw = (ref_names.get(rid) or wb.PENDING_REFERRER) if rid else (source or "").strip()
        if not raw:
            return wb.BLANK_SOURCE, wb.BLANK_GROUP, False
        m = mapping.get(raw)
        if m and m.status == "approved":
            return (m.clean_source or raw), (m.source_group or wb.UNMAPPED_GROUP), False
        if m and USE_PROPOSED_MAPPING:
            return (m.clean_source or raw), (m.source_group or wb.UNMAPPED_GROUP), True
        return raw, wb.UNMAPPED_GROUP, True

    case_rows, by_number = [], {}
    pending_partners, pending_cases = set(), 0
    for c in cases:
        name, group, pending = resolve(c.referred_by_id, c.source_raw)
        if name not in p_idx:
            p_idx[name] = len(partners)
            partners.append(name)
        pi = p_idx[name]
        p_groups[pi][group] += 1
        if pending:
            pending_partners.add(pi)
            pending_cases += 1
        code = c.case_type_raw or "(none)"
        if code not in t_idx:
            t_idx[code] = len(types)
            types.append(code)
        ri = -1
        if c.referred_by_id:
            if c.referred_by_id not in r_idx:
                r_idx[c.referred_by_id] = len(refs)
                refs.append(c.referred_by_id)
            ri = r_idx[c.referred_by_id]
        status = None if c.is_closed is None else (1 if c.is_closed else 0)
        by_number[c.case_number] = (len(case_rows), c.client_last)
        case_rows.append([c.case_number, c.date_opened.isoformat(), pi, t_idx[code],
                          tg_idx[type_group(c.case_type_raw)], status, ri])
    pgroup = [p_groups[i].most_common(1)[0][0] for i in range(len(partners))]

    # Ledger join (dev note 2).
    attys, a_idx = [], {}
    fees, exceptions = [], []
    gl_total, gl_cases = Decimal(0), set()
    matched_total, matched_cases = Decimal(0), set()
    oos_total, oos_cases = Decimal(0), set()
    for num, d, amount, atty, name_last_, txn_type, account in ledger:
        gl_total += amount
        if num:
            gl_cases.add(num)
        hit = by_number.get(num) if num else None
        reason = None
        if not num:
            reason = "No case number on the ledger row"
        elif hit is None:
            oos_total += amount
            oos_cases.add(num)
            continue                            # case opened before the window, or not in Neos
        elif not names_agree(name_last_, hit[1]):
            reason = ("No client name on the ledger row" if not name_last_ else
                      "Client last name on the ledger does not match Neos")
        if reason:
            exceptions.append({"case_number": num, "date": d.isoformat(), "amount": float(amount),
                               "attorney": atty, "type": txn_type, "account": account, "reason": reason})
            continue
        if atty not in a_idx:
            a_idx[atty] = len(attys)
            attys.append(atty)
        fees.append([hit[0], d.isoformat(), float(amount), a_idx[atty]])
        matched_total += amount
        matched_cases.add(num)

    fee_attys = sorted({a for a in attys if a != "(no attorney account)"})
    pint, pint_src = [], []
    for name in partners:
        if name in flags:
            pint.append(1 if flags[name] else 0)
            pint_src.append("confirmed")
        else:
            pint.append(1 if likely_internal(name, fee_attys) else 0)
            pint_src.append("inferred")

    opened = [c[1] for c in case_rows]
    fee_dates = [r[1] for r in ledger]
    fees_from = min(fee_dates) if fee_dates else None
    fees_to = max(fee_dates) if fee_dates else None
    cases_to = date.fromisoformat(max(opened)) if opened else None
    joined_rows = len(fees) + sum(1 for e in exceptions if e["case_number"])

    payload = {
        "ready": True,
        "partners": partners, "pgroup": pgroup, "pint": pint, "pintSrc": pint_src,
        "ppend": [1 if i in pending_partners else 0 for i in range(len(partners))],
        "types": types, "tgroups": tgroups, "attys": attys, "refs": refs,
        "cases": case_rows,     # [caseNumber, opened, partnerIdx, typeIdx, tgroupIdx, closed(1/0/null), refIdx]
        "fees": fees,           # [caseIdx, feeDate, netAmount, attyIdx]
        "partnerGroups": PARTNER_GROUPS,
        "meta": {
            "cases_from": min(opened) if opened else None,
            "cases_to": max(opened) if opened else None,
            "window_start": CASES_FROM.isoformat(),
            "fees_from": fees_from.isoformat() if fees_from else None,
            "fees_to": fees_to.isoformat() if fees_to else None,
            "gl_total": float(gl_total), "gl_cases": len(gl_cases),
            "matched_total": float(matched_total), "matched_cases": len(matched_cases),
            "out_of_scope_total": float(oos_total), "out_of_scope_cases": len(oos_cases),
            "exceptions": len(exceptions), "exceptions_total": float(sum(e["amount"] for e in exceptions)),
            "name_agree_pct": round(len(fees) / joined_rows * 100, 1) if joined_rows else None,
            "default_year": _default_year(fees_from, fees_to, cases_to),
            "pending_mapping_cases": pending_cases,
            "uses_proposed_mapping": USE_PROPOSED_MAPPING,
        },
        "uploads": uploads,
        "last_sync": _log_dict(latest),
    }
    _payload_cache.update(key=cache_key, payload=payload, exceptions=exceptions)
    return {**payload, "running": _log_dict(running)}


# ----------------------------------------------------------------- endpoints
@router.get("/data")
async def rp_data(background: BackgroundTasks, user: dict = Depends(rp_user)):
    if AUTO_SYNC_HOURS > 0 and await asyncio.to_thread(_needs_auto_sync):
        sync_id = await asyncio.to_thread(_start_log, "auto")
        if sync_id:
            background.add_task(run_sync, sync_id)
    payload = await asyncio.to_thread(build_payload)
    return {**payload, "can_admin": _is_admin(user["email"])}


@router.get("/exceptions")
async def rp_exceptions(user: dict = Depends(rp_user)):
    """Ledger rows rejected by the join, for review. Case numbers only, no client names."""
    await asyncio.to_thread(build_payload)
    rows = _payload_cache.get("exceptions") or []
    return {"count": len(rows), "rows": sorted(rows, key=lambda r: (r["date"], r["case_number"] or ""))}


@router.post("/sync")
async def rp_sync(background: BackgroundTasks, user: dict = Depends(rp_admin)):
    sync_id = await asyncio.to_thread(_start_log, user["email"])
    if not sync_id:
        raise HTTPException(status_code=409, detail="A Neos refresh is already running.")
    background.add_task(run_sync, sync_id)
    return {"started": True, "sync_id": sync_id}


@router.get("/sync/status")
def rp_sync_status(user: dict = Depends(rp_user)):
    db = SessionLocal()
    try:
        return {"running": _log_dict(_running(db)), "last_sync": _log_dict(_latest_success(db)),
                "last_attempt": _log_dict(db.query(RpSyncLog).order_by(RpSyncLog.id.desc()).first())}
    finally:
        db.close()


@router.post("/ledger")
async def rp_ledger_upload(file: UploadFile = File(...), user: dict = Depends(rp_admin)):
    name = file.filename or "ledger"
    if not name.lower().endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(status_code=400, detail="Upload a QuickBooks export as .xlsx, .xls or .csv.")
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File is larger than 30 MB.")
    try:
        rows = await asyncio.to_thread(parse_ledger, contents, name)
    except Exception as e:
        log.warning("ledger parse failed: %s", type(e).__name__)
        raise HTTPException(status_code=400, detail=f"Could not read the file ({type(e).__name__}).")
    if not rows:
        raise HTTPException(status_code=400, detail=(
            "No Legal Fee Income (401xx) transaction rows found. Export the QuickBooks transaction "
            "detail for the Legal Fee Income accounts, with Type, Date, Name, Case Number, Debit and Credit columns."))
    result = await asyncio.to_thread(_store_ledger, rows, name, user["email"])
    _payload_cache.clear()
    return result


@router.delete("/ledger/uploads/{upload_id}")
def rp_ledger_delete(upload_id: int, user: dict = Depends(rp_admin)):
    db = SessionLocal()
    try:
        up = db.get(RpLedgerUpload, upload_id)
        if not up:
            raise HTTPException(status_code=404, detail="Upload not found.")
        db.query(RpLedgerRow).filter(RpLedgerRow.upload_id == upload_id).delete(synchronize_session=False)
        db.delete(up)
        db.commit()
    finally:
        db.close()
    _payload_cache.clear()
    return {"deleted": upload_id}


@router.post("/partners/internal")
def rp_set_internal(payload: dict = Body(...), user: dict = Depends(rp_admin)):
    partner = (payload.get("partner") or "").strip()[:255]
    internal = payload.get("internal")
    if not partner or internal not in (True, False, None):
        raise HTTPException(status_code=400, detail="Send {partner, internal: true|false|null}.")
    db = SessionLocal()
    try:
        row = db.query(RpPartnerFlag).filter_by(partner=partner).first()
        if internal is None:
            if row:
                db.delete(row)
        elif row:
            row.internal, row.set_by = internal, user["email"]
        else:
            db.add(RpPartnerFlag(partner=partner, internal=internal, set_by=user["email"]))
        db.commit()
    finally:
        db.close()
    _payload_cache.clear()
    return {"partner": partner, "internal": internal}