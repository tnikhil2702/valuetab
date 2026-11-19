import hashlib
import json
from datetime import datetime
from decimal import Decimal
from db import Draw
from PeakTrac.interest import payoff_quote
from db import PayoffReview


def comparison(db, account):
    draws = db.query(Draw).filter(Draw.account_id == account.id, Draw.draw_status == 'Funded').all()
    if any(d.funded_at is None for d in draws):
        raise ValueError('Funded draw has no funded date; correct it before quoting.')
    quote = payoff_quote([{'amount': float(d.amount), 'funded_at': d.funded_at} for d in draws],
                         float(account.interest_rate))
    due = account.neos_due_to_firm
    difference = None if due is None else Decimal(str(quote['total_payoff'])) - Decimal(str(due))
    data = {'account_id': account.id, 'case_number': account.case_number, 'quote': quote,
            'neos_due_to_firm': float(due) if due is not None else None,
            'neos_case_expenses_on_statement': float(account.neos_case_expenses_on_statement)
            if account.neos_case_expenses_on_statement is not None else None,
            'difference': float(difference) if difference is not None else None,
            'neos_settlement_checked_at': account.neos_settlement_checked_at.isoformat()
            if account.neos_settlement_checked_at else None}
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    review = db.get(PayoffReview, account.id)
    required = difference is None or abs(difference) > Decimal('1.00')
    data.update(review_required=required, review_current=bool(review and review.fingerprint == fingerprint))
    data['can_export'] = not required or data['review_current']
    return data, fingerprint


def save_review(db, account, note, actor):
    if not isinstance(note, str) or not note.strip() or len(note) > 4000:
        raise ValueError('Reviewer note must contain 1–4000 characters.')
    _, fingerprint = comparison(db, account)
    row = db.get(PayoffReview, account.id) or PayoffReview(account_id=account.id)
    row.note, row.reviewed_by = note.strip(), actor
    row.reviewed_at, row.fingerprint = datetime.utcnow(), fingerprint
    db.add(row); db.commit()
    return comparison(db, account)[0]
