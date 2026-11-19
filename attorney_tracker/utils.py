# utils.py
import math
import uuid
from datetime import datetime, date
from io import BytesIO

import numpy as np
import pandas as pd
from fastapi import HTTPException

from db import AttorneyTrackerRecord, AttorneyQuickBookUpload, Session, QuickbookTransaction

FORMULA_VERSION = "v1"
DEFAULT_THRESHOLD = 300_000

LARGE_CASE_THRESHOLD = 100_000

THRESHOLD_OPTIONS = [
    {"label": "Over $250,000",   "value": 250_000},
    {"label": "Over $500,000",   "value": 500_000},
    {"label": "Over $1,000,000", "value": 1_000_000},
    {"label": "Top 5%",          "value": "top_5pct"},  # resolved at query time
    {"label": "Custom",          "value": None},          # admin enters manually
]

KNOWN_DIVISIONS = {
    "Litigation",
    "Pre-Litigation",
    "Arizona",
    "Arizona Pre-Litigation",
    "LitPath",
}

_DIVISION_MERGE_ALIASES = {
    "pre-lit":         "Pre-Litigation",
    "pre lit":         "Pre-Litigation",
    "az":              "Arizona",
    "az pre-lit":      "Arizona Pre-Litigation",
    "arizona pre-lit": "Arizona Pre-Litigation",
    "litpath":         "LitPath",
}

QUICKBOOK_SIGNATURE_COLUMNS = {
    "type", "date", "credit", "debit", "class",
    "case type description", "case number", "balance",
}
QUICKBOOK_MIN_SIGNATURE_MATCHES = 4

QUICKBOOK_CASE_TYPE_MERGE_MAP = {
    "motor vehicle accident": "Motor Vehicle Crash",
    "motor vehicle crash":    "Motor Vehicle Crash",
    "premises liability":            "Premises / Slip & Fall",
    "premises case rif in house":    "Premises / Slip & Fall",
}

COLUMN_ALIASES = {
    "case_id": [
        "case_id",
        "case number",
        "case_number",
        "case id",
    ],
    "settlement_date": [
        "settlement_date",
        "settlement date",
    ],
    "month": [
        "month",
        "reporting_month",
        "reporting month",
    ],
    "attorney": [
        "attorney",
        "attorney_name",
        "attorney name",
        "lawyer",
    ],
    "division": [
        "division",
        "department",
        "practice_area",
        "practice group",
    ],
    "settlement_value": [
        "settlement_value",
        "settlement amount",
        "settlement value",
        "total settlement value",
    ],

    "legal_fee_value": [
        "legal fee value",
        "legal_fee_value",
        "legal fees",
        "fee value",
    ],

    "case_type": [
        "case type",
        "case_type",
        "type",
    ],
    "is_very_large_case": [
        "is_very_large_case",
        "large_case",
        "large case",
        "is large case",
        "is large",
    ],
    "source_sheet": [
        "source_sheet",
        "source sheet",
        "sheet",
    ],
    "notes": [
        "notes",
        "note",
        "comments",
        "comment",
    ],
}

MONTHLY_METRIC_ALIASES = {
    "record_id": ["record id", "record_id"],
    "source_file": ["source file", "source_file"],
    "source_sheet": ["source sheet", "source_sheet", "sheet"],
    "detected_format": ["detected format", "detected_format"],
    "year": ["year", "reporting year"],
    "reporting_month": ["reporting month", "reporting_month", "month"],
    "month_number": ["month number", "month_number"],
    "division": ["division", "department", "practice_area", "practice group"],
    "attorney": ["attorney", "attorney_name", "attorney name", "lawyer"],
    "legal_fees_collected": [
        "legal fees collected",
        "legal_fees_collected",
        "attorney fees collected",
        "fees collected",
    ],
    "cases_distributed": [
        "cases distributed",
        "cases_distributed",
        "number of cases distributed",
    ],
    "monthly_fee_reduction": [
        "monthly fee reduction",
        "monthly_fee_reduction",
        "fee reduction",
    ],
    "active_caseload_snapshot": [
        "active caseload snapshot",
        "active_caseload_snapshot",
        "active caseload",
    ],
    "total_settlement_value": [
        "total settlement value",
        "total_settlement_value",
        "settlement value",
        "settlement_value",
    ],
    "adjusted_value_excluding_large_cases": [
        "adjusted value excluding large cases",
        "adjusted_value_excluding_large_cases",
        "adjusted settlement value",
        "adjusted value",
    ],
    "large_case_count": [
        "large case count",
        "large_case_count",
        "high end resolution count",
    ],
    "large_case_value": [
        "large case value",
        "large_case_value",
        "fees over 100k",
        "high end value",
    ],
    "settlement_count": [
        "settlement count",
        "settlement_count",
        "number of settlements",
    ],
    "notes": ["notes", "note", "comments", "comment"],
}

ANNUAL_ADJUSTMENT_ALIASES = {
    "source_file":       ["source file", "source_file"],
    "source_sheet":      ["source sheet", "source_sheet", "sheet"],
    "detected_format":   ["detected format", "detected_format"],
    "year":              ["year", "reporting year"],
    "division":          ["division", "department"],
    "attorney":          ["attorney", "attorney_name", "attorney name", "lawyer"],
    "metric_name":       ["metric name", "metric_name", "metric"],
    "metric_value":      ["metric value", "metric_value", "value"],
    "metric_category":   ["metric category", "metric_category", "category"],
    "is_adjusted_metric":["is adjusted metric", "is_adjusted_metric", "adjusted"],
    "notes":             ["notes", "note", "comments", "comment"],
}

MONTHLY_METRIC_SHEET_NAMES = {
    "monthly attorney metrics",
    "monthly metrics",
    "monthly_attorney_metrics",
    "attorney metrics",
    "attorney_metrics",
    "monthly",
}

ANNUAL_ADJUSTMENT_SHEET_NAMES = {
    "annual adjustments",
    "annual_adjustments",
    "annual",
    "adjustments",
    "year end",
    "year_end",
}

CASE_LEVEL_SHEET_NAMES = {
    "case-level settlements",
    "case level settlements",
    "case_level_settlements",
    "raw_settlements",
    "raw settlements",
    "settlements",
    "data",
    "sheet1",
}


# Helpers
def _warning(message: str, severity: str = "Medium") -> dict:
    """Return a structured warning dict the frontend can act on."""
    return {"message": message, "severity": severity}


