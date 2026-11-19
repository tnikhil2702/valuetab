import argparse
import asyncio
import os

from PeakTrac.neos_client import (
    NeosClient, NeosRequestError, case_search_body, query_filter, DEFAULT_COLUMNS,
)

COLS = DEFAULT_COLUMNS + ["CaseName"]
LAST_NAME_CANDIDATES = [
    ("CaseName", "StartsWith"),
    ("CaseName", "Contains"),
    ("FirstPartyLastName", "Equal"),
    ("FirstPartyFullName", "Contains"),
    ("CaseName", "Like"),
]


def _rows(data):
    if isinstance(data, dict):
        for k in ("Results", "results"):
            if k in data:
                return data[k] or []
    return []


def _total(data):
    return data.get("TotalResultsCount") if isinstance(data, dict) else None


def _val(row, key):
    return row.get(key) if isinstance(row, dict) else None


async def run_search(c, label, filters):
    try:
        data = await c.advanced_search(case_search_body(filters, take=25, columns=COLS))
    except NeosRequestError as e:
        print(f"  {label:<46} FAIL  HTTP {e.status}")
        return None
    return data


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-number", required=True, help="a known OPEN case number")
    ap.add_argument("--closed-case-number", help="a known CLOSED case number (optional)")
    ap.add_argument("--last-name", required=True, help="last name of a known open client")
    a = ap.parse_args()

    c = NeosClient(os.environ["NEOS_ACCESS_TOKEN"], os.environ["NEOS_REFRESH_TOKEN"])
    try:
        print("\n0. Baseline (no filter)")
        base = await run_search(c, "all cases", [])
        base_total = _total(base) if base else None
        print(f"  total={base_total}")
        if base and _rows(base):
            print("  row keys (names only):", sorted(_rows(base)[0].keys()))

        print("\n0b. Which column holds the client name? (counts only)")
        for col in ("CaseName", "FirstPartyFullName", "FirstPartyLastName"):
            try:
                d = await c.advanced_search(case_search_body([], take=25, columns=["CaseNumber", col]))
                filled = sum(bool(_val(r, col)) for r in _rows(d))
                print(f"  {col:<22} HTTP ok, filled in {filled}/{len(_rows(d))} rows")
            except NeosRequestError as e:
                print(f"  {col:<22} FAIL HTTP {e.status}")

        print("\n1. CaseNumber Equal (already proven, sanity check)")
        d = await run_search(c, f"CaseNumber Equal {a.case_number}",
                             [query_filter("CaseNumber", a.case_number)])
        if d:
            print(f"  total={_total(d)}  -> {'PASS' if _total(d) == 1 else 'CHECK'}")

        print("\n2. IsClosed = false")
        winner = None
        for label, value in (("FilterValue 'false' (string)", "false"),
                             ("FilterValue False (boolean)", False),
                             ("FilterValue 'False' (string)", "False")):
            d = await run_search(c, label, [query_filter("IsClosed", value)])
            if not d:
                continue
            rows = _rows(d)
            flags = [str(_val(r, "IsClosed")).lower() for r in rows]
            all_open = bool(rows) and all(f == "false" for f in flags)
            narrower = base_total is not None and _total(d) is not None and _total(d) < base_total
            ok = all_open and narrower
            print(f"  {label:<46} {'PASS' if ok else 'FAIL'}  total={_total(d)} "
                  f"rows={len(rows)} open_rows={flags.count('false')}")
            if ok and winner is None:
                winner = (label, value)
        print("  RESULT:", f"use {winner[0]}" if winner else
              "filter NOT usable -> filter on the server after the query (fallback in plan)")

        print("\n3. Last-name filter candidates")
        for binding, op in LAST_NAME_CANDIDATES:
            d = await run_search(c, f"{binding} {op} '{a.last_name}'",
                                 [query_filter(binding, a.last_name, op)])
            if not d:
                continue
            rows = _rows(d)
            hits = sum(a.last_name.lower() in str(_val(r, "CaseName") or "").lower() for r in rows)
            narrower = base_total is not None and _total(d) is not None and _total(d) < base_total
            ok = bool(rows) and hits == len(rows) and narrower
            print(f"    -> {'PASS' if ok else 'FAIL'}  total={_total(d)} rows={len(rows)} "
                  f"rows_containing_name={hits}")

        if a.closed_case_number:
            print("\n4. Combined: IsClosed=false AND that closed case number must return 0")
            if winner:
                d = await run_search(c, "closed case + IsClosed=false",
                                     [query_filter("CaseNumber", a.closed_case_number),
                                      query_filter("IsClosed", winner[1])])
                if d:
                    print(f"  total={_total(d)} -> {'PASS' if _total(d) == 0 else 'FAIL'}")
    finally:
        await c.aclose()