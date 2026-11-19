from datetime import datetime, date

CASE_RATE_PCT = 7.75
FIRM_OPERATING_RATE_PCT = 7.50


def current_case_rate_pct() -> float:
    return CASE_RATE_PCT


def current_firm_operating_rate_pct() -> float:
    return FIRM_OPERATING_RATE_PCT


def days_between(start: datetime, end: datetime) -> int:
    if isinstance(start, datetime):
        start = start.date()
    if isinstance(end, datetime):
        end = end.date()
    return max((end - start).days, 0)


def accrued_interest(
    principal: float,
    annual_rate_pct: float,
    start: datetime,
    as_of: datetime = None,
) -> float:
    as_of = as_of or datetime.utcnow()
    days = days_between(start, as_of)
    daily_rate = (annual_rate_pct / 100) / 365
    return round(principal * daily_rate * days, 2)


def per_diem(principal: float, annual_rate_pct: float) -> float:
    daily_rate = (annual_rate_pct / 100) / 365
    return round(principal * daily_rate, 2)


def payoff_quote(
    funded_draws: list[dict],
    annual_rate_pct: float,
    good_through: date = None,
) -> dict:
    good_through = good_through or date.today()
    good_through_dt = datetime.combine(good_through, datetime.min.time())

    principal = round(sum(d["amount"] for d in funded_draws), 2)

    line_items = []
    total_interest = 0.0
    for d in funded_draws:
        interest = accrued_interest(
            d["amount"], annual_rate_pct, d["funded_at"], good_through_dt
        )
        total_interest += interest
        line_items.append(
            {
                "amount": d["amount"],
                "funded_at": d["funded_at"].isoformat(),
                "days_outstanding": days_between(d["funded_at"], good_through_dt),
                "interest_accrued": interest,
            }
        )

    total_interest = round(total_interest, 2)
    daily_per_diem = per_diem(principal, annual_rate_pct)

    return {
        "principal": principal,
        "accrued_interest": total_interest,
        "per_diem": daily_per_diem,
        "good_through": good_through.isoformat(),
        "total_payoff": round(principal + total_interest, 2),
        "rate_pct": annual_rate_pct,
        "line_items": line_items,
    }