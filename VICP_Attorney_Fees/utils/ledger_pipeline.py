import pandas as pd
import uuid
import asyncio
from collections import defaultdict
from datetime import datetime
from .excel_parser import parse_excel
from .normalization import normalize_text
from .classifier import classify_entry
from .lead_generator import (
    llm_lead_attorney,
    generate_attorney_entries,
    generate_paralegal_entry
)
from db import LedgerRun, LedgerEntry, LedgerEntryAudit, Session

# nearby date finder if exact match not found (±2 day window)
async def find_nearby_date(target_date, available_dates):
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d")

        valid_dates = sorted([
            datetime.strptime(d, "%Y-%m-%d")
            for d in available_dates if d
        ])

        if not valid_dates:
            return None

        # exact match
        if target in valid_dates:
            return target.strftime("%Y-%m-%d")

        # closest within ±2 days
        closest = min(valid_dates, key=lambda d: abs((d - target).days))

        if abs((closest - target).days) <= 2:
            return closest.strftime("%Y-%m-%d")

        return None
    except Exception:
        return None

async def normalize_date(date_val):
    try:
        if pd.isna(date_val):
            return None

        dt = pd.to_datetime(date_val, errors="coerce")
        if pd.isna(dt):
            return None

        # filter junk dates
        if dt.year < 1980:
            return None

        return dt.strftime("%Y-%m-%d")
    
    except:
        return None