def _coerce_numeric(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if s in ("", "#DIV/0!", "#N/A", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#NULL!"):
        return None
    if s.lower() in ("start month", "n/a", "na", "-"):
        return None
    s = s.replace("$", "").replace(",", "")
    try:
        return float(s)
    except (ValueError, TypeError):
        return None

def _month_name_to_number(month_str) -> int | None:
    """Convert month name or number string to int 1-12, or None."""
    if month_str is None:
        return None
    month_map = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    s = str(month_str).strip().lower()
    if s in month_map:
        return month_map[s]
    try:
        n = int(float(s))
        if 1 <= n <= 12:
            return n
    except (ValueError, TypeError):
        pass
    return None


def _build_reporting_month_date(year, month_str, month_number=None):
    """Build a date(year, month, 1) from year + month name (or number)."""
    month_num = _month_name_to_number(month_str) or _month_name_to_number(month_number)
    if not month_num or not year:
        return None
    try:
        return date(int(year), month_num, 1)
    except (ValueError, TypeError):
        return None


def clean_value(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def normalize_columns(df: pd.DataFrame, alias_map: dict | None = None) -> pd.DataFrame:
    """Rename df columns according to alias_map (defaults to COLUMN_ALIASES)."""
    if alias_map is None:
        alias_map = COLUMN_ALIASES
    column_mapping = {}
    lower_columns = {col.lower().strip(): col for col in df.columns}
    for standard_name, aliases in alias_map.items():
        for alias in aliases:
            alias = alias.lower().strip()
            if alias in lower_columns:
                column_mapping[lower_columns[alias]] = standard_name
                break
    return df.rename(columns=column_mapping)


def _detect_sheet_format(sheet_name: str) -> str:
    """Return 'monthly_metric' | 'annual_adjustment' | 'case_level' | 'unknown'."""
    s = sheet_name.lower().strip()
    if s in MONTHLY_METRIC_SHEET_NAMES:
        return "monthly_metric"
    if s in ANNUAL_ADJUSTMENT_SHEET_NAMES:
        return "annual_adjustment"
    if s in CASE_LEVEL_SHEET_NAMES:
        return "case_level"
    return "unknown"

def _read_sheet_with_header_detection(
    workbook: pd.ExcelFile,
    sheet_name: str,
    max_scan_rows: int = 10,
) -> pd.DataFrame:
    _HEADER_KEYWORDS = {
        "record id", "record_id",
        "attorney", "attorney name", "attorney_name",
        "division", "department",
        "reporting month", "reporting_month",
        "year",
        "legal fees collected", "legal_fees_collected",
        "cases distributed", "cases_distributed",
        "month number", "month_number",
    }
    df_scan = pd.read_excel(workbook, sheet_name=sheet_name, header=None, nrows=max_scan_rows)
    detected_header_row = 0  # default — preserves original behaviour
    for row_idx, row in df_scan.iterrows():
        cell_values = {
            str(v).strip().lower()
            for v in row
            if pd.notna(v) and str(v).strip()
        }
        if len(cell_values & _HEADER_KEYWORDS) >= 3:
            detected_header_row = row_idx
            break
    return pd.read_excel(workbook, sheet_name=sheet_name, header=detected_header_row)


def read_all_sheets(contents: bytes) -> dict[str, tuple[str, pd.DataFrame]]:
    workbook = pd.ExcelFile(BytesIO(contents))
    result = {}
    for sheet_name in workbook.sheet_names:
        fmt = _detect_sheet_format(sheet_name)
        df = pd.read_excel(workbook, sheet_name=sheet_name)
        result[sheet_name] = (fmt, df)
    return result


def read_raw_settlements(contents: bytes) -> pd.DataFrame:
    """Parse uploaded file bytes and return the raw DataFrame (case-level only)."""
    workbook = pd.ExcelFile(BytesIO(contents))
    matched = next(
        (s for s in workbook.sheet_names if s.lower().strip() in CASE_LEVEL_SHEET_NAMES),
        workbook.sheet_names[0],
    )
    return pd.read_excel(workbook, sheet_name=matched)


def resolve_threshold(threshold, raw_df: pd.DataFrame) -> float:
    if threshold == "top_5pct":
        for col in ("legal_fees_collected", "settlement_value", "total_settlement_value"):
            if col in raw_df.columns and not raw_df.empty:
                return float(raw_df[col].quantile(0.95))
        return DEFAULT_THRESHOLD
    try:
        return float(threshold)
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD


def _has_at_least_one_metric(record) -> bool:
    metric_fields = [
        "settlement_count",
        "legal_fees_collected",
        "cases_distributed",
        "monthly_fee_reduction",
        "active_caseload_snapshot",
        "total_settlement_value",
        "adjusted_value_excluding_large_cases",
        "large_case_count",
        "large_case_value",
    ]
    
    for field in metric_fields:
        value = getattr(record, field, None)
        if value is not None:
            return True
    
    return False


def validate_monthly_metric_record(record) -> list[str]:
    errors = []

    if not record.attorney_name:
        errors.append("Attorney name missing")
    if not record.division_name:
        errors.append("Division missing")
    elif record.division_name not in KNOWN_DIVISIONS:
        # Check for common merge aliases that indicate a mis-filed division.
        alias_key = str(record.division_name).strip().lower()
        canonical = _DIVISION_MERGE_ALIASES.get(alias_key)
        if canonical:
            errors.append(
                f"Division '{record.division_name}' appears to be a merge alias "
                f"for '{canonical}'. Use the canonical division name."
            )
        else:
            errors.append(
                f"Unknown division '{record.division_name}'. "
                f"Valid: {', '.join(sorted(KNOWN_DIVISIONS))}"
            )
    if record.reporting_month is None:
        errors.append("Reporting month missing")

    # At least one metric field must be present
    if not _has_at_least_one_metric(record):
        errors.append(
            "At least one metric field must be populated (e.g., Settlement Count, "
            "Legal Fees Collected, Cases Distributed, Monthly Fee Reduction, "
            "Active Caseload Snapshot, Total Settlement Value, Large Case Count, etc.)"
        )

    return errors


def validate_record(record) -> list[str]:
    errors = []
    if not record.case_id:
        errors.append("Case ID missing")
    if not record.attorney_name:
        errors.append("Attorney name missing")
    if not record.division_name:
        errors.append("Division missing")
    elif record.division_name not in KNOWN_DIVISIONS:
        alias_key = str(record.division_name).strip().lower()
        canonical = _DIVISION_MERGE_ALIASES.get(alias_key)
        if canonical:
            errors.append(
                f"Division '{record.division_name}' appears to be a merge alias "
                f"for '{canonical}'. Use the canonical division name."
            )
        else:
            errors.append(
                f"Unknown division '{record.division_name}'. "
                f"Valid: {', '.join(sorted(KNOWN_DIVISIONS))}"
            )
    if record.settlement_value is None:
        errors.append("Settlement value missing")
    return errors


def validate_upload_ready(db: Session, upload_id: int):
    invalid_count = (
        db.query(AttorneyTrackerRecord)
        .filter(
            AttorneyTrackerRecord.upload_id == upload_id,
            AttorneyTrackerRecord.is_valid == False,
        )
        .count()
    )
    if invalid_count:
        raise HTTPException(
            status_code=400,
            detail=f"{invalid_count} rows require correction before analysis",
        )


def validate_monthly_metrics_df(
    df: pd.DataFrame,
    threshold: float = DEFAULT_THRESHOLD,
) -> tuple[pd.DataFrame, dict]:
    validation_summary = {
        "total_rows": len(df),
        "duplicate_rows_removed": 0,
        "null_rows_removed": 0,
        "warnings": [],
        "format": "monthly_metric",
    }

    # 1. Drop exact duplicates
    before = len(df)
    df = df.drop_duplicates()
    validation_summary["duplicate_rows_removed"] = before - len(df)

    # 2. Coerce numerics — #DIV/0!, 'Start Month', etc. → null
    numeric_cols = [
        "legal_fees_collected",
        "cases_distributed",
        "large_case_count",
        "large_case_value",
        "monthly_fee_reduction",
        "active_caseload_snapshot",
        "total_settlement_value",
        "adjusted_value_excluding_large_cases",
        "settlement_count",
        "month_number",
        "year",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = df[col].apply(_coerce_numeric)

    # 3. Required identity field checks
    required = ["attorney", "division", "reporting_month"]
    for col in required:
        if col not in df.columns:
            validation_summary["warnings"].append(
                _warning(f"Required column '{col}' not found in upload", "High")
            )
            continue
        null_count = df[col].isnull().sum()
        if null_count:
            validation_summary["warnings"].append(
                _warning(f"{null_count} rows missing '{col}'", "High")
            )

    # 4. Check at least one metric field per row
    metric_cols = [
        "settlement_count",
        "legal_fees_collected",
        "cases_distributed",
        "monthly_fee_reduction",
        "active_caseload_snapshot",
        "total_settlement_value",
        "adjusted_value_excluding_large_cases",
        "large_case_count",
        "large_case_value",
    ]
    present_metric_cols = [col for col in metric_cols if col in df.columns]
    
    if present_metric_cols:
        rows_with_no_metrics = df[df[present_metric_cols].isnull().all(axis=1)]
        if not rows_with_no_metrics.empty:
            validation_summary["warnings"].append(
                _warning(
                    f"{len(rows_with_no_metrics)} rows have all metric fields blank. "
                    "Each row must have at least one metric value populated.",
                    "High"
                )
            )

    if "legal_fees_collected" in df.columns:
        blank_fees = df["legal_fees_collected"].isnull().sum()
        if blank_fees:
            validation_summary["warnings"].append(
                _warning(
                    f"{blank_fees} rows have blank Legal Fees Collected. "
                    "This is acceptable if other metrics are present. "
                    "Enter 0 if the attorney worked but collected $0.",
                    "Low",
                )
            )
        neg_fees = df[df["legal_fees_collected"].notna() & (df["legal_fees_collected"] < 0)]
        if not neg_fees.empty:
            validation_summary["warnings"].append(
                _warning(f"{len(neg_fees)} rows have negative Legal Fees Collected", "High")
            )


    if "active_caseload_snapshot" in df.columns:
        blank_caseload = df["active_caseload_snapshot"].isnull().sum()
        if blank_caseload == len(df):
            validation_summary["warnings"].append(
                _warning(
                    "Active Caseload Snapshot column is entirely blank. "
                    "This is a month-end snapshot, not a running total — "
                    "leave blank only if your division does not track it.",
                    "Low",
                )
            )

    if "monthly_fee_reduction" in df.columns:
        neg_reduction = df[
            df["monthly_fee_reduction"].notna() & (df["monthly_fee_reduction"] < 0)
        ]
        if not neg_reduction.empty:
            validation_summary["warnings"].append(
                _warning(
                    f"{len(neg_reduction)} rows have negative Monthly Fee Reduction. "
                    "Fee reductions should be positive values.",
                    "High",
                )
            )

    if "division" in df.columns:
        unknown_divs = set(df["division"].dropna().unique()) - KNOWN_DIVISIONS
        if unknown_divs:
            # Check for merge aliases first
            merge_warnings = []
            truly_unknown = []
            for div in unknown_divs:
                alias_key = str(div).strip().lower()
                canonical = _DIVISION_MERGE_ALIASES.get(alias_key)
                if canonical:
                    merge_warnings.append(
                        f"'{div}' looks like a merge alias for '{canonical}' — "
                        "use the canonical name to avoid incorrect roll-ups."
                    )
                else:
                    truly_unknown.append(div)
            if merge_warnings:
                validation_summary["warnings"].append(
                    _warning(
                        "Division merge alias detected: " + "; ".join(merge_warnings),
                        "High",
                    )
                )
            if truly_unknown:
                validation_summary["warnings"].append(
                    _warning(
                        f"Unknown division(s): {', '.join(sorted(truly_unknown))}. "
                        f"Valid divisions: {', '.join(sorted(KNOWN_DIVISIONS))}",
                        "Medium",
                    )
                )

    if "attorney" in df.columns:
        names = df["attorney"].dropna().astype(str).unique().tolist()
        prefix_map: dict = {}
        for name in names:
            key = name.strip()[:6].lower()
            prefix_map.setdefault(key, [])
            if name not in prefix_map[key]:
                prefix_map[key].append(name)
        variations = {k: v for k, v in prefix_map.items() if len(v) > 1}
        if variations:
            examples = [" vs ".join(v) for v in list(variations.values())[:3]]
            validation_summary["warnings"].append(
                _warning(
                    "Possible attorney spelling variations detected: " + "; ".join(examples),
                    "Medium",
                )
            )

    key_cols = [c for c in ["attorney", "division", "reporting_month"] if c in df.columns]
    if len(key_cols) == 3:
        dup_rows = df[df.duplicated(subset=key_cols, keep=False)]
        if not dup_rows.empty:
            validation_summary["warnings"].append(
                _warning(
                    f"{len(dup_rows)} duplicate attorney/division/month rows found. "
                    "Review before approval.",
                    "Medium",
                )
            )

    return df, validation_summary


def validate_raw_settlements(
    raw_df: pd.DataFrame,
    threshold: float = DEFAULT_THRESHOLD,
) -> tuple[pd.DataFrame, dict]:
    validation_summary = {
        "total_rows": len(raw_df),
        "duplicate_rows_removed": 0,
        "null_rows_removed": 0,
        "warnings": [],
        "format": "case_level",
    }

    # 1. Remove exact duplicate rows
    before = len(raw_df)
    raw_df = raw_df.drop_duplicates()
    validation_summary["duplicate_rows_removed"] = before - len(raw_df)

    required_columns = [
        "case_id",
        "settlement_date",
        "month",
        "attorney",
        "division",
        "settlement_value",
    ]

    # 2. Missing columns / missing values
    for column in required_columns:
        if column not in raw_df.columns:
            severity = "High" if column in ("case_id", "attorney", "settlement_value") else "Medium"
            validation_summary["warnings"].append(
                _warning(f"Column '{column}' not found in upload", severity)
            )
            continue
        null_count = raw_df[column].isnull().sum()
        if null_count > 0:
            severity = "High" if column in ("case_id", "attorney", "settlement_value") else "Medium"
            validation_summary["warnings"].append(
                _warning(f"{null_count} rows missing {column}", severity)
            )

    # 3. Settlement value validation
    if "settlement_value" in raw_df.columns:
        raw_df["settlement_value"] = pd.to_numeric(raw_df["settlement_value"], errors="coerce")
        invalid_values = raw_df["settlement_value"].isna().sum()
        if invalid_values:
            validation_summary["warnings"].append(
                _warning(f"{invalid_values} rows contain invalid settlement values", "High")
            )
        non_positive = raw_df[raw_df["settlement_value"] <= 0].dropna(subset=["settlement_value"])
        if not non_positive.empty:
            validation_summary["warnings"].append(
                _warning(f"{len(non_positive)} rows have negative or zero settlement values", "High")
            )
        unusual = raw_df[raw_df["settlement_value"] > 5_000_000]
        if not unusual.empty:
            validation_summary["warnings"].append(
                _warning(f"{len(unusual)} unusually large settlements found (> $5M)", "Review")
            )

    # 3b. Legal Fee Value (new v2 column) — warn if negative
    if "legal_fee_value" in raw_df.columns:
        raw_df["legal_fee_value"] = pd.to_numeric(raw_df["legal_fee_value"], errors="coerce")
        neg_fees = raw_df[raw_df["legal_fee_value"] < 0].dropna(subset=["legal_fee_value"])
        if not neg_fees.empty:
            validation_summary["warnings"].append(
                _warning(f"{len(neg_fees)} rows have negative Legal Fee Value", "High")
            )

    # 4. Duplicate case IDs
    if "case_id" in raw_df.columns:
        dup_case_ids = raw_df[raw_df.duplicated(subset=["case_id"], keep=False)]
        if not dup_case_ids.empty:
            validation_summary["warnings"].append(
                _warning(f"{len(dup_case_ids)} duplicate case_id records found", "Medium")
            )

    # 5. Duplicate attorney/month combinations
    if "attorney" in raw_df.columns and "month" in raw_df.columns:
        dup_atty_month = raw_df[raw_df.duplicated(subset=["attorney", "month"], keep=False)]
        if not dup_atty_month.empty:
            examples = (
                dup_atty_month[["attorney", "month"]]
                .drop_duplicates()
                .head(3)
                .apply(lambda r: f"{r['attorney']} / {r['month']}", axis=1)
                .tolist()
            )
            validation_summary["warnings"].append(
                _warning(
                    f"{len(dup_atty_month)} duplicate attorney/month rows found "
                    f"(e.g. {', '.join(examples)}). Review before approval.",
                    "Medium",
                )
            )

    # 6. Division split rules (same as monthly metric)
    if "division" in raw_df.columns:
        unknown_divs = set(raw_df["division"].dropna().unique()) - KNOWN_DIVISIONS
        if unknown_divs:
            merge_warnings = []
            truly_unknown = []
            for div in unknown_divs:
                alias_key = str(div).strip().lower()
                canonical = _DIVISION_MERGE_ALIASES.get(alias_key)
                if canonical:
                    merge_warnings.append(f"'{div}' → '{canonical}'")
                else:
                    truly_unknown.append(div)
            if merge_warnings:
                validation_summary["warnings"].append(
                    _warning(
                        "Division merge alias(es) detected: " + "; ".join(merge_warnings),
                        "High",
                    )
                )
            if truly_unknown:
                validation_summary["warnings"].append(
                    _warning(f"Unknown division(s): {', '.join(sorted(truly_unknown))}", "Medium")
                )

    # 7. Attorney spelling variations
    if "attorney" in raw_df.columns:
        names = raw_df["attorney"].dropna().astype(str).unique().tolist()
        prefix_map: dict = {}
        for name in names:
            key = name.strip()[:6].lower()
            prefix_map.setdefault(key, [])
            if name not in prefix_map[key]:
                prefix_map[key].append(name)
        variations = {k: v for k, v in prefix_map.items() if len(v) > 1}
        if variations:
            examples = [" vs ".join(v) for v in list(variations.values())[:3]]
            validation_summary["warnings"].append(
                _warning(
                    "Possible attorney spelling variations detected: " + "; ".join(examples),
                    "Medium",
                )
            )

    # 8. Uploaded large-case flag mismatch
    if "is_very_large_case" in raw_df.columns and "settlement_value" in raw_df.columns:
        uploaded_flag = (
            raw_df["is_very_large_case"].astype(str).str.strip().str.lower().isin(["yes", "true", "1"])
        )
        computed_flag = raw_df["settlement_value"] > threshold
        mismatches = raw_df[uploaded_flag != computed_flag]
        if not mismatches.empty:
            validation_summary["warnings"].append(
                _warning(f"{len(mismatches)} row(s) have incorrect is_very_large_case values.", "Review")
            )

    # 9. Records missing required fields
    if all(c in raw_df.columns for c in ["case_id", "attorney", "settlement_value"]):
        would_drop = raw_df[
            raw_df["case_id"].isna()
            | raw_df["attorney"].isna()
            | raw_df["settlement_value"].isna()
        ]
        if not would_drop.empty:
            validation_summary["warnings"].append(
                _warning(
                    f"{len(would_drop)} records are missing required fields and "
                    "need correction before approval.",
                    "High",
                )
            )

    return raw_df, validation_summary

# Core metric calculations
def _outlier_dependence_label(score: float) -> str:
    if score <= 20:
        return "Low"
    if score <= 50:
        return "Moderate"
    return "High"


def calculate_attorney_monthly_metrics(raw_df: pd.DataFrame, threshold: float = DEFAULT_THRESHOLD) -> list[dict]:
    metrics = []

    if "legal_fees_collected" in raw_df.columns:
        for _, row in raw_df.iterrows():
            month = row.get("reporting_month")
            attorney = row.get("attorney")
            division = row.get("division")
            if pd.isna(month) or pd.isna(attorney) or pd.isna(division):
                continue

            legal_fees = float(row.get("legal_fees_collected") or 0)
            settlement_count = int(row.get("settlement_count") or row.get("cases_distributed") or 0)
            large_case_count = int(row.get("large_case_count") or 0)
            large_case_value = float(row.get("large_case_value") or 0)

            explicit_adj = row.get("adjusted_value_excluding_large_cases")
            if explicit_adj is not None and not pd.isna(explicit_adj):
                adjusted_settlement_value = float(explicit_adj)
            else:
                adjusted_settlement_value = legal_fees - large_case_value

            total_settlement_value = float(row.get("total_settlement_value") or legal_fees)
            large_case_share = (
                round((large_case_value / total_settlement_value) * 100, 2)
                if total_settlement_value else 0
            )
            average_settlement = total_settlement_value / settlement_count if settlement_count else 0

            metrics.append({
                "month": month,
                "attorney": attorney,
                "division": division,
                "settlement_count": settlement_count,
                "total_settlement_value": total_settlement_value,
                "legal_fees_collected": legal_fees,
                "cases_distributed": int(row.get("cases_distributed") or 0),
                "monthly_fee_reduction": float(row.get("monthly_fee_reduction") or 0),
                "active_caseload_snapshot": row.get("active_caseload_snapshot"),
                "large_case_count": large_case_count,
                "large_case_value": large_case_value,
                "adjusted_settlement_value": adjusted_settlement_value,
                "average_settlement": float(average_settlement),
                "large_case_share": float(large_case_share),
                "outlier_dependence_label": _outlier_dependence_label(large_case_share),
            })

    # ---- Case-level format ----
    elif "settlement_value" in raw_df.columns:
        grouped = raw_df.groupby(["month", "attorney", "division"])
        for (month, attorney, division), group in grouped:
            settlement_count = len(group)
            total_settlement_value = group["settlement_value"].sum()
            large_case_df = group[group["settlement_value"] > threshold]
            large_case_count = len(large_case_df)
            large_case_value = large_case_df["settlement_value"].sum()
            adjusted_settlement_value = total_settlement_value - large_case_value
            average_settlement = total_settlement_value / settlement_count if settlement_count else 0
            large_case_share = (
                round((large_case_value / total_settlement_value) * 100, 2)
                if total_settlement_value else 0
            )
            metrics.append({
                "month": month,
                "attorney": attorney,
                "division": division,
                "settlement_count": settlement_count,
                "total_settlement_value": float(total_settlement_value),
                "legal_fees_collected": float(total_settlement_value),
                "cases_distributed": settlement_count,
                "monthly_fee_reduction": 0.0,
                "active_caseload_snapshot": None,
                "large_case_count": large_case_count,
                "large_case_value": float(large_case_value),
                "adjusted_settlement_value": float(adjusted_settlement_value),
                "average_settlement": float(average_settlement),
                "large_case_share": float(large_case_share),
                "outlier_dependence_label": _outlier_dependence_label(large_case_share),
            })
    else:
        return []

    metrics_df = pd.DataFrame(metrics)
    if metrics_df.empty:
        return []

    metrics_df = metrics_df.sort_values(["attorney", "division", "month"])

    metrics_df["rolling_3mo_adjusted"] = (
        metrics_df.groupby(["attorney", "division"])["adjusted_settlement_value"]
        .transform(lambda x: x.rolling(window=3, min_periods=1).mean())
        .round(2)
    )
    metrics_df["mom_adjusted_change"] = (
        metrics_df.groupby(["attorney", "division"])["adjusted_settlement_value"]
        .pct_change()
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0)
        .mul(100)
        .round(2)
    )

    return metrics_df.to_dict(orient="records")


def calculate_dashboard_metrics(raw_df: pd.DataFrame, threshold: float = DEFAULT_THRESHOLD) -> dict:
    """Firm-wide dashboard KPIs. Handles both monthly-metric and case-level DataFrames."""
    value_col = "legal_fees_collected" if "legal_fees_collected" in raw_df.columns else "settlement_value"
    month_col = "reporting_month" if "reporting_month" in raw_df.columns else "month"

    total_settlements = len(raw_df)
    total_settlement_value = raw_df[value_col].sum() if value_col in raw_df.columns else 0

    if "large_case_value" in raw_df.columns:
        large_case_value = raw_df["large_case_value"].fillna(0).sum()
        large_case_count = (
            int(raw_df["large_case_count"].fillna(0).sum())
            if "large_case_count" in raw_df.columns else 0
        )
    elif value_col in raw_df.columns:
        large_case_df = raw_df[raw_df[value_col] > threshold]
        large_case_count = len(large_case_df)
        large_case_value = large_case_df[value_col].sum()
    else:
        large_case_count = 0
        large_case_value = 0.0

    if "adjusted_value_excluding_large_cases" in raw_df.columns:
        adjusted_settlement_value = raw_df["adjusted_value_excluding_large_cases"].fillna(0).sum()
    else:
        adjusted_settlement_value = total_settlement_value - large_case_value

    average_settlement = total_settlement_value / total_settlements if total_settlements else 0

    mom_change = mom_change_pct = None
    if month_col in raw_df.columns and value_col in raw_df.columns:
        monthly = raw_df.groupby(month_col)[value_col].sum().sort_index()
        if len(monthly) >= 2:
            prev, curr = float(monthly.iloc[-2]), float(monthly.iloc[-1])
            mom_change = curr - prev
            mom_change_pct = round(((curr - prev) / prev) * 100, 2) if prev else None

    best_division = None
    if "division" in raw_df.columns:
        if "adjusted_value_excluding_large_cases" in raw_df.columns:
            div_adjusted = raw_df.groupby("division")["adjusted_value_excluding_large_cases"].sum()
        else:
            div_adjusted = raw_df.groupby("division").apply(
                lambda g: g[value_col].sum() - g[g[value_col] > threshold][value_col].sum()
            )
        if not div_adjusted.empty:
            best_division = div_adjusted.idxmax()

    total_fee_reduction = (
        float(raw_df["monthly_fee_reduction"].fillna(0).sum())
        if "monthly_fee_reduction" in raw_df.columns else None
    )
    total_cases_distributed = (
        int(raw_df["cases_distributed"].fillna(0).sum())
        if "cases_distributed" in raw_df.columns else None
    )

    trending_up: list[dict] = []
    trending_down: list[dict] = []
    if month_col in raw_df.columns and "attorney" in raw_df.columns and value_col in raw_df.columns:
        months_sorted = sorted(raw_df[month_col].dropna().unique())
        if len(months_sorted) >= 2:
            last_two = months_sorted[-2:]
            last_df = raw_df[raw_df[month_col].isin(last_two)]
            pivot = (
                last_df.groupby(["attorney", "division", month_col])[value_col]
                .sum()
                .unstack(fill_value=0)
            )
            if pivot.shape[1] == 2:
                pivot.columns = ["prev", "curr"]
                pivot = pivot.reset_index()
                trending_up = (
                    pivot[pivot["curr"] > pivot["prev"]][["attorney", "division"]]
                    .to_dict(orient="records")
                )
                trending_down = (
                    pivot[pivot["curr"] < pivot["prev"]][["attorney", "division"]]
                    .to_dict(orient="records")
                )

    return {
        "total_settlements": total_settlements,
        "total_settlement_value": float(total_settlement_value),
        "adjusted_settlement_value": float(adjusted_settlement_value),
        "average_settlement": float(average_settlement),
        "large_case_count": large_case_count,
        "large_case_value": float(large_case_value),
        "total_fee_reduction": total_fee_reduction,
        "total_cases_distributed": total_cases_distributed,
        "mom_change": float(mom_change) if mom_change is not None else None,
        "mom_change_pct": mom_change_pct,
        "best_division": best_division,
        "attorneys_trending_up": trending_up,
        "attorneys_trending_down": trending_down,
    }


def calculate_leaderboard(raw_df: pd.DataFrame, threshold: float = DEFAULT_THRESHOLD) -> list[dict]:
    value_col = "legal_fees_collected" if "legal_fees_collected" in raw_df.columns else "settlement_value"

    leaderboard = []
    grouped = raw_df.groupby(["attorney", "division"])

    for (attorney, division), group in grouped:
        settlement_count = (
            int(group["settlement_count"].sum()) if "settlement_count" in group.columns
            else (
                int(group["cases_distributed"].sum()) if "cases_distributed" in group.columns
                else len(group)
            )
        )
        total_value = group[value_col].sum() if value_col in group.columns else 0
        large_case_value = (
            group["large_case_value"].fillna(0).sum()
            if "large_case_value" in group.columns
            else group[group[value_col] > threshold][value_col].sum()
        )
        adjusted_value = (
            group["adjusted_value_excluding_large_cases"].fillna(0).sum()
            if "adjusted_value_excluding_large_cases" in group.columns
            else total_value - large_case_value
        )
        average_settlement = total_value / settlement_count if settlement_count else 0
        outlier_dependence = round((large_case_value / total_value) * 100, 2) if total_value else 0

        leaderboard.append({
            "attorney": attorney,
            "division": division,
            "settlement_count": settlement_count,
            "total_settlement_value": float(total_value),
            "adjusted_settlement_value": float(adjusted_value),
            "average_settlement": float(average_settlement),
            "outlier_dependence_pct": float(outlier_dependence),
            "outlier_dependence_label": _outlier_dependence_label(outlier_dependence),
        })

    leaderboard.sort(key=lambda x: x["total_settlement_value"], reverse=True)
    for i, row in enumerate(leaderboard, 1):
        row["raw_rank"] = i

    adjusted_sorted = sorted(leaderboard, key=lambda x: x["adjusted_settlement_value"], reverse=True)
    rank_map = {(row["attorney"], row["division"]): i for i, row in enumerate(adjusted_sorted, 1)}
    for row in leaderboard:
        adj_rank = rank_map[(row["attorney"], row["division"])]
        row["adjusted_rank"] = adj_rank
        row["rank_shift"] = row["raw_rank"] - adj_rank

    leaderboard.sort(key=lambda x: x["adjusted_settlement_value"], reverse=True)
    return leaderboard


# Division metric rules (v2 workbook)

DIVISION_METRIC_RULES = {
    "Litigation": [
        {"metric": "Legal Fees Collected",      "enabled": True,    "note": "Core litigation metric"},
        {"metric": "Cases Distributed",         "enabled": True,    "note": "Core litigation workload metric"},
        {"metric": "High End Resolutions",      "enabled": True,    "note": "Outlier metric ($100k threshold)"},
        {"metric": "Monthly Fee Reduction",     "enabled": True,    "note": "Track monthly fee reductions per attorney"},
        {"metric": "Attorney leaderboard rank", "enabled": True,    "note": "Filter by division"},
        {"metric": "Attorney value score",      "enabled": True,    "note": "Composite performance score"},
    ],
    "Pre-Litigation": [
        {"metric": "Legal Fees Collected",         "enabled": True,  "note": "Core pre-lit metric"},
        {"metric": "Cases Distributed",            "enabled": True,  "note": "Core pre-lit workload metric"},
        {"metric": "High End Resolutions",         "enabled": True,  "note": "Outlier metric ($100k threshold)"},
        {"metric": "Monthly Fee Reduction",        "enabled": True,  "note": "Equalization and family/friend discount reductions"},
        {"metric": "Active Caseload Snapshot",     "enabled": True,  "note": "Month-end snapshot — NOT a running total"},
        {"metric": "Attorney leaderboard rank",    "enabled": True,  "note": "Filter by division"},
        {"metric": "Attorney value score",         "enabled": True,  "note": "Composite performance score"},
    ],
    "Arizona": [
        {"metric": "Legal Fees Collected",      "enabled": True,    "note": "Core Arizona (litigation-stage) metric"},
        {"metric": "Cases Distributed",         "enabled": True,    "note": "Core Arizona workload metric"},
        {"metric": "Monthly Fee Reduction",     "enabled": True,    "note": "Track monthly fee reductions"},
        {"metric": "Attorney leaderboard rank", "enabled": True,    "note": "Filter by division"},
        {"metric": "Attorney value score",      "enabled": True,    "note": "Composite performance score"},
    ],
    "Arizona Pre-Litigation": [
        {"metric": "Legal Fees Collected",         "enabled": True,  "note": "Mirrors pre-lit; AZ attorneys tracked separately"},
        {"metric": "Cases Distributed",            "enabled": True,  "note": "AZ pre-lit workload metric"},
        {"metric": "Monthly Fee Reduction",        "enabled": True,  "note": "Track monthly fee reductions"},
        {"metric": "Active Caseload Snapshot",     "enabled": True,  "note": "Month-end snapshot for AZ pre-lit attorneys"},
        {"metric": "Attorney leaderboard rank",    "enabled": True,  "note": "Filter by division"},
        {"metric": "Attorney value score",         "enabled": True,  "note": "Composite performance score"},
    ],
    "LitPath": [
        {"metric": "Legal Fees Collected",         "enabled": True,  "note": "Core LitPath metric — separate from Pre-Litigation"},
        {"metric": "Cases Distributed",            "enabled": True,  "note": "Core LitPath workload metric"},
        {"metric": "High End Resolutions",         "enabled": True,  "note": "LitPath outlier metric — $100k threshold"},
        {"metric": "Monthly Fee Reduction",        "enabled": True,  "note": "Track monthly fee reductions"},
        {"metric": "Active Caseload Snapshot",     "enabled": True,  "note": "LitPath attorney caseload at month end"},
        {"metric": "Attorney leaderboard rank",    "enabled": True,  "note": "Filter by division"},
        {"metric": "Attorney value score",         "enabled": True,  "note": "Composite performance score"},
    ],
}


def get_division_metric_rules() -> dict:
    return DIVISION_METRIC_RULES

# Attorney Value Score
def _safe_pct_rank(series: pd.Series) -> pd.Series:
    n = len(series)
    if n == 1:
        return pd.Series([50.0], index=series.index)
    return series.rank(pct=True).mul(100)


def calculate_attorney_value_scores(
    raw_df: pd.DataFrame,
    attorney_monthly_metrics: list[dict],
    threshold: float = DEFAULT_THRESHOLD,
) -> list[dict]:
    """Attorney Value Score formula (v1) — dual-format aware."""
    if not attorney_monthly_metrics:
        return []

    df = pd.DataFrame(attorney_monthly_metrics)
    agg = (
        df.groupby(["attorney", "division"])
        .agg(
            total_adjusted=("adjusted_settlement_value", "sum"),
            total_count=("settlement_count", "sum"),
            total_value=("total_settlement_value", "sum"),
            avg_rolling_3mo=("rolling_3mo_adjusted", "mean"),
            last_mom=("mom_adjusted_change", "last"),
        )
        .reset_index()
    )
    agg["adjusted_avg"] = agg.apply(
        lambda r: r["total_adjusted"] / r["total_count"] if r["total_count"] else 0, axis=1
    )
    consistency = (
        df.groupby(["attorney", "division"])["adjusted_settlement_value"]
        .std()
        .fillna(0)
        .reset_index()
        .rename(columns={"adjusted_settlement_value": "adj_std"})
    )
    agg = agg.merge(consistency, on=["attorney", "division"], how="left")
    agg["adj_std"] = agg["adj_std"].fillna(0)
    agg["outlier_dep"] = agg.apply(
        lambda r: (r["total_value"] - r["total_adjusted"]) / r["total_value"] if r["total_value"] else 0,
        axis=1,
    )

    scores = []
    for division, grp in agg.groupby("division"):
        grp = grp.copy()
        grp["p_adjusted_value"]  = _safe_pct_rank(grp["total_adjusted"])
        grp["p_count"]           = _safe_pct_rank(grp["total_count"])
        grp["p_adj_avg"]         = _safe_pct_rank(grp["adjusted_avg"])
        grp["momentum_score"]    = grp["last_mom"].clip(-100, 100).add(100).div(2)
        grp["consistency_score"] = 100 - _safe_pct_rank(grp["adj_std"])
        grp["outlier_penalty"]   = grp["outlier_dep"].mul(20)
        grp["attorney_value_score"] = (
            0.40 * grp["p_adjusted_value"]
            + 0.20 * grp["p_count"]
            + 0.15 * grp["p_adj_avg"]
            + 0.15 * grp["momentum_score"]
            + 0.10 * grp["consistency_score"]
            - grp["outlier_penalty"]
        ).clip(0, 100).round(1)

        for _, row in grp.iterrows():
            scores.append({
                "attorney": row["attorney"],
                "division": row["division"],
                "attorney_value_score": float(row["attorney_value_score"]),
                "consistency_score": float(round(row["consistency_score"], 1)),
                "outlier_dependence_pct": float(round(row["outlier_dep"] * 100, 2)),
                "outlier_dependence_label": _outlier_dependence_label(row["outlier_dep"] * 100),
                "momentum_score": float(round(row["momentum_score"], 1)),
                "p_adjusted_value": float(round(row["p_adjusted_value"], 1)),
                "p_count": float(round(row["p_count"], 1)),
                "small_division_note": (
                    "Division has ≤ 2 attorneys; percentile scores are neutral (50) "
                    "to avoid misleading rankings."
                ) if len(grp) <= 2 else None,
            })

    scores.sort(key=lambda x: x["attorney_value_score"], reverse=True)
    return scores


# Insights
def generate_insights(
    dashboard: dict,
    leaderboard: list[dict],
    attorney_monthly_metrics: list[dict],
) -> list[dict]:
    """Rule-based insight and alert generator."""
    insights = []

    # 1. High outlier dependence
    high_dep = [r["attorney"] for r in leaderboard if r.get("outlier_dependence_pct", 0) > 50]
    if high_dep:
        insights.append({
            "type": "warning",
            "message": (
            f"{len(high_dep)} attorneys generated more than half of their settlement value from large cases. Firm performance remains heavily influenced by outlier settlements rather than consistent results across the portfolio."
            ),
        })

    # 2. Declining 3-month adjusted trend
    if attorney_monthly_metrics:
        df = pd.DataFrame(attorney_monthly_metrics)
        last_month = df["month"].max()
        declining = df[(df["month"] == last_month) & (df["mom_adjusted_change"] < -10)]
        if not declining.empty:
            insights.append({
                "type": "warning",
                "message": (
                    f"{len(declining)} attorneys experienced a decline of more than 10% in adjusted settlement performance compared with the prior month, indicating potential slowdown in underlying production."
            ),
            })

    # 3. Firm-wide raw vs adjusted divergence
    total = dashboard.get("total_settlement_value", 0)
    adjusted = dashboard.get("adjusted_settlement_value", 0)
    if total and adjusted:
        divergence_pct = round(((total - adjusted) / total) * 100, 1)
        if divergence_pct > 20:
            insights.append({
                "type": "info",
                "message": (
                     f"Approximately {divergence_pct}% of total settlement value came from large cases. Adjusted performance metrics suggest underlying trends may differ from headline settlement results."
                ),
            })
    # 4. Firm-wide MoM change
    mom_pct = dashboard.get("mom_change_pct")

    if mom_pct is not None:
        if mom_pct > 0:
            insights.append({
                "type": "positive",
                "message": (
                    f"Firm-wide settlement value increased {mom_pct:.1f}% "
                    "month-over-month, reflecting stronger overall production."
                ),
            })
        elif mom_pct < 0:
            insights.append({
                "type": "warning",
                "message": (
                    f"Firm-wide settlement value declined {abs(mom_pct):.1f}% "
                    "month-over-month, indicating softer performance compared "
                    "with the previous period."
                ),
            })
        else:
            insights.append({
                "type": "info",
                "message": (
                    "Firm-wide settlement value remained unchanged "
                    "month-over-month."
                ),
            })
    else:
        insights.append({
            "type": "info",
            "message": (
                "Firm-wide month-over-month settlement performance could not "
                "be calculated because there is insufficient prior-period data."
            ),
        })

    # 5. Best division callout
    if dashboard.get("best_division"):
        insights.append({
            "type": "positive",
            "message":  f"{dashboard['best_division']} led all divisions in adjusted settlement value, making it the strongest contributor to firm performance this period.",
        })

    # 6. Rank droppers
    rank_droppers = [r["attorney"] for r in leaderboard if r.get("rank_shift", 0) > 2]
    if rank_droppers:
        insights.append({
            "type": "info",
            "message": (
                f"{len(rank_droppers)} attorneys saw a meaningful drop in ranking when large settlements were excluded, suggesting performance may be concentrated in a relatively small number of high-value outcomes."
            ),
        })

    # 7 & 8. Division MoM improvement and decline
    if attorney_monthly_metrics:
        df = pd.DataFrame(attorney_monthly_metrics)
        if "month" in df.columns and "division" in df.columns:
            months_sorted = sorted(df["month"].dropna().unique())
            if len(months_sorted) >= 2:
                prev_m, curr_m = months_sorted[-2], months_sorted[-1]
                prev_div = df[df["month"] == prev_m].groupby("division")["adjusted_settlement_value"].sum()
                curr_div = df[df["month"] == curr_m].groupby("division")["adjusted_settlement_value"].sum()
                all_divs = prev_div.index.union(curr_div.index)
                div_change = curr_div.reindex(all_divs, fill_value=0) - prev_div.reindex(all_divs, fill_value=0)
                if not div_change.empty:
                    best_div, best_chg = div_change.idxmax(), div_change.max()
                    worst_div, worst_chg = div_change.idxmin(), div_change.min()
                    if best_chg > 0:
                        insights.append({
                            "type": "positive",
                            "message": (
                                f"{best_div} showed the strongest month-over-month improvement, increasing adjusted settlement value by ${best_chg:,.0f}."
                            ),
                        })
                    if worst_chg < 0:
                        insights.append({
                            "type": "warning",
                            "message": (
                                f"{worst_div} recorded the largest month-over-month decline in adjusted settlement value, decreasing by ${abs(worst_chg):,.0f}."
                            ),
                        })

    # 9. High volume, below-average value
    if leaderboard:
        avg_value = sum(r["average_settlement"] for r in leaderboard) / len(leaderboard)
        avg_count = sum(r["settlement_count"] for r in leaderboard) / len(leaderboard)
        high_vol_low_val = [
            r["attorney"]
            for r in leaderboard
            if r["settlement_count"] > avg_count and r["average_settlement"] < avg_value
        ]
        if high_vol_low_val:
            insights.append({
                "type": "info",
                "message": (
                     f"{len(high_vol_low_val)} attorneys are resolving cases at an above-average volume but achieving below-average settlement values, highlighting an opportunity to improve value capture per case."
                ),
            })

    # 10. Divisions where raw rose but adjusted stayed flat or fell
    if attorney_monthly_metrics:
        df = pd.DataFrame(attorney_monthly_metrics)
        if "month" in df.columns and "division" in df.columns:
            months_sorted = sorted(df["month"].dropna().unique())
            if len(months_sorted) >= 2:
                prev_m, curr_m = months_sorted[-2], months_sorted[-1]
                for div in df["division"].dropna().unique():
                    div_df = df[df["division"] == div]
                    prev_row = div_df[div_df["month"] == prev_m]
                    curr_row = div_df[div_df["month"] == curr_m]
                    if prev_row.empty or curr_row.empty:
                        continue
                    if (
                        curr_row["total_settlement_value"].sum() > prev_row["total_settlement_value"].sum()
                        and curr_row["adjusted_settlement_value"].sum()
                        <= prev_row["adjusted_settlement_value"].sum()
                    ):
                        insights.append({
                            "type": "warning",
                            "message": (
                                f"{div} reported higher settlement value than the prior month, but adjusted performance remained flat or declined. Growth appears to have been driven primarily by a small number of large settlements rather than broad-based improvement."
                            ),
                        })

    return insights


# CRUD helpers
def _resolve_upload_any(upload_id: int, db: Session, source: str | None = None):
    query = db.query(AttorneyQuickBookUpload).filter(AttorneyQuickBookUpload.id == upload_id)
    if source in ("tracker", "quickbook"):
        query = query.filter(AttorneyQuickBookUpload.source_type == source)

    upload = query.first()
    if not upload:
        raise HTTPException(status_code=404, detail="Upload not found")
    return upload.source_type, upload


def approve_upload(upload_id: int, db: Session, source: str | None = None):
    kind, _ = _resolve_upload_any(upload_id, db, source=source)
    if kind == "quickbook":
        return approve_quickbook_upload(upload_id=upload_id, db=db)
    return _approve_tracker_upload(upload_id=upload_id, db=db)


def _approve_tracker_upload(upload_id: int, db: Session):
    upload = (
        db.query(AttorneyQuickBookUpload)
        .filter(AttorneyQuickBookUpload.id == upload_id, AttorneyQuickBookUpload.source_type == "tracker")
        .first()
    )
    if not upload:
        raise HTTPException(status_code=404, detail="Upload not found")
    invalid_rows = (
        db.query(AttorneyTrackerRecord)
        .filter(
            AttorneyTrackerRecord.upload_id == upload_id,
            AttorneyTrackerRecord.is_valid == False,
        )
        .count()
    )
    if invalid_rows:
        raise HTTPException(
            status_code=400,
            detail=f"{invalid_rows} invalid rows still exist. Please correct them before approval.",
        )
    upload.status = "APPROVED"
    upload.approved_at = datetime.utcnow()
    db.commit()
    return {"message": "Upload approved successfully", "upload_id": upload_id, "status": upload.status}


def lock_upload(upload_id: int, db: Session, source: str | None = None):
    kind, _ = _resolve_upload_any(upload_id, db, source=source)
    if kind == "quickbook":
        return lock_quickbook_upload(upload_id=upload_id, db=db)
    return _lock_tracker_upload(upload_id=upload_id, db=db)


def _lock_tracker_upload(upload_id: int, db: Session):
    upload = (
        db.query(AttorneyQuickBookUpload)
        .filter(AttorneyQuickBookUpload.id == upload_id, AttorneyQuickBookUpload.source_type == "tracker")
        .first()
    )
    if not upload:
        raise HTTPException(status_code=404, detail="Upload not found")
    if upload.status != "APPROVED":
        raise HTTPException(status_code=400, detail="Upload must be approved before it can be locked.")
    db.query(AttorneyTrackerRecord).filter(
        AttorneyTrackerRecord.upload_id == upload_id
    ).update({"is_locked": True})
    upload.status = "LOCKED"
    db.commit()
    return {"message": "Upload locked successfully. Records are now read-only.", "upload_id": upload_id, "status": upload.status}


def update_attorney_tracker_record(db: Session, record_id: int, payload):
    record = db.query(AttorneyTrackerRecord).filter(AttorneyTrackerRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    if record.is_locked:
        raise HTTPException(status_code=403, detail="Record is locked and cannot be edited.")

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(record, field, value)

    upload = (
        db.query(AttorneyQuickBookUpload)
        .filter(AttorneyQuickBookUpload.id == record.upload_id, AttorneyQuickBookUpload.source_type == "tracker")
        .first()
    )
    threshold = float(upload.threshold) if upload and upload.threshold is not None else DEFAULT_THRESHOLD
    detected_format = getattr(upload, "detected_format", "monthly_metric") or "monthly_metric"

    # Recalculate is_very_large_case
    if "legal_fees_collected" in update_data and record.legal_fees_collected is not None:
        record.is_very_large_case = float(record.legal_fees_collected) > threshold
    elif "settlement_value" in update_data and record.settlement_value is not None:
        record.is_very_large_case = float(record.settlement_value) > threshold

    # Re-validate using the correct validator for the format
    if detected_format == "case_level":
        validation_errors = validate_record(record)
    else:
        validation_errors = validate_monthly_metric_record(record)

    record.is_valid = not validation_errors
    record.validation_errors = ", ".join(validation_errors) if validation_errors else None

    db.commit()
    db.refresh(record)
    return {
        "message": "Record updated successfully",
        "record_id": record.id,
        "is_valid": record.is_valid,
        "validation_errors": record.validation_errors,
    }


def get_uploads_any(db: Session, upload_id: int | None = None, source: str | None = None):
    if upload_id is not None:
        kind, upload = _resolve_upload_any(upload_id, db, source=source)
        if kind == "quickbook":
            result = list_quickbook_uploads(db=db, upload_id=upload_id)
            result["source"] = "quickbook"
            return result
        records = (
            db.query(AttorneyTrackerRecord)
            .filter(AttorneyTrackerRecord.upload_id == upload_id)
            .all()
        )
        return {
            "upload": upload,
            "record_count": len(records),
            "records": records,
            "source": "tracker",
        }

    query = db.query(AttorneyQuickBookUpload)
    if source in ("tracker", "quickbook"):
        query = query.filter(AttorneyQuickBookUpload.source_type == source)
    uploads = query.order_by(AttorneyQuickBookUpload.created_at.desc()).all()

    combined = [{"source": u.source_type, "upload": u} for u in uploads]
    return {"count": len(combined), "uploads": combined}


def update_record_any(db: Session, record_id: int, payload, source: str | None = None):
    tracker_record = None
    quickbook_txn = None
    if source != "quickbook":
        tracker_record = db.query(AttorneyTrackerRecord).filter(AttorneyTrackerRecord.id == record_id).first()
    if source != "tracker":
        quickbook_txn = db.query(QuickbookTransaction).filter(QuickbookTransaction.id == record_id).first()

    if tracker_record and quickbook_txn:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Record id {record_id} exists in both the attorney-tracker and "
                "QuickBooks transaction tables. Pass ?source=tracker or "
                "?source=quickbook to disambiguate."
            ),
        )
    if quickbook_txn:
        return update_quickbook_transaction(db=db, transaction_id=record_id, payload=payload)
    if tracker_record:
        return update_attorney_tracker_record(db=db, record_id=record_id, payload=payload)
    raise HTTPException(status_code=404, detail="Record not found")


def delete_upload_any(upload_id: int, db: Session, source: str | None = None):
    kind, upload = _resolve_upload_any(upload_id, db, source=source)
    if kind == "quickbook":
        return delete_quickbook_upload(upload_id=upload_id, db=db)
    db.query(AttorneyTrackerRecord).filter(AttorneyTrackerRecord.upload_id == upload_id).delete()
    db.delete(upload)
    db.commit()
    return {
        "success": True,
        "message": f"Upload {upload_id} and all associated records deleted successfully",
    }


def get_invalid_records_for_upload(upload_id: int, db: Session, source: str | None = None):
    kind, _ = _resolve_upload_any(upload_id, db, source=source)
    if kind == "quickbook":
        return get_invalid_quickbook_transactions(upload_id=upload_id, db=db)
    return (
        db.query(AttorneyTrackerRecord)
        .filter(
            AttorneyTrackerRecord.upload_id == upload_id,
            AttorneyTrackerRecord.is_valid == False,
        )
        .all()
    )


def create_manual_record(db: Session, payload):
    try:
        settlement_date = datetime.strptime(payload.settlement_date, "%Y-%m-%d").date()
        reporting_month = datetime.strptime(payload.reporting_month, "%Y-%m").date().replace(day=1)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Date parse error: {e}")

    bucket_name = f"MANUAL_{payload.reporting_month}"
    upload = (
        db.query(AttorneyQuickBookUpload)
        .filter(AttorneyQuickBookUpload.file_name == bucket_name, AttorneyQuickBookUpload.source_type == "tracker")
        .first()
    )
    if not upload:
        upload = AttorneyQuickBookUpload(
            upload_uuid=str(uuid.uuid4()),
            file_name=bucket_name,
            source_type="tracker",
            total_rows=0,
            status="PENDING_REVIEW",
            threshold=DEFAULT_THRESHOLD,
            formula_version=FORMULA_VERSION,
            detected_format="case_level",
        )
        db.add(upload)
        db.commit()
        db.refresh(upload)

    threshold = float(upload.threshold) if upload.threshold is not None else DEFAULT_THRESHOLD

    record = AttorneyTrackerRecord(
        upload_id=upload.id,
        case_id=payload.case_id,
        settlement_date=settlement_date,
        reporting_month=reporting_month,
        attorney_name=payload.attorney_name,
        division_name=payload.division_name,
        settlement_value=payload.settlement_value,
        is_very_large_case=payload.settlement_value > threshold,
        source_sheet="MANUAL_ENTRY",
        notes=payload.notes,
        detected_format="case_level",
    )
    errors = validate_record(record)
    record.is_valid = not errors
    record.validation_errors = ", ".join(errors) if errors else None

    db.add(record)
    upload.total_rows = (
        db.query(AttorneyTrackerRecord)
        .filter(AttorneyTrackerRecord.upload_id == upload.id)
        .count()
        + 1
    )
    db.commit()
    db.refresh(record)

    return {
        "message": "Record submitted successfully and is pending admin approval.",
        "record_id": record.id,
        "upload_id": upload.id,
        "status": "PENDING_REVIEW",
        "is_valid": record.is_valid,
        "validation_errors": record.validation_errors,
    }

def process_monthly_metrics(
    contents: bytes,
    filename: str,
    db: Session,
    threshold: float = DEFAULT_THRESHOLD,
    sheet_name: str | None = None,
):

    workbook = pd.ExcelFile(BytesIO(contents))

    if sheet_name:
        target = sheet_name
    else:
        target = next(
            (s for s in workbook.sheet_names if s.lower().strip() in MONTHLY_METRIC_SHEET_NAMES),
            None,
        )
        if target is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "No 'Monthly Attorney Metrics' sheet found in workbook. "
                    f"Available sheets: {workbook.sheet_names}"
                ),
            )

    # raw_df = pd.read_excel(workbook, sheet_name=target)
    raw_df = _read_sheet_with_header_detection(workbook, target)
    raw_df = normalize_columns(raw_df, MONTHLY_METRIC_ALIASES)
    effective_threshold = resolve_threshold(threshold, raw_df)
    raw_df, validation_summary = validate_monthly_metrics_df(raw_df, threshold=effective_threshold)

    upload = AttorneyQuickBookUpload(
        upload_uuid=str(uuid.uuid4()),
        file_name=filename,
        source_type="tracker",
        total_rows=len(raw_df),
        status="PENDING_REVIEW",
        threshold=effective_threshold,
        formula_version=FORMULA_VERSION,
        detected_format="monthly_metric",
    )
    db.add(upload)
    db.commit()
    db.refresh(upload)

    for _, row in raw_df.iterrows():
        attorney = clean_value(row.get("attorney"))
        division = clean_value(row.get("division"))
        if not attorney or not division:
            continue  # skip structurally empty rows

        year_val = _coerce_numeric(row.get("year"))
        month_str = row.get("reporting_month")
        month_num_val = _coerce_numeric(row.get("month_number"))
        reporting_month_date = _build_reporting_month_date(year_val, month_str, month_num_val)

        legal_fees = _coerce_numeric(row.get("legal_fees_collected"))
        large_case_count_val = _coerce_numeric(row.get("large_case_count"))
        large_case_value_val = _coerce_numeric(row.get("large_case_value"))
        is_large = (
            (large_case_value_val or 0) > effective_threshold
            or (large_case_count_val or 0) > 0
        )

        record = AttorneyTrackerRecord(
            upload_id=upload.id,
            case_id=clean_value(row.get("record_id")),       # record_id mapped to case_id
            settlement_date=reporting_month_date,
            reporting_month=reporting_month_date,
            attorney_name=attorney,
            division_name=division,
            settlement_value=legal_fees,
            legal_fees_collected=legal_fees,
            cases_distributed=int(_coerce_numeric(row.get("cases_distributed")) or 0),
            monthly_fee_reduction=_coerce_numeric(row.get("monthly_fee_reduction")),
            active_caseload_snapshot=_coerce_numeric(row.get("active_caseload_snapshot")),
            total_settlement_value=_coerce_numeric(row.get("total_settlement_value")),
            adjusted_value_excluding_large_cases=_coerce_numeric(
                row.get("adjusted_value_excluding_large_cases")
            ),
            large_case_count=(
                int(large_case_count_val) if large_case_count_val is not None else None
            ),
            large_case_value=large_case_value_val,
            is_very_large_case=is_large,
            source_sheet=clean_value(row.get("source_sheet", target)),
            source_file=clean_value(row.get("source_file", filename)),
            year=int(year_val) if year_val is not None else None,
            month_number=int(month_num_val) if month_num_val is not None else None,
            detected_format=clean_value(row.get("detected_format", "monthly_metric")),
            notes=clean_value(row.get("notes")),
        )

        errors = validate_monthly_metric_record(record)
        record.is_valid = not errors
        if errors:
            record.validation_errors = ", ".join(errors)

        db.add(record)

    db.commit()

    invalid_count = (
        db.query(AttorneyTrackerRecord)
        .filter(
            AttorneyTrackerRecord.upload_id == upload.id,
            AttorneyTrackerRecord.is_valid == False,
        )
        .count()
    )

    return {
        "message": "Monthly metrics upload completed. Review validation warnings before approval.",
        "upload_id": upload.id,
        "total_rows": upload.total_rows,
        "invalid_rows": invalid_count,
        "status": upload.status,
        "detected_format": "monthly_metric",
        "formula_version": upload.formula_version,
        "threshold_used": effective_threshold,
        "validation_summary": validation_summary,
    }

