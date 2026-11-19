from __future__ import annotations

CATEGORY_TO_NEOS_CODE: dict[str, str | None] = {
    "Medical Records": None,            # TODO Finance
    "Medical Treatment": None,          # TODO Finance (no UTBMS equivalent)
    "Expert Witness": "E119",
    "Deposition": "E115",
    "Court and Filing Fees": "E112",
    "Investigation": "E120",
    "Travel": "E110",
    "Other": "E124",
}


def _n(value) -> str:
    return " ".join(str(value or "").strip().casefold().split())


_CODE_TO_CATEGORY = {_n(c): k for k, c in CATEGORY_TO_NEOS_CODE.items() if c}
_CATEGORY_TO_CODE = {_n(k): c for k, c in CATEGORY_TO_NEOS_CODE.items() if c}


def neos_code_for_category(category, db=None) -> str | None:
    if db is not None:
        from db import CategoryMapping
        return next((r.neos_code for r in db.query(CategoryMapping).all()
                     if _n(r.category) == _n(category)), None)
    return _CATEGORY_TO_CODE.get(_n(category))


def category_for_neos_code(code, db=None) -> str | None:
    if db is not None:
        from db import CategoryMapping
        return next((r.category for r in db.query(CategoryMapping).all()
                     if r.neos_code and _n(r.neos_code) == _n(code)), None)
    return _CODE_TO_CATEGORY.get(_n(code))


def category_matches(draw_category, expense_code, db=None) -> bool:
    """True when the draw's category maps to the expense's Neos code.

    If the category has no mapping yet, fall back to the old literal comparison so
    behaviour never gets looser than before.
    """
    mapped = neos_code_for_category(draw_category, db)
    if db is not None and not mapped:
        return False
    if mapped:
        return _n(mapped) == _n(expense_code)
    return bool(_n(draw_category)) and _n(draw_category) == _n(expense_code)


def draw_matches_code(draw, expense_code, db=None) -> bool:
    """A draw carrying its own Neos code (chosen from the dropdown) matches on that code.
    Older draws without one fall back to the Finance category mapping."""
    code = getattr(draw, "neos_code", None)
    if code:
        return _n(code) == _n(expense_code)
    return category_matches(getattr(draw, "category", None), expense_code, db)