from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import or_

NEOS_DIRECT = "neos_direct"
NOT_SCORED = "not_scored"

# Columns copied from Neos into the lead every load (Neos is the system of record for these).
_NEOS_OWNED = (
    "first_name", "last_name", "phone", "source", "case_description",
    "received_at", "neos_case_number", "neos_status", "assigned_to",
)
# Fields staff can edit on the site: only filled from the file while still blank,
# so a reload never wipes a correction made in IntakeOps.
_FILL_IF_BLANK = ("case_type", "email")

_META_KEYS = (
    "neos_intake_date", "neos_stage", "neos_status_name", "neos_taken_by",
    "neos_primary_staff", "neos_referred_by", "neos_marketing_source", "neos_last_update",
)


# ── Guards ──────────────────────────────────────────────────────────────────

def is_neos_direct(lead) -> bool:
    return getattr(lead, "intake_channel", None) == NEOS_DIRECT


def lead_emails_blocked(lead) -> bool:
    # Either signal blocks: a neos_direct row can never email even if the flag was flipped.
    return is_neos_direct(lead) or getattr(lead, "send_emails", True) is False


def lead_neos_writes_blocked(lead) -> bool:
    return is_neos_direct(lead) or getattr(lead, "write_neos_notes", True) is False


def ensure_lead_can_email(lead) -> None:
    if lead_emails_blocked(lead):
        raise HTTPException(
            status_code=403,
            detail="Emails are disabled for Neos Direct leads.",
        )


def not_neos_direct_clause(model):
    """SQL filter: rows that are normal IntakeOps leads (NULL-safe)."""
    return or_(model.intake_channel.is_(None), model.intake_channel != NEOS_DIRECT)


def neos_writable_clause(model):
    return (model.write_neos_notes.is_(True)) & not_neos_direct_clause(model)


# ── Loader ──────────────────────────────────────────────────────────────────

def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "1", "yes", "y")


def _clean(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_dt(value):
    text = _clean(value)
    if not text:
        return None
    # Export timestamps are ISO, naive (treated as UTC like the rest of received_at).
    return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)


def read_rows(path: str | Path) -> list[dict]:
    path = Path(path)
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload["leads"] if isinstance(payload, dict) else payload
    elif path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    else:
        raise ValueError(f"Unsupported file type: {path.suffix} (expected .json or .csv)")
    return rows


def _row_to_fields(row: dict) -> dict:
    neos_intake_id = _clean(row.get("neos_intake_id"))
    if not neos_intake_id:
        raise ValueError("row is missing neos_intake_id")
    channel = _clean(row.get("intake_channel")) or NEOS_DIRECT
    if channel != NEOS_DIRECT:
        raise ValueError(f"unexpected intake_channel {channel!r} for {neos_intake_id}")

    received_at = _parse_dt(row.get("received_at")) or _parse_dt(row.get("neos_intake_date"))
    return {
        "neos_intake_id": neos_intake_id,
        "first_name": _clean(row.get("first_name")),
        "last_name": _clean(row.get("last_name")),
        "phone": _clean(row.get("phone")),
        "email": _clean(row.get("email")),
        "source": _clean(row.get("source")),
        "case_type": _clean(row.get("case_type")),
        "case_description": _clean(row.get("case_description")),
        "received_at": received_at,
        "neos_case_number": _clean(row.get("neos_case_number")),
        "neos_status": _clean(row.get("neos_status")),
        "assigned_to": _clean(row.get("neos_assigned_to")),
        "link_review": _as_bool(row.get("link_review")),
        "link_candidate_lead_ids": _clean(row.get("link_candidate_lead_ids")),
        "neos_intake_meta": {k: _clean(row.get(k)) for k in _META_KEYS},
    }