# Process upload — Case-Level format (legacy / optional)
def process_raw_settlements(
    contents: bytes,
    filename: str,
    db: Session,
    threshold: float = DEFAULT_THRESHOLD,
):
    raw_df = read_raw_settlements(contents)
    raw_df = normalize_columns(raw_df, COLUMN_ALIASES)
    effective_threshold = resolve_threshold(threshold, raw_df)
    raw_df, validation_summary = validate_raw_settlements(raw_df, threshold=effective_threshold)

    upload = AttorneyQuickBookUpload(
        upload_uuid=str(uuid.uuid4()),
        file_name=filename,
        source_type="tracker",
        total_rows=len(raw_df),
        status="PENDING_REVIEW",
        threshold=effective_threshold,
        formula_version=FORMULA_VERSION,
        detected_format="case_level",
    )
    db.add(upload)
    db.commit()
    db.refresh(upload)

    for _, row in raw_df.iterrows():
        sv = clean_value(row.get("settlement_value"))
        numeric_sv = float(sv) if sv is not None else 0

        record = AttorneyTrackerRecord(
            upload_id=upload.id,
            case_id=clean_value(row.get("case_id")),
            settlement_date=clean_value(row.get("settlement_date")),
            reporting_month=clean_value(row.get("month")),
            attorney_name=clean_value(row.get("attorney")),
            division_name=clean_value(row.get("division")),
            settlement_value=sv,
            legal_fee_value=clean_value(row.get("legal_fee_value")),  # NEW v2
            case_type=clean_value(row.get("case_type")),               # NEW v2
            is_very_large_case=numeric_sv > effective_threshold,
            source_sheet=clean_value(row.get("source_sheet", "Case-Level Settlements")),
            notes=clean_value(row.get("notes")),
            detected_format="case_level",
        )
        errors = validate_record(record)
        record.is_valid = not errors
        if errors:
            record.validation_errors = ", ".join(errors)

        db.add(record)

    db.commit()

    invalid_count = (
        db.query(AttorneyTrackerRecord)
        .filter(
            AttorneyTrackerRecord.upload_id == upload.id,
            AttorneyTrackerRecord.is_valid == False,
        )
        .count()
    )

    return {
        "message": "Case-level upload completed. Review validation warnings before approval.",
        "upload_id": upload.id,
        "total_rows": upload.total_rows,
        "invalid_rows": invalid_count,
        "status": upload.status,
        "detected_format": "case_level",
        "formula_version": upload.formula_version,
        "threshold_used": effective_threshold,
        "validation_summary": validation_summary,
    }



