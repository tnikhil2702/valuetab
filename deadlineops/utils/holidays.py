"""
holidays.py — Federal + state holiday sets for DeadlineOps.

Authoritative source for Arizona: A.R.S. §1-301 (updated 2022 with Juneteenth).
Authoritative source for Colorado: C.R.S. §24-11-101.

Usage:
    from deadlineops.utils.holidays import get_holidays, add_emergency_closure
    holidays = get_holidays("federal+arizona", year=2025)
"""

from datetime import date, timedelta
from typing import Set, Dict, List


# ─── module-level store for emergency/ad-hoc closures ───────────────────────
# Keyed by year → list of extra dates. Authorized users may call
# add_emergency_closure() to inject Governor-declared or judge-ordered
# emergency court-closure dates that cannot be predicted in advance.
_EMERGENCY_CLOSURES: Dict[int, List[date]] = {}


def add_emergency_closure(d: date) -> None:
    """
    Register an ad-hoc court closure (e.g. Governor-declared emergency,
    extreme weather) so it is treated as a holiday for deadline calculations.

    Parameters
    ----------
    d : The date of the emergency closure.
    """
    _EMERGENCY_CLOSURES.setdefault(d.year, [])
    if d not in _EMERGENCY_CLOSURES[d.year]:
        _EMERGENCY_CLOSURES[d.year].append(d)


def clear_emergency_closures(year: int = None) -> None:
    """
    Remove ad-hoc closures. Pass a year to clear only that year,
    or call with no argument to clear all.
    """
    global _EMERGENCY_CLOSURES
    if year is None:
        _EMERGENCY_CLOSURES = {}
    else:
        _EMERGENCY_CLOSURES.pop(year, None)


# ─── helpers ────────────────────────────────────────────────────────────────

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the n-th occurrence (1-based) of weekday (0=Mon … 6=Sun) in month."""
    first = date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    first_occurrence = date(year, month, 1 + delta)
    return date(year, month, first_occurrence.day + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the last occurrence of weekday in month."""
    import calendar as _cal
    last = date(year, month, _cal.monthrange(year, month)[1])
    delta = (last.weekday() - weekday) % 7
    return date(year, month, last.day - delta)


def _observed(d: date) -> date:
    """If d is Saturday return Friday; if Sunday return Monday."""
    if d.weekday() == 5:   # Saturday
        return d - timedelta(days=1)
    if d.weekday() == 6:   # Sunday
        return d + timedelta(days=1)
    return d


def _add_observed(holidays: Set[date], raw: date) -> None:
    """Add both the raw date and its observed substitute (if different)."""
    holidays.add(raw)
    obs = _observed(raw)
    if obs != raw:
        holidays.add(obs)


# ─── federal holidays ────────────────────────────────────────────────────────

def _federal_holidays(year: int) -> Set[date]:
    """
    Return the set of federal holidays for a given year.

    Fixed-date holidays (New Year's Day, Juneteenth, Independence Day,
    Veterans Day, Christmas Day) include observed substitutes per the
    Saturday→Friday / Sunday→Monday convention.

    Juneteenth (June 19) became a federal holiday in 2021 (Public Law 117-17).
    Many third-party libraries pre-2022 omit it — this implementation
    always includes it for any requested year.
    """
    h: Set[date] = set()

    # Fixed-date federal holidays (+ observed substitutes)
    fixed = [
        date(year, 1, 1),    # New Year's Day
        date(year, 6, 19),   # Juneteenth National Independence Day (since 2021)
        date(year, 7, 4),    # Independence Day
        date(year, 11, 11),  # Veterans Day
        date(year, 12, 25),  # Christmas Day
    ]
    for d in fixed:
        _add_observed(h, d)

    # Floating federal holidays
    h.add(_nth_weekday(year, 1, 0, 3))   # MLK Day: 3rd Monday in Jan
    h.add(_nth_weekday(year, 2, 0, 3))   # Presidents' Day: 3rd Monday in Feb
    h.add(_last_weekday(year, 5, 0))     # Memorial Day: last Monday in May
    h.add(_nth_weekday(year, 9, 0, 1))   # Labor Day: 1st Monday in Sep
    h.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving: 4th Thursday in Nov

    return h


# ─── Colorado state holidays ─────────────────────────────────────────────────

def _colorado_holidays(year: int) -> Set[date]:
    """
    Colorado state holidays beyond the federal set (C.R.S. §24-11-101).

    Additional CO state holiday:
      - César Chávez Day: March 31 (observed if falls on weekend)
    """
    h: Set[date] = set()
    _add_observed(h, date(year, 3, 31))  # César Chávez Day (C.R.S. § 24-11-101)
    return h


# ─── Arizona state holidays ──────────────────────────────────────────────────

def _arizona_holidays(year: int) -> Set[date]:
    """
    Arizona state holidays per A.R.S. §1-301.

    AZ observes all federal holidays plus:
      - Columbus Day / Indigenous Peoples' Day: 2nd Monday in October
        (A.R.S. §1-301 — AZ observes Columbus Day statewide)

    Note: MLK / Civil Rights Day (3rd Monday January) is the same calendar
    date as the federal MLK Day — already included in the federal set.
    Presidents' Day (3rd Monday February) is also the same as federal.
    Juneteenth (June 19, fixed) was added to AZ law in 2022 per A.R.S. §1-301
    and is already included in the federal set.
    """
    h: Set[date] = set()
    h.add(_nth_weekday(year, 10, 0, 2))  # Columbus Day: 2nd Monday in Oct
    return h


# ─── public API ──────────────────────────────────────────────────────────────

def get_holidays(holiday_set: str, year: int) -> Set[date]:
    """
    Return a set of holiday dates for the given holiday_set string and year.

    holiday_set values:
        "federal"            — federal holidays only
        "federal+colorado"   — federal + CO state holidays
        "federal+arizona"    — federal + AZ state holidays (A.R.S. §1-301)

    Also merges any emergency court closures registered via add_emergency_closure().
    """
    base = _federal_holidays(year)

    tag = holiday_set.lower().replace(" ", "")
    if "colorado" in tag:
        base |= _colorado_holidays(year)
    elif "arizona" in tag:
        base |= _arizona_holidays(year)

    # Merge ad-hoc emergency closures for this year
    for d in _EMERGENCY_CLOSURES.get(year, []):
        base.add(d)

    return base


def is_holiday(d: date, holiday_set: str) -> bool:
    """Return True if d is a holiday in the given holiday_set."""
    return d in get_holidays(holiday_set, d.year)