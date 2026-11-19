from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from PeakTrac.neos_details import _find_row
from PeakTrac.neos_expenses import _date, _first, _money, _records
from PeakTrac.neos_session import get_neos_client

TRUST_HINTS = ("trust", "deposit", "ledger", "iolta", "receipt", "fund", "clientaccount")
AMOUNT_KEYS = ("Amount", "TotalAmount", "TransactionAmount")
DATE_KEYS = ("Date", "TransactionDate", "PaidDate", "DatePaid")


def _walk_strings(node: Any, out: list[tuple[str, str]], path: str = "") -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _walk_strings(v, out, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for v in node:
            _walk_strings(v, out, path)
    elif node is not None:
        out.append((path, str(node)))


def classify(records: list[dict], paid_date: date | None, amount: Decimal | None,
             window_days: int = 3) -> dict:
    hints: set[str] = set()
    exact: list[int] = []
    amt_only: list[int] = []
    for i, rec in enumerate(records):
        pairs: list[tuple[str, str]] = []
        _walk_strings(rec, pairs)
        for key, text in pairs:
            low_key = key.casefold()
            low_text = text.casefold()
            if any(h in low_key for h in TRUST_HINTS):
                hints.add(f"field name: {key.split('.')[-1]}")
            if key.split(".")[-1].casefold() in ("type", "transactiontype", "category") and \
                    any(h in low_text for h in TRUST_HINTS):
                hints.add(f"{key.split('.')[-1]} value: {text[:40]}")
        rec_amt = _money(_first(rec, AMOUNT_KEYS))
        rec_date = _date(_first(rec, DATE_KEYS))
        if amount is not None and rec_amt is not None and abs(abs(rec_amt) - abs(amount)) <= Decimal("0.01"):
            amt_only.append(i)
            if paid_date and rec_date and abs((rec_date - paid_date).days) <= window_days:
                exact.append(i)

    if not records:
        verdict, why = "NO_DATA", "The call returned no records for this case. Try another case."
    elif exact and not hints:
        verdict, why = "LIKELY_COST_PAYMENTS", (
            "A record matches the known payment on amount and date, and nothing in the "
            "records mentions trust, deposits or a ledger.")
    elif hints and not exact:
        verdict, why = "LIKELY_TRUST_DEPOSITS", (
            "Records carry trust/deposit/ledger wording and none matches the known payment.")
    else:
        verdict, why = "INCONCLUSIVE", (
            "Mixed or missing evidence (match: %s, trust wording: %s). A person who knows "
            "the Neos Funds screen must decide." % (bool(exact), bool(hints)))
    return {"record_count": len(records), "amount_matches": len(amt_only),
            "amount_and_date_matches": len(exact), "trust_hints": sorted(hints),
            "verdict": verdict, "why": why}

def render_note(case_label: str, result: dict, field_names: list[str]) -> str:
    follow_up = {
        "LIKELY_COST_PAYMENTS": "Open a follow-up: use the paid date as the interest-accrual start. Needs Finance sign-off.",
        "LIKELY_TRUST_DEPOSITS": "Note that these are trust deposits and drop PT-8 from scope.",
    }.get(result["verdict"], "Do not act yet. Re-run on a second case or ask the Neos admin what the Funds screen shows.")
    return (
        "# PT-8: what GET /valueTransactions means\n\n"
        f"- Checked on: {date.today().isoformat()} (read-only, one case: {case_label})\n"
        f"- Records returned: {result['record_count']}\n"
        f"- Matches known payment (amount): {result['amount_matches']}; (amount + date): {result['amount_and_date_matches']}\n"
        f"- Trust/deposit wording found: {', '.join(result['trust_hints']) or 'none'}\n"
        f"- Fields seen: {', '.join(field_names)}\n\n"
        f"**Probe verdict:** {result['verdict']}. {result['why']}\n\n"
        f"**Follow-up:** {follow_up}\n\n"
        "**Human sign-off (required):** ______________________  Date: ____________\n"
    )

async def main_async(args: argparse.Namespace) -> int:
    client = get_neos_client()
    try:
        row = await _find_row(client, args.case_number)
        if not row or not row.get("Id"):
            print("Case not found in Neos.")
            return 1
        payload = await client.get("/valueTransactions", params={"caseId": row["Id"], "pageSize": 50})
        records = _records(payload)
        names = sorted({str(k) for r in records for k in r.keys()})
        result = classify(
            records,
            datetime.strptime(args.paid_date, "%Y-%m-%d").date() if args.paid_date else None,
            Decimal(args.amount) if args.amount else None,
        )
        print(f"Records: {result['record_count']}   Fields: {', '.join(names)}")
        for r in records[:10]:
            print("  ", {k: r.get(k) for k in ("Date", "Provider", "Amount", "Reference") if k in r})
        print(f"\nVerdict: {result['verdict']}\n{result['why']}")
        if args.write_note:
            Path(args.write_note).write_text(render_note(f"#{args.case_number}", result, names), encoding="utf-8")
            print(f"Draft note written to {args.write_note} (needs human sign-off).")
        return 0
    finally:
        await client.aclose()