def _sheet_looks_like_quickbook(workbook: pd.ExcelFile, sheet_name: str, max_scan_rows: int = 15) -> bool:
    try:
        scan = pd.read_excel(workbook, sheet_name=sheet_name, header=None, nrows=max_scan_rows)
    except Exception:
        return False
    for _, row in scan.iterrows():
        cell_values = {
            str(v).strip().lower()
            for v in row
            if pd.notna(v) and str(v).strip()
        }
        if len(cell_values & QUICKBOOK_SIGNATURE_COLUMNS) >= QUICKBOOK_MIN_SIGNATURE_MATCHES:
            return True
    return False
 
 
def _read_quickbook_sheet_with_header_detection(
    workbook: pd.ExcelFile,
    sheet_name: str,
    max_scan_rows: int = 15,
) -> pd.DataFrame:
    df_scan = pd.read_excel(workbook, sheet_name=sheet_name, header=None, nrows=max_scan_rows)
    detected_header_row = 0
    for row_idx, row in df_scan.iterrows():
        cell_values = {
            str(v).strip().lower()
            for v in row
            if pd.notna(v) and str(v).strip()
        }
        if len(cell_values & QUICKBOOK_SIGNATURE_COLUMNS) >= QUICKBOOK_MIN_SIGNATURE_MATCHES:
            detected_header_row = row_idx
            break
    df = pd.read_excel(workbook, sheet_name=sheet_name, header=detected_header_row)
    df.columns = [str(c).strip() for c in df.columns]
    return df
 
 
