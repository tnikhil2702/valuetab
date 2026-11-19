"""
date_math.py — Core date-arithmetic engine for DeadlineOps.
"""

from __future__ import annotations

import warnings
from datetime import date, timedelta
from typing import Literal, Optional

from deadlineops.utils.holidays import get_holidays
from dateutil.relativedelta import relativedelta

DayType = Literal["calendar", "business"]
Rollover = Literal["next_business_day", "previous_business_day", "none"]


# ─── business-day helpers ────────────────────────────────────────────────────

def is_business_day(d: date, holiday_set: str) -> bool:
    """True if d is Mon–Fri and not a holiday."""
    if d.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    holidays = get_holidays(holiday_set, d.year)
    # Also fetch next year's holidays in case we're near year-end
    if d.month == 12:
        holidays |= get_holidays(holiday_set, d.year + 1)
    return d not in holidays


def next_business_day(d: date, holiday_set: str) -> date:
    """Return d if it is already a business day, otherwise advance forward."""
    candidate = d
    while not is_business_day(candidate, holiday_set):
        candidate += timedelta(days=1)
    return candidate


def previous_business_day(d: date, holiday_set: str) -> date:
    """Return d if it is already a business day, otherwise step backward."""
    candidate = d
    while not is_business_day(candidate, holiday_set):
        candidate -= timedelta(days=1)
    return candidate


# ─── core add_days ───────────────────────────────────────────────────────────

def add_days(
    start: date,
    offset_days: int,
    day_type: DayType,
    rollover: Rollover,
    holiday_set: str,
) -> date:
    """
    Compute a deadline date from a start date.

    Parameters
    ----------
    start        : The triggering event date.
    offset_days  : Calendar or business days to add (negative = before start).
    day_type     : "calendar" counts all days; "business" skips weekends/holidays.
    rollover     : What to do if the result lands on a non-business day.
                   "next_business_day"     — push forward (typical for deadlines *due*)
                   "previous_business_day" — pull back  (typical for deadlines *before* trial)
                   "none"                  — leave as-is
    holiday_set  : One of "federal", "federal+colorado", "federal+arizona".

    Returns
    -------
    date : The computed deadline.
    """
    if day_type == "calendar":
        result = start + timedelta(days=offset_days)
    elif day_type == "business":
        result = _add_business_days(start, offset_days, holiday_set)
    else:
        raise ValueError(f"Unknown day_type: {day_type!r}")

    if rollover == "next_business_day":
        result = next_business_day(result, holiday_set)
    elif rollover == "previous_business_day":
        result = previous_business_day(result, holiday_set)
    elif rollover != "none":
        raise ValueError(f"Unknown rollover: {rollover!r}")

    return result


def _add_business_days(start: date, n: int, holiday_set: str) -> date:
    """
    Add (or subtract if n < 0) exactly n business days to start.
    The start date itself is NOT counted.
    """
    if n == 0:
        return start

    step = 1 if n > 0 else -1
    remaining = abs(n)
    current = start

    while remaining > 0:
        current += timedelta(days=step)
        if is_business_day(current, holiday_set):
            remaining -= 1

    return current


# ─── reminder dates ──────────────────────────────────────────────────────────

def compute_reminder_dates(deadline: date, reminders_days: list[int], holiday_set: str) -> list[date]:
    """
    Return a list of business-day-adjusted reminder dates before `deadline`.

    reminders_days : list of integers, e.g. [30, 14, 7, 3, 1]
                     Each is the number of calendar days BEFORE the deadline.

    Reminders that fall on non-business days are pushed to the previous business day.
    Duplicate dates (after adjustment) are removed and the list is returned sorted ascending.
    """
    reminder_dates = set()
    for days_before in reminders_days:
        raw = deadline - timedelta(days=days_before)
        adjusted = previous_business_day(raw, holiday_set)
        if adjusted < date.today():
            continue   # Skip reminders already in the past
        reminder_dates.add(adjusted)

    return sorted(reminder_dates)


def add_years(
    start: date,
    offset_years: int,
    rollover: Rollover,
    holiday_set: str,
) -> date:
    """
    Add calendar years to a date using relativedelta (leap-year safe).
    Jan 1 2020 + 6 years = Jan 1 2026, regardless of leap years.
    """
    result = start + relativedelta(years=offset_years)

    if rollover == "next_business_day":
        result = next_business_day(result, holiday_set)
    elif rollover == "previous_business_day":
        result = previous_business_day(result, holiday_set)
    elif rollover != "none":
        raise ValueError(f"Unknown rollover: {rollover!r}")

    return result


# ─── MIN formula for discovery cutoff ────────────────────────────────────────

TIER_DAYS = {1: 120, 2: 180, 3: 240}


def compute_discovery_cutoff_min(
    trial_date: date,
    early_meeting_date: date,
    tier: int,
    trial_offset_days: int = 60,
    holiday_set: str = "federal+arizona",
    rollover: Rollover = "previous_business_day",
) -> date:
    """
    Compute discovery cutoff using the confirmed AZ MIN formula.

    Formula:
        MIN(trial_date − trial_offset_days,  early_meeting_date + tier_days)

    Parameters
    ----------
    trial_date          : Date of trial.
    early_meeting_date  : Date of the ARCP 16(b) Early Meeting.
    tier                : 1, 2, or 3 (ARCP 26.2 tier).
    trial_offset_days   : Days before trial for the first bound (default 60).
                          Implement as CMO-overridable constant — NOT hardcoded.
    holiday_set         : Holiday set for weekend/holiday rollover.
    rollover            : Rollover direction (default previous_business_day).

    Returns
    -------
    date : The earlier of the two bounds, adjusted for weekends/holidays.

    Notes
    -----
    The 60-day figure is a Maricopa scheduling order default, NOT a fixed
    ARCP rule. Pass cmo_cutoff_date directly to apply_cmo_override() when
    a scheduling order provides a specific date.
    """
    if tier not in TIER_DAYS:
        raise ValueError(f"Unknown tier: {tier!r}. Must be 1, 2, or 3.")

    tier_days = TIER_DAYS[tier]

    bound_trial = trial_date - timedelta(days=trial_offset_days)
    bound_tier  = early_meeting_date + timedelta(days=tier_days)

    raw = min(bound_trial, bound_tier)

    if rollover == "next_business_day":
        return next_business_day(raw, holiday_set)
    elif rollover == "previous_business_day":
        return previous_business_day(raw, holiday_set)
    elif rollover == "none":
        return raw
    else:
        raise ValueError(f"Unknown rollover: {rollover!r}")