# MAIN PIPELINE
async def generate_ledger(file_bytes, user_email, db: Session, filename: str):
    sheets = await parse_excel(file_bytes)
    run = LedgerRun(
        run_id=str(uuid.uuid4()),
        project_name="vicp",
        user_email=user_email,
        source_filename=filename,
        total_rows=sum(len(df) for df in sheets.values())
    )
    db.add(run)
    db.flush()

    total_generated_rows = 0
    sheet_names = list(sheets.keys())

    # PROCESS EACH SHEET SEQUENTIALLY (to manage LLM load and maintain order)
    for i, sheet_name in enumerate(sheet_names):

        df = sheets[sheet_name]

        ledger_rows = []        # preserves original order
        audit_log = []
        llm_input_rows = []

        # PARSE - DIRECT ENTRIES
        for _, row in df.iterrows():

            normalized = await normalize_text(row["task"])
            classification = await classify_entry(normalized)

            norm_date = await normalize_date(row["date"])
            if not norm_date:
                continue

            entry = await generate_paralegal_entry(
                norm_date,
                classification,
                hours=row["hours"],
                memos=row["task"]
            )

            entry["_meta"] = {
                "entry_type": "direct",
                "source_staff_code": row["staff"],
                "sheet_name": sheet_name
            }

            ledger_rows.append(entry)

            # LLM input
            llm_input_rows.append({
                "date": row["date"],
                "task": row["task"],
                "staff": row["staff"]
            })

            audit_log.append({
                "paralegal_memos": [row["task"]],
                "normalized": normalized,
                "primary_category": classification["primary_category"],
                "secondary_category": classification["secondary_category"],
                "confidence": classification["confidence"],
                "cluster_size": 1,
                "cluster_date": row["date"],
                "mixed": classification["mixed"],
                "generated_description": entry["Task Description"],
                "generated_hours": entry["Hours"],
                "generated_category": entry["Category"],
                "flag_reason": None
            })

        # LLM CALL 
        df_for_llm = pd.DataFrame(llm_input_rows)

        llm_output = await llm_lead_attorney(df_for_llm)
        attorney_entries = await generate_attorney_entries(llm_output)

        # BUILD ATTORNEY MAP
        attorney_map = defaultdict(list)

        # available_dates = [row["Date"] for row in ledger_rows]
        available_dates = [
            await normalize_date(row["Date"]) 
            for row in ledger_rows 
            if await normalize_date(row["Date"])
        ]

        for att in attorney_entries:

            original_date_raw = att["date"]
            original_date = await normalize_date(original_date_raw)
            if not original_date:
                print(f"Skipping invalid date: {original_date_raw}")
                continue
            best_date = await find_nearby_date(original_date, available_dates)

            if not best_date:
                print(f"Skipping hallucinated date: {original_date}")
                continue

            if best_date != original_date:
                print(f"Date adjusted: {original_date} to {best_date}")

            new_entry = {
                "Date": best_date,
                "Timekeeper": att["timekeeper"],
                "Task Description": att["task"],
                "Hours": att["hours"],
                "Category": att["category"],
                "_meta": {
                    "entry_type": "derived",
                    "source_staff_code": "JPR",
                    "sheet_name": sheet_name
                }
            }

            attorney_map[best_date].append(new_entry)

        # ORDER-PRESERVING MERGE
        sheet_ledger_rows = []
        rolling_memos = []

        for row in ledger_rows:
            date = row["Date"]

            # keep original entry
            sheet_ledger_rows.append(row)

            # collect paralegal memos
            if row.get("_meta", {}).get("entry_type") == "direct":
                rolling_memos.append(row["Task Description"])

            # insert attorney entries AFTER that date
            if date in attorney_map:
                for att_row in attorney_map[date]:
                    att_row["_meta"]["aggregated_memos"] = list(rolling_memos)
                    sheet_ledger_rows.append(att_row)
                del attorney_map[date]
                rolling_memos = []

        # edge case: unmatched dates
        for remaining in attorney_map.values():
            sheet_ledger_rows.extend(remaining)

        total_generated_rows += len(sheet_ledger_rows)

        # DB INSERT
        audit_index = 0

        for row in sheet_ledger_rows:

            meta = row.get("_meta", {})

            entry_type = meta.get("entry_type")
            source_staff_code = meta.get("source_staff_code")
            sheet_name_meta = meta.get("sheet_name")

            if entry_type == "direct":
                assigned_staff_code = source_staff_code
                assigned_role = "paralegal"
            else:
                assigned_staff_code = "JPR"
                assigned_role = "lead_attorney"

            entry = LedgerEntry(
                run_id=run.run_id,
                date=row["Date"],
                timekeeper=row["Timekeeper"],
                source_staff_code=source_staff_code,
                assigned_staff_code=assigned_staff_code,
                assigned_role=assigned_role,
                entry_type=entry_type,
                sheet_name=sheet_name_meta,
                description=row["Task Description"],
                hours=row["Hours"],
                category=row["Category"],
                source="generated",
                status="pending"
            )

            db.add(entry)
            db.flush()

            aggregated_memos = meta.get("aggregated_memos")

            # PARLEGAL AUDIT
            if entry_type == "direct" and audit_index < len(audit_log):

                audit = audit_log[audit_index]

                memos = audit["paralegal_memos"]
                if isinstance(memos, str):
                    memos = [memos]

                original_memo = " | ".join(memos)

                audit_index += 1

            # ATTORNEY AUDIT
            elif entry_type == "derived":
                original_memo = " | ".join(aggregated_memos or [])
            else:
                original_memo = None


            # SAVE AUDIT (for BOTH)
            if original_memo:

                audit_row = LedgerEntryAudit(
                    entry_id=entry.id,
                    original_memo=original_memo,
                    normalized_text=None,
                    primary_category=None,
                    secondary_category=None,
                    confidence=None,
                    cluster_size=None,
                    cluster_date=None,
                    mixed=None,
                    generated_description=row["Task Description"],
                    generated_hours=row["Hours"],
                    generated_category=row["Category"],
                    flag_reason=None,
                    entry_type=entry_type,
                    assigned_role=assigned_role
                )

                db.add(audit_row)
                # audit_index += 1

        print(f"Finished processing sheet: {sheet_name}")

        # delay between sheets
        if i < len(sheet_names) - 1:
            await asyncio.sleep(5)

    run.generated_rows = total_generated_rows
    db.commit()

    return run.run_id