def merge_quickbook_case_type(raw_case_type) -> str | None:
    if raw_case_type is None or (isinstance(raw_case_type, float) and pd.isna(raw_case_type)):
        return None
    key = str(raw_case_type).strip().lower()
    return QUICKBOOK_CASE_TYPE_MERGE_MAP.get(key, str(raw_case_type).strip())
 
 
def _extract_attorney_from_account_header(row: pd.Series) -> str | None:
    for val in row:
        if not isinstance(val, str):
            continue
        if "legal fee income" in val.lower() and "-" in val:
            after_dash = val.rsplit("-", 1)[-1].strip()
            if after_dash:
                return after_dash
    return None
 
 
def _parse_quickbook_date(value):
    parsed = pd.to_datetime(value, errors="coerce", dayfirst=False)
    return parsed.date() if pd.notna(parsed) else None
 
 
def parse_quickbook_export(contents: bytes, sheet_name: str | None = None) -> pd.DataFrame:
    workbook = pd.ExcelFile(BytesIO(contents))
    target_sheet = sheet_name or next(
        (s for s in workbook.sheet_names if _sheet_looks_like_quickbook(workbook, s)),
        workbook.sheet_names[0],
    )
    df_raw = _read_quickbook_sheet_with_header_detection(workbook, target_sheet)
 
    current_attorney = None
    parsed_rows = []
 
    for _, row in df_raw.iterrows():
        row_type = row.get("Type")
        is_blank_type = pd.isna(row_type) or str(row_type).strip() == ""
 
        if is_blank_type:
            # Account header / subtotal row — never a transaction.
            attorney = _extract_attorney_from_account_header(row)
            if attorney:
                current_attorney = attorney
            continue
 
        row_type_str = str(row_type).strip()
        if row_type_str != "Deposit":
            continue
 
        credit = _coerce_numeric(row.get("Credit"))
        if credit is None or credit <= 0:
            continue
 
        raw_office = clean_value(row.get("Class"))
        office_display = str(raw_office).split(":")[-1].strip() if raw_office else None
 
        raw_case_type = clean_value(row.get("Case Type Description"))
        merged_case_type = merge_quickbook_case_type(raw_case_type)
 
        transaction_date = _parse_quickbook_date(row.get("Date"))
        debit_val = _coerce_numeric(row.get("Debit"))
        balance_val = _coerce_numeric(row.get("Balance"))
 
        parsed_rows.append({
            # Raw QuickBooks columns — stored verbatim.
            "type": row_type_str,
            "date": transaction_date,
            "num": clean_value(row.get("Num")),
            "name": clean_value(row.get("Name")),
            "memo": clean_value(row.get("Memo")),
            "class_raw": raw_office,
            "clr": clean_value(row.get("Clr")),
            "split": clean_value(row.get("Split")),
            "debit": debit_val,
            "credit": float(credit),
            "balance": balance_val,
            "case_number": clean_value(row.get("Case Number")),
            "case_type_raw": raw_case_type,
            # Derived columns.
            "attorney": current_attorney,
            "office": office_display,
            "case_type": merged_case_type,
            "reporting_month": transaction_date.strftime("%Y-%m") if transaction_date else None,
        })
 
    return pd.DataFrame(parsed_rows)
 
 