# ─── rebuttal expert chain trigger ───────────────────────────────────────────

def compute_rebuttal_expert_date(
    defendant_expert_disclosure_date: Optional[date],
    trial_date: date,
    holiday_set: str = "federal+arizona",
    provisional_trial_offset: int = 60,
) -> tuple[date, bool]:
    """
    Compute the rebuttal expert disclosure deadline.

    CONFIRMED formula:
        rebuttal_deadline = defendant_expert_disclosure_date + 30 days

    If defendant_expert_disclosure_date is None (not yet known), returns a
    PROVISIONAL estimate of trial_date − provisional_trial_offset with
    is_provisional=True so the caller can display a warning.

    Parameters
    ----------
    defendant_expert_disclosure_date : Actual date defendant served expert
                                       disclosures, or None if not yet known.
    trial_date                       : Trial date (used only for provisional estimate).
    holiday_set                      : Holiday set for rollover.
    provisional_trial_offset         : Days before trial for provisional estimate.

    Returns
    -------
    (date, is_provisional) : Computed date and whether it is provisional.

    Notes
    -----
    Caller MUST display a visible warning when is_provisional is True:
    "Estimated — update when defendant discloses experts."
    Also warn if computed date falls fewer than 30 days before trial.
    """
    if defendant_expert_disclosure_date is None:
        provisional = next_business_day(
            trial_date - timedelta(days=provisional_trial_offset),
            holiday_set,
        )
        return provisional, True

    result = next_business_day(
        defendant_expert_disclosure_date + timedelta(days=30),
        holiday_set,
    )
    return result, False


# ─── CMO override logic ───────────────────────────────────────────────────────

class StatutoryDeadlineOverrideError(ValueError):
    """Raised when a CMO date would push past a statutory / JURISDICTIONAL deadline."""
    pass


def apply_cmo_override(
    formula_date: date,
    cmo_date: Optional[date],
    cmo_override_value: str,
    statutory_deadline: Optional[date] = None,
    rule_id: str = "",
) -> date:
    """
    Apply CMO override logic per the brief's implementation pattern.

    CMO Override Value         System Behavior
    -----------------------    -----------------------------------------------
    "No – Statutory"           Ignore cmo_date. If cmo_date > statutory_date,
                               raise StatutoryDeadlineOverrideError.
    "Court extension only"     Use formula_date. Accept cmo_date only if
                               caller passes it (treated as court order).
    "Yes – CMO sets date"      If cmo_date provided: use it. Else: formula_date.
    "N/A – Condition precedent" Returns formula_date (no date calculation).
    "N/A – Strategic"          Returns formula_date (informational only).

    Parameters
    ----------
    formula_date        : The calculated deadline date.
    cmo_date            : Optional date from the scheduling order.
    cmo_override_value  : One of the five CMO override strings above.
    statutory_deadline  : For "No – Statutory" rows — the hard statutory date.
                          If cmo_date > statutory_deadline, error is raised.
    rule_id             : Rule identifier for error messages.

    Returns
    -------
    date : The final deadline date to use.
    """
    v = cmo_override_value.strip()

    if v == "No – Statutory":
        if cmo_date is not None and statutory_deadline is not None:
            if cmo_date > statutory_deadline:
                raise StatutoryDeadlineOverrideError(
                    f"Rule {rule_id!r}: CMO date {cmo_date} is LATER than statutory "
                    f"deadline {statutory_deadline}. CMO cannot override a statutory "
                    f"deadline. System refuses to apply the CMO date."
                )
        return formula_date

    elif v == "Yes – CMO sets date":
        return cmo_date if cmo_date is not None else formula_date

    elif v == "Court extension only":
        # Accept court_extension_date only if a court order is documented
        return cmo_date if cmo_date is not None else formula_date

    elif v in ("N/A – Condition precedent", "N/A – Strategic"):
        return formula_date

    else:
        warnings.warn(
            f"Rule {rule_id!r}: Unknown cmo_override_value {v!r}. "
            f"Returning formula_date.",
            stacklevel=2,
        )
        return formula_date


# ─── missing trigger warning ──────────────────────────────────────────────────

def warn_missing_trigger(trigger_name: str, rule_id: str) -> None:
    """
    Emit a UserWarning when a required trigger date is not yet available.

    The engine calls this when a rule's trigger date is missing from the
    trigger_map so that callers / UIs can surface the warning to users.

    Parameters
    ----------
    trigger_name : e.g. "defendant_expert_disclosure_date"
    rule_id      : e.g. "az_state_rebuttal_expert"
    """
    warnings.warn(
        f"Rule {rule_id!r}: trigger date '{trigger_name}' is not yet available. "
        f"Deadline will be computed once this date is entered.",
        UserWarning,
        stacklevel=2,
    )