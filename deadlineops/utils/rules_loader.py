"""
rules_loader.py — Load and validate jurisdiction rule files from YAML.

Usage
-----
    from deadlineops.utils.rules_loader import load_rules, get_rules_for_trigger

    rules = load_rules("CO_STATE")          # loads rules/co_state.yaml
    triggered = get_rules_for_trigger(rules, "service_of_complaint")
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
import yaml

# Directory containing the YAML rule files
RULES_DIR = Path("deadlineops/rules")

JURISDICTION_FILES = {
    "CO_STATE":   "co_state.yaml",
    "CO_FEDERAL": "co_federal.yaml",
    "AZ_STATE":   "az_state.yaml",
    "AZ_FEDERAL": "az_federal.yaml",
}


@dataclass
class DeadlineRule:
    id: str
    trigger: str            # e.g. "service_of_complaint"
    label: str              # human-readable deadline name
    offset_days: int        # positive = after trigger; negative = before anchor
    day_type: str           # "calendar" | "business"
    rollover: str           # "next_business_day" | "previous_business_day" | "none"
    reminders_days: List[int]
    color: str              # "red" | "orange" | "yellow" | "blue" | "green"
    notes: str = ""
    jurisdiction: str = ""
    holiday_set: str = "federal"
    offset_years: Optional[int] = None
    cmo_override: str = ""  # "No – Statutory" | "Yes – CMO sets date" | "Court extension only" | etc.


@dataclass
class JurisdictionRules:
    jurisdiction: str
    name: str
    holiday_set: str
    rules: List[DeadlineRule] = field(default_factory=list)


def load_rules(jurisdiction: str) -> JurisdictionRules:
    """
    Load rules for a jurisdiction from its YAML file.

    Parameters
    ----------
    jurisdiction : One of "CO_STATE", "CO_FEDERAL", "AZ_STATE", "AZ_FEDERAL"

    Returns
    -------
    JurisdictionRules object with all encoded rules.
    """
    jurisdiction = jurisdiction.upper()
    if jurisdiction not in JURISDICTION_FILES:
        raise ValueError(
            f"Unknown jurisdiction: {jurisdiction!r}. "
            f"Valid options: {list(JURISDICTION_FILES.keys())}"
        )

    yaml_path = RULES_DIR / JURISDICTION_FILES[jurisdiction]
    if not yaml_path.exists():
        raise FileNotFoundError(f"Rules file not found: {yaml_path}")

    with open(yaml_path, "r") as f:
        data = yaml.safe_load(f)

    holiday_set = data.get("holiday_set", "federal")

    rules = []
    for r in data.get("rules", []):
        rule = DeadlineRule(
            id=r["id"],
            trigger=r["trigger"],
            label=r["label"],
            # offset_days=int(r["offset_days"]),
            offset_days=int(r.get("offset_days", 0)),
            day_type=r.get("day_type", data.get("day_type_default", "calendar")),
            rollover=r.get("rollover", "next_business_day"),
            reminders_days=r.get("reminders_days", [7, 1]),
            color=r.get("color", "blue"),
            notes=r.get("notes", ""),
            jurisdiction=jurisdiction,
            holiday_set=holiday_set,
            offset_years=int(r["offset_years"]) if "offset_years" in r else None,
            cmo_override=r.get("cmo_override", ""),
        )
        rules.append(rule)

    return JurisdictionRules(
        jurisdiction=jurisdiction,
        name=data["name"],
        holiday_set=holiday_set,
        rules=rules,
    )


def load_all_rules() -> dict[str, JurisdictionRules]:
    """Load all four jurisdiction rule sets at once."""
    return {j: load_rules(j) for j in JURISDICTION_FILES}


def get_rules_for_trigger(
    jurisdiction_rules: JurisdictionRules,
    trigger: str,
) -> List[DeadlineRule]:
    """
    Return all rules for a specific trigger event.

    Parameters
    ----------
    jurisdiction_rules : Loaded rules for a jurisdiction.
    trigger            : e.g. "service_of_complaint", "trial_date", "msj_filed_date"
    """
    return [r for r in jurisdiction_rules.rules if r.trigger == trigger]


def list_triggers(jurisdiction_rules: JurisdictionRules) -> List[str]:
    """Return all unique trigger names in a jurisdiction's rules."""
    return sorted(set(r.trigger for r in jurisdiction_rules.rules))