def validate_quickbook_df(df: pd.DataFrame) -> dict:
    validation_summary = {
        "total_rows": len(df),
        "duplicate_rows_removed": 0,
        "null_rows_removed": 0,
        "warnings": [],
        "format": "quickbook",
    }
    if df.empty:
        validation_summary["warnings"].append(
            _warning(
                "No Deposit rows with Credit > 0 were found. Check that the "
                "export contains transaction detail, not just summary totals.",
                "High",
            )
        )
        return validation_summary
 
    missing_attorney = df["attorney"].isna().sum()
    if missing_attorney:
        validation_summary["warnings"].append(
            _warning(
                f"{missing_attorney} row(s) had no attorney sub-account header above them "
                "and could not be attributed to an attorney.",
                "High",
            )
        )
    missing_office = df["office"].isna().sum()
    if missing_office:
        validation_summary["warnings"].append(
            _warning(f"{missing_office} row(s) are missing an office (Class).", "Medium")
        )
    missing_case_type = df["case_type"].isna().sum()
    if missing_case_type:
        validation_summary["warnings"].append(
            _warning(f"{missing_case_type} row(s) are missing a Case Type Description.", "Low")
        )
    missing_date = df["date"].isna().sum()
    if missing_date:
        validation_summary["warnings"].append(
            _warning(f"{missing_date} row(s) have an unparseable Date.", "High")
        )
    dup_case_numbers = df[df["case_number"].notna() & df.duplicated(subset=["case_number"], keep=False)]
    if not dup_case_numbers.empty:
        validation_summary["warnings"].append(
            _warning(f"{len(dup_case_numbers)} row(s) share a duplicate Case Number.", "Low")
        )
    return validation_summary
 