def load_neos_direct(db, rows: list[dict], *, dry_run: bool = False) -> dict:
    """Upsert Neos Direct rows on neos_intake_id. Returns counts; commits unless dry_run."""
    from db import LeadIntake  # local import keeps this module importable without a DB for tests

    parsed, errors, seen = [], [], set()
    for i, row in enumerate(rows, start=1):
        try:
            fields = _row_to_fields(row)
        except Exception as exc:  # report and skip bad rows; never half-load one
            errors.append(f"row {i}: {exc}")
            continue
        if fields["neos_intake_id"] in seen:
            errors.append(f"row {i}: duplicate neos_intake_id {fields['neos_intake_id']} in file")
            continue
        seen.add(fields["neos_intake_id"])
        parsed.append(fields)

    existing = {
        lead.neos_intake_id: lead
        for lead in db.query(LeadIntake).filter(LeadIntake.neos_intake_id.in_(seen)).all()
    } if seen else {}

    now = datetime.utcnow()
    inserted = updated = conflicts = 0
    for f in parsed:
        lead = existing.get(f["neos_intake_id"])
        if lead is not None and lead.intake_channel != NEOS_DIRECT:
            # neos_intake_id already belongs to a normal IntakeOps lead; leave it alone.
            conflicts += 1
            errors.append(f"{f['neos_intake_id']}: already on non-Neos-Direct lead {lead.id}, skipped")
            continue

        if lead is None:
            lead = LeadIntake(
                neos_intake_id=f["neos_intake_id"],
                intake_channel=NEOS_DIRECT,
                status=None,
                scoring_status=NOT_SCORED,
                email_sent=False,
                neos_updated=False,
                link_review=f["link_review"],
                link_candidate_lead_ids=f["link_candidate_lead_ids"],
            )
            db.add(lead)
            inserted += 1
        else:
            updated += 1
            # Only (re)flag for review; never clear a hold from a file reload.
            if f["link_review"]:
                lead.link_review = True
                lead.link_candidate_lead_ids = f["link_candidate_lead_ids"]

        for col in _NEOS_OWNED:
            setattr(lead, col, f[col])
        for col in _FILL_IF_BLANK:
            if not getattr(lead, col) and f[col]:
                setattr(lead, col, f[col])
        lead.neos_intake_meta = f["neos_intake_meta"]
        # Always re-assert the safety flags, whatever the file says.
        lead.write_neos_notes = False
        lead.send_emails = False
        lead.neos_direct_loaded_at = now

    if dry_run:
        db.rollback()
    else:
        db.commit()

    return {
        "rows_in_file": len(rows),
        "inserted": inserted,
        "updated": updated,
        "skipped_conflicts": conflicts,
        "errors": errors,
        "dry_run": dry_run,
    }


def neos_direct_check(db) -> dict:
    """Numbers for the client's post-load check."""
    from db import LeadIntake

    q = db.query(LeadIntake).filter(LeadIntake.intake_channel == NEOS_DIRECT)
    return {
        "neos_direct_rows": q.count(),
        "emails_logged": q.filter(
            or_(LeadIntake.email_sent.is_(True), LeadIntake.lead_email_sent_at.isnot(None))
        ).count(),
        "neos_written": q.filter(LeadIntake.neos_updated.is_(True)).count(),
        "link_review_held": q.filter(LeadIntake.link_review.is_(True)).count(),
        "not_scored": q.filter(LeadIntake.scoring_status == NOT_SCORED).count(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Load / refresh Neos Direct leads.")
    parser.add_argument("path", help="neos_direct_leads_*.json or .csv")
    parser.add_argument("--dry-run", action="store_true", help="parse and count, write nothing")
    args = parser.parse_args(argv)

    from db import SessionLocal

    db = SessionLocal()
    try:
        result = load_neos_direct(db, read_rows(args.path), dry_run=args.dry_run)
        for err in result.pop("errors"):
            print("WARN", err)
        print(json.dumps(result, indent=2))
        print(json.dumps(neos_direct_check(db), indent=2))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