def clean_value(value):
    if pd.isna(value):
        return None
    return value

def process_quickbook_upload(
    contents: bytes,
    filename: str,
    db: Session,
    sheet_name: str | None = None,
):
    workbook = pd.ExcelFile(BytesIO(contents))
    target_sheet = sheet_name or next(
        (s for s in workbook.sheet_names if _sheet_looks_like_quickbook(workbook, s)),
        workbook.sheet_names[0],
    )
 
    parsed_df = parse_quickbook_export(contents, sheet_name=target_sheet)
    validation_summary = validate_quickbook_df(parsed_df)
 
    upload = AttorneyQuickBookUpload(
        upload_uuid=str(uuid.uuid4()),
        file_name=filename,
        source_type="quickbook",
        sheet_name=target_sheet,
        total_rows=len(parsed_df),
        status="PENDING_REVIEW",
        formula_version=FORMULA_VERSION,
        detected_format="quickbook",
    )
    db.add(upload)
    db.commit()
    db.refresh(upload)
 
    for _, row in parsed_df.iterrows():
        transaction = QuickbookTransaction(
            upload_id=upload.id,
            type=clean_value(row.get("type")),
            date=clean_value(row.get("date")),
            num=str(row.get("num")) if clean_value(row.get("num")) is not None else None,
            name=clean_value(row.get("name")),
            memo=clean_value(row.get("memo")),
            class_=clean_value(row.get("class_raw")),
            clr=clean_value(row.get("clr")),
            split=clean_value(row.get("split")),
            debit=clean_value(row.get("debit")),
            credit=clean_value(row.get("credit")),
            balance=clean_value(row.get("balance")),
            case_number=str(row.get("case_number")) if clean_value(row.get("case_number")) is not None else None,
            case_type_description=clean_value(row.get("case_type_raw")),
            # Derived columns (see parse_quickbook_export) — required by
            # generate_quickbook_analysis / update_quickbook_transaction.
            attorney=clean_value(row.get("attorney")),
            office=clean_value(row.get("office")),
            case_type_merged=clean_value(row.get("case_type")))
        # errors = validate_quickbook_record(transaction)
        # transaction.is_valid = not errors
        # if errors:
        #     transaction.validation_errors = ", ".join(errors)
 
        db.add(transaction)
 
    db.commit()
 
    invalid_count = (
        db.query(QuickbookTransaction)
        .filter(
            QuickbookTransaction.upload_id == upload.id,
            QuickbookTransaction.is_valid == False,
        )
        .count()
    )
 
    return {
        "message": "QuickBooks export uploaded. Review validation warnings before approval.",
        "upload_id": upload.id,
        "sheet_name": target_sheet,
        "total_rows": upload.total_rows,
        "invalid_rows": invalid_count,
        "status": upload.status,
        "detected_format": "quickbook",
        "formula_version": upload.formula_version,
        "validation_summary": validation_summary,
    } 
 
# QuickBooks — CRUD helpers (approve / lock / edit / list)
 
def approve_quickbook_upload(upload_id: int, db: Session):
    upload = (
        db.query(AttorneyQuickBookUpload)
        .filter(AttorneyQuickBookUpload.id == upload_id, AttorneyQuickBookUpload.source_type == "quickbook")
        .first()
    )
    if not upload:
        raise HTTPException(status_code=404, detail="QuickBooks upload not found")
    invalid_rows = (
        db.query(QuickbookTransaction)
        .filter(
            QuickbookTransaction.upload_id == upload_id,
            QuickbookTransaction.is_valid == False,
        )
        .count()
    )
    if invalid_rows:
        raise HTTPException(
            status_code=400,
            detail=f"{invalid_rows} invalid rows still exist. Please correct them before approval.",
        )
    upload.status = "APPROVED"
    upload.approved_at = datetime.utcnow()
    db.commit()
    return {"message": "QuickBooks upload approved successfully", "upload_id": upload_id, "status": upload.status}
 
 
def lock_quickbook_upload(upload_id: int, db: Session):
    upload = (
        db.query(AttorneyQuickBookUpload)
        .filter(AttorneyQuickBookUpload.id == upload_id, AttorneyQuickBookUpload.source_type == "quickbook")
        .first()
    )
    if not upload:
        raise HTTPException(status_code=404, detail="QuickBooks upload not found")
    if upload.status != "APPROVED":
        raise HTTPException(status_code=400, detail="Upload must be approved before it can be locked.")
    db.query(QuickbookTransaction).filter(
        QuickbookTransaction.upload_id == upload_id
    ).update({"is_locked": True})
    upload.status = "LOCKED"
    db.commit()
    return {"message": "QuickBooks upload locked successfully. Transactions are now read-only.", "upload_id": upload_id, "status": upload.status}
 
 
def get_invalid_quickbook_transactions(upload_id: int, db: Session):
    return (
        db.query(QuickbookTransaction)
        .filter(
            QuickbookTransaction.upload_id == upload_id,
            QuickbookTransaction.is_valid == False,
        )
        .all()
    )


def update_quickbook_transaction(db: Session, transaction_id: int, payload):
    transaction = db.query(QuickbookTransaction).filter(QuickbookTransaction.id == transaction_id).first()
    if not transaction:
        raise HTTPException(status_code=404, detail="QuickBooks transaction not found")
    if transaction.is_locked:
        raise HTTPException(status_code=403, detail="Transaction is locked and cannot be edited.")
 
    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(transaction, field, value)
 
    if "class_" in update_data and transaction.class_:
        transaction.office = str(transaction.class_).split(":")[-1].strip()
    if "case_type_description" in update_data:
        transaction.case_type_merged = merge_quickbook_case_type(transaction.case_type_description)
    if "date" in update_data and transaction.date:
        transaction.reporting_month = transaction.date.strftime("%Y-%m")
 
    db.commit()
    db.refresh(transaction)
    return {
        "message": "QuickBooks transaction updated successfully",
        "transaction_id": transaction.id,
        "is_valid": transaction.is_valid,
        "validation_errors": transaction.validation_errors,
    }
 
 
def delete_quickbook_upload(upload_id: int, db: Session):
    upload = (
        db.query(AttorneyQuickBookUpload)
        .filter(AttorneyQuickBookUpload.id == upload_id, AttorneyQuickBookUpload.source_type == "quickbook")
        .first()
    )
    if not upload:
        raise HTTPException(status_code=404, detail="QuickBooks upload not found")
    db.query(QuickbookTransaction).filter(QuickbookTransaction.upload_id == upload_id).delete()
    db.delete(upload)
    db.commit()
    return {"success": True, "message": f"QuickBooks upload {upload_id} and all associated transactions deleted successfully"}
 
 
def list_quickbook_uploads(db: Session, upload_id: int | None = None):
    if upload_id is not None:
        upload = (
            db.query(AttorneyQuickBookUpload)
            .filter(AttorneyQuickBookUpload.id == upload_id, AttorneyQuickBookUpload.source_type == "quickbook")
            .first()
        )
        if not upload:
            raise HTTPException(status_code=404, detail="QuickBooks upload not found")
        transactions = (
            db.query(QuickbookTransaction)
            .filter(QuickbookTransaction.upload_id == upload_id)
            .all()
        )
        return {"upload": upload, "transaction_count": len(transactions), "transactions": transactions}
 
    uploads = (
        db.query(AttorneyQuickBookUpload)
        .filter(AttorneyQuickBookUpload.source_type == "quickbook")
        .order_by(AttorneyQuickBookUpload.created_at.desc())
        .all()
    )
    return {"count": len(uploads), "uploads": uploads}
 

# QuickBooks (Formula Set 6)
 
def _format_currency(value) -> str:
    if value is None:
        return "$0"
    v = float(value)
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000:
        return f"{sign}${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{sign}${v:,.0f}"
    return f"{sign}${v:.0f}"
 
 
def _format_pct(value) -> str:
    if value is None:
        return None
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.1f}%"
 
 
# QuickBooks — revenue metric calculations (Formula Sets 1-5)
 
def calculate_quickbook_revenue_metrics(df: pd.DataFrame) -> dict:

    if df.empty:
        return {
            "kpi_summary": {},
            "monthly_trend": [],
            "case_type_breakdown": [],
            "office_breakdown": [],
            "attorney_leaderboard": [],
        }
 
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df["year"] = df["date"].dt.year
    df["month"] = df["date"].dt.month
    df["year_month"] = df["date"].dt.strftime("%Y-%m")
 
    total_revenue = float(df["credit"].sum())
    current_year = int(df["year"].max())
    prior_year = current_year - 1
 
    # ---- Formula Set 1 — KPI Summary Cards ----
    cur_year_df = df[df["year"] == current_year]
    prior_year_df = df[df["year"] == prior_year]
 
    total_ytd = float(cur_year_df["credit"].sum())
    total_prior_year = float(prior_year_df["credit"].sum())
 
    # 1C — YoY growth compared over the SAME months only.
    months_in_current_year = sorted(cur_year_df["month"].unique().tolist())
    cur_same_months = cur_year_df["credit"].sum()
    prior_same_months = prior_year_df[prior_year_df["month"].isin(months_in_current_year)]["credit"].sum()
    yoy_growth_pct = (
        ((cur_same_months - prior_same_months) / prior_same_months) * 100
        if prior_same_months else None
    )
 
    # 1D — Current month revenue (latest month with data in the current year).
    current_month = max(months_in_current_year) if months_in_current_year else None
    current_month_revenue = float(
        cur_year_df[cur_year_df["month"] == current_month]["credit"].sum()
    ) if current_month else 0.0
 
    # 1E — Average monthly revenue YTD.
    months_elapsed = len(months_in_current_year)
    avg_monthly_ytd = total_ytd / months_elapsed if months_elapsed else 0.0
 
    # 1F / 1G — Cases settled + average revenue per case.
    cases_ytd = int(len(cur_year_df))
    avg_per_case = total_ytd / cases_ytd if cases_ytd else 0.0
 
    kpi_summary = {
        "total_revenue_ytd": total_ytd,
        "total_revenue_ytd_display": _format_currency(total_ytd),
        "total_revenue_prior_year": total_prior_year,
        "total_revenue_prior_year_display": _format_currency(total_prior_year),
        "yoy_growth_pct": round(yoy_growth_pct, 1) if yoy_growth_pct is not None else None,
        "yoy_growth_display": _format_pct(yoy_growth_pct),
        "current_month_revenue": current_month_revenue,
        "current_month_revenue_display": _format_currency(current_month_revenue),
        "avg_monthly_revenue_ytd": round(avg_monthly_ytd, 2),
        "avg_monthly_revenue_ytd_display": _format_currency(avg_monthly_ytd),
        "cases_settled_ytd": cases_ytd,
        "avg_revenue_per_case": round(avg_per_case, 2),
        "avg_revenue_per_case_display": _format_currency(avg_per_case),
        "current_year": current_year,
        "prior_year": prior_year,
    }
 
    # ---- Formula Set 2 — Monthly Revenue Trend ----
    monthly = (
        df.groupby("year_month")["credit"]
        .sum()
        .reset_index()
        .sort_values("year_month")
        .reset_index(drop=True)
    )
    monthly_trend = []
    values = monthly["credit"].tolist()
    for i, row in monthly.iterrows():
        prev_value = values[i - 1] if i > 0 else None
        mom_growth_pct = (
            ((row["credit"] - prev_value) / prev_value) * 100
            if prev_value else None
        )
        window = values[max(0, i - 2): i + 1]
        rolling_3mo = sum(window) / len(window) if window else None
        monthly_trend.append({
            "year_month": row["year_month"],
            "revenue": float(row["credit"]),
            "revenue_display": _format_currency(row["credit"]),
            "mom_growth_pct": round(mom_growth_pct, 1) if mom_growth_pct is not None else None,
            "mom_growth_display": _format_pct(mom_growth_pct),
            "rolling_3mo_avg": round(rolling_3mo, 2) if rolling_3mo is not None else None,
        })
 
    # ---- Formula Set 3 — Revenue by Case Type ----
    case_type_group = (
        df.dropna(subset=["case_type"])
        .groupby("case_type")["credit"]
        .sum()
        .sort_values(ascending=False)
    )
    case_type_breakdown = [
        {
            "case_type": case_type,
            "revenue": float(revenue),
            "revenue_display": _format_currency(revenue),
            "pct_of_total": round((revenue / total_revenue) * 100, 1) if total_revenue else 0.0,
        }
        for case_type, revenue in case_type_group.items()
    ]
 
    # ---- Formula Set 4 — Revenue by Office ----
    office_group = (
        df.dropna(subset=["office"])
        .groupby("office")["credit"]
        .sum()
        .sort_values(ascending=False)
    )
    office_breakdown = [
        {
            "office": office,
            "revenue": float(revenue),
            "revenue_display": _format_currency(revenue),
            "pct_of_total": round((revenue / total_revenue) * 100, 1) if total_revenue else 0.0,
        }
        for office, revenue in office_group.items()
    ]
 
    # ---- Formula Set 5 — Attorney Leaderboard ----
    attorney_leaderboard = []
    atty_df = df.dropna(subset=["attorney"])
    atty_totals = atty_df.groupby("attorney")["credit"].agg(["sum", "count"]).sort_values("sum", ascending=False)
 
    for attorney, row in atty_totals.iterrows():
        total = float(row["sum"])
        cases = int(row["count"])
        avg_case = total / cases if cases else 0.0
 
        atty_cur = atty_df[(atty_df["attorney"] == attorney) & (atty_df["year"] == current_year)]
        atty_cur_same_months = atty_cur["credit"].sum()
        atty_prior_same_months = atty_df[
            (atty_df["attorney"] == attorney)
            & (atty_df["year"] == prior_year)
            & (atty_df["month"].isin(months_in_current_year))
        ]["credit"].sum()
        atty_growth_pct = (
            ((atty_cur_same_months - atty_prior_same_months) / atty_prior_same_months) * 100
            if atty_prior_same_months else None
        )
 
        attorney_leaderboard.append({
            "attorney": attorney,
            "total_revenue": total,
            "total_revenue_display": _format_currency(total),
            "cases": cases,
            "avg_revenue_per_case": round(avg_case, 2),
            "avg_revenue_per_case_display": _format_currency(avg_case),
            "revenue_ytd": float(atty_cur_same_months),
            "revenue_ytd_display": _format_currency(atty_cur_same_months),
            "pct_of_total_revenue": round((total / total_revenue) * 100, 1) if total_revenue else 0.0,
            "yoy_growth_pct": round(atty_growth_pct, 1) if atty_growth_pct is not None else None,
            "yoy_growth_display": _format_pct(atty_growth_pct),
        })
 
    return {
        "kpi_summary": kpi_summary,
        "monthly_trend": monthly_trend,
        "case_type_breakdown": case_type_breakdown,
        "office_breakdown": office_breakdown,
        "attorney_leaderboard": attorney_leaderboard,
    }
 
 
def generate_quickbook_analysis(
    upload_id: int,
    db: Session,
    status_filter: str = "approved",
    source: str | None = None,
) -> dict:
    upload = (
        db.query(AttorneyQuickBookUpload)
        .filter(AttorneyQuickBookUpload.id == upload_id, AttorneyQuickBookUpload.source_type == "quickbook")
        .first()
    )
    if not upload:
        raise HTTPException(status_code=404, detail="QuickBooks upload not found")
 
    if status_filter == "approved" and upload.status not in ("APPROVED", "LOCKED"):
        raise HTTPException(
            status_code=400,
            detail=f"Upload status is '{upload.status}'. Upload must be approved before analysis can run.",
        )
    elif status_filter in ("pending", "locked") and upload.status.lower() != status_filter:
        raise HTTPException(
            status_code=400,
            detail=f"Upload status is '{upload.status}', not '{status_filter}'.",
        )
 
    transactions = (
        db.query(QuickbookTransaction)
        .filter(QuickbookTransaction.upload_id == upload_id)
        .all()
    )
    raw_df = pd.DataFrame([
        {
            "date": t.date,
            "credit": float(t.credit) if t.credit else 0.0,
            "attorney": t.attorney,
            "office": t.office,
            "case_type": t.case_type_merged,
        }
        for t in transactions
    ])
    revenue_metrics = calculate_quickbook_revenue_metrics(raw_df)
    return clean_nan({
        "source":source,
        "revenue_metrics": revenue_metrics,
        "upload_id": upload.id,
        "total_rows": upload.total_rows,
        "formula_version": upload.formula_version or FORMULA_VERSION,
        "detected_format": "quickbook",
        "status_filter_applied": status_filter,
    })

# Auto-detect upload format

def process_upload_auto(
    contents: bytes,
    filename: str,
    db: Session,
    threshold: float = DEFAULT_THRESHOLD,
):
    workbook = pd.ExcelFile(BytesIO(contents))
    sheet_lower = {s.lower().strip(): s for s in workbook.sheet_names}

    for sheet in workbook.sheet_names:
        if _sheet_looks_like_quickbook(workbook, sheet):
            return process_quickbook_upload(
                contents=contents, filename=filename, db=db,
                threshold=threshold, sheet_name=sheet,
            )

    for name in MONTHLY_METRIC_SHEET_NAMES:
        if name in sheet_lower:
            return process_monthly_metrics(
                contents=contents, filename=filename, db=db,
                threshold=threshold, sheet_name=sheet_lower[name],
            )

    for name in CASE_LEVEL_SHEET_NAMES:
        if name in sheet_lower:
            return process_raw_settlements(
                contents=contents, filename=filename, db=db, threshold=threshold,
            )

    return process_monthly_metrics(
        contents=contents, filename=filename, db=db,
        threshold=threshold, sheet_name=workbook.sheet_names[0],
    )


# Generate analysis (entry point) — format-aware

def generate_analysis(
    upload_id: int,
    db: Session,
    threshold: float = DEFAULT_THRESHOLD,
    status_filter: str = "approved",
    source: str | None = None,
):
    kind, _ = _resolve_upload_any(upload_id, db, source=source)
    if kind == "quickbook":
        return generate_quickbook_analysis(upload_id=upload_id, db=db, status_filter=status_filter, source = source)

    validate_upload_ready(db=db, upload_id=upload_id)

    upload = (
        db.query(AttorneyQuickBookUpload)
        .filter(AttorneyQuickBookUpload.id == upload_id, AttorneyQuickBookUpload.source_type == "tracker")
        .first()
    )
    if not upload:
        raise HTTPException(status_code=404, detail="Upload not found")

    if upload.status not in ("APPROVED", "LOCKED"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Upload status is '{upload.status}'. "
                "Upload must be approved before analysis can run."
            ),
        )

    effective_threshold = resolve_threshold(
        threshold if threshold is not None else upload.threshold,
        pd.DataFrame(),
    )

    records = (
        db.query(AttorneyTrackerRecord)
        .filter(AttorneyTrackerRecord.upload_id == upload_id)
        .all()
    )

    detected_format = getattr(upload, "detected_format", "monthly_metric") or "monthly_metric"

    if detected_format == "quickbook":
        raw_df = pd.DataFrame([
            {
                "date": r.settlement_date,
                "credit": float(r.settlement_value) if r.settlement_value else 0.0,
                "attorney": r.attorney_name,
                "office": r.division_name,
                "case_type": r.case_type,
            }
            for r in records
        ])
        revenue_metrics = calculate_quickbook_revenue_metrics(raw_df)
        result = {
            "revenue_metrics": revenue_metrics,
            "threshold_used": effective_threshold,
            "formula_version": upload.formula_version or FORMULA_VERSION,
            "detected_format": detected_format,
            "status_filter_applied": status_filter,
        }
        return clean_nan(result)

    if detected_format == "case_level":
        raw_df = pd.DataFrame([
            {
                "case_id": r.case_id,
                "month": r.reporting_month,
                "attorney": r.attorney_name,
                "division": r.division_name,
                "settlement_value": float(r.settlement_value) if r.settlement_value else 0,
            }
            for r in records
        ])
    else:
        # Default: monthly_metric format
        raw_df = pd.DataFrame([
            {
                "reporting_month": r.reporting_month,
                "attorney": r.attorney_name,
                "division": r.division_name,
                "legal_fees_collected": float(r.legal_fees_collected or r.settlement_value or 0),
                "cases_distributed": r.cases_distributed or 0,
                "settlement_count": r.cases_distributed or 0,
                "monthly_fee_reduction": float(r.monthly_fee_reduction or 0),
                "active_caseload_snapshot": r.active_caseload_snapshot,
                "total_settlement_value": float(
                    r.total_settlement_value or r.legal_fees_collected or r.settlement_value or 0
                ),
                "adjusted_value_excluding_large_cases": (
                    float(r.adjusted_value_excluding_large_cases)
                    if r.adjusted_value_excluding_large_cases is not None else None
                ),
                "large_case_count": r.large_case_count or 0,
                "large_case_value": float(r.large_case_value or 0),
            }
            for r in records
        ])
        # Derive missing adjusted values and alias month column
        if "adjusted_value_excluding_large_cases" in raw_df.columns:
            mask = raw_df["adjusted_value_excluding_large_cases"].isna()
            raw_df.loc[mask, "adjusted_value_excluding_large_cases"] = (
                raw_df.loc[mask, "total_settlement_value"]
                - raw_df.loc[mask, "large_case_value"]
            )
        raw_df["month"] = raw_df["reporting_month"]

    if threshold == "top_5pct":
        effective_threshold = resolve_threshold("top_5pct", raw_df)

    attorney_monthly_metrics = calculate_attorney_monthly_metrics(raw_df, threshold=effective_threshold)
    dashboard = calculate_dashboard_metrics(raw_df, threshold=effective_threshold)
    leaderboard = calculate_leaderboard(raw_df, threshold=effective_threshold)
    attorney_value_scores = calculate_attorney_value_scores(raw_df, attorney_monthly_metrics, threshold=effective_threshold)
    insights = generate_insights(dashboard, leaderboard, attorney_monthly_metrics)
    division_metric_rules = get_division_metric_rules()

    result = {
        "source": source,
        "dashboard": dashboard,
        "leaderboard": leaderboard,
        "attorney_monthly_metrics": attorney_monthly_metrics,
        "attorney_value_scores": attorney_value_scores,
        "insights": insights,
        "division_metric_rules": division_metric_rules,
        "threshold_used": effective_threshold,
        "formula_version": upload.formula_version or FORMULA_VERSION,
        "detected_format": detected_format,
        "status_filter_applied": status_filter,
    }
    return clean_nan(result)

def clean_nan(obj):
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_nan(v) for v in obj]
    elif isinstance(obj, float):
        if math.isnan(obj):
            return None
        return obj
    return obj