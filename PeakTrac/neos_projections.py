"""PT-12: publish a snapshot only after every selected case was read successfully."""
import asyncio
import calendar
import logging
import os
from datetime import date, datetime
from decimal import Decimal
from PeakTrac.neos_client import case_search_body, query_filter, NeosRequestError
from PeakTrac.neos_details import parse_date, truthy
from PeakTrac.neos_expenses import _records, _first, _money, _is_due_to_firm
from PeakTrac.neos_watcher import parse_statement_expenses
from db import ProjectionInput


def settlement_total(payload):
    from PeakTrac.neos_watcher import _first_flat, _CODE_KEYS, _AMOUNT_KEYS
    found = []
    def walk(node):
        if isinstance(node, list):
            for child in node: walk(child)
        elif isinstance(node, dict):
            code = str(_first_flat(node, _CODE_KEYS) or '').casefold()
            amount = _money(_first_flat(node, _AMOUNT_KEYS))
            if code == 'settle' and amount is not None:
                found.append(amount)
                return
            for child in node.values():
                if isinstance(child, (dict, list)): walk(child)
    walk(payload)
    return sum(found, Decimal(0)) if found else None


log = logging.getLogger('peaktrac.neos.projections')
MAX_CASES = int(os.getenv('PEAKTRAC_PROJECTION_MAX_CASES', '300'))
CONCURRENCY = 5
PAGE = 100


async def _closed_cases(client, cutoff, today):
    from PeakTrac.neos_client import _find_key
    body = case_search_body([query_filter('IsClosed', 'true')], take=PAGE,
                            columns=['CaseNumber', 'CaseType_Code', 'IsClosed', 'DateClosed'],
                            order_by='DateClosed')
    rows, skip = [], 0
    while len(rows) < MAX_CASES:
        data = await client.advanced_search({**body, 'Skip': skip})
        page = _find_key(data, 'Results') or []
        total = int(_find_key(data, 'TotalResultsCount') or 0)
        in_window = 0
        for row in page:
            if truthy(row.get('IsClosed')) is not True: continue
            end = parse_date(row.get('DateClosed'))
            if end is None or not cutoff <= end <= today: continue
            in_window += 1
            rows.append((row, end))
        skip += PAGE
        # Sorted newest-first: a page with nothing in the window means we are past it.
        if not page or skip >= total or in_window == 0: break
    return rows[:MAX_CASES]


async def _read_case(client, row, end):
    """One closed case -> (case_type, expense, won, life_days) or None if it can't be read."""
    case_id = row.get('Id')
    if not case_id: return None
    statement = await client.get(f'/cases/{case_id}/value_items/settlement')
    if not statement: return None        
    expense = parse_statement_expenses(statement) or Decimal(0)
    settle = settlement_total(statement) or Decimal(0)
    first_dates, token, tokens = [], None, set()
    while True:
        params = {'caseId': case_id, 'type': 'Value', 'pageSize': 200}
        if token: params['ContinuationToken'] = token
        payload = await client.get('/timesandvalues', params=params)
        for line in _records(payload):
            if _is_due_to_firm(line):
                day = parse_date(_first(line, ('ServiceDate', 'DueDate', 'Due', 'Date')))
                if day: first_dates.append(day)
        token = _first(payload, ('ContinuationToken',)) if isinstance(payload, dict) else None
        if not token or token in tokens: break
        tokens.add(token)
    first = min(first_dates) if first_dates else None
    life = (end - first).days if first and first <= end else None
    return str(row.get('CaseType_Code') or 'Unknown'), expense, settle > 0, life


async def compute_inputs(db, client, today=None):
    today = today or date.today()
    cutoff = today.replace(year=today.year - 3,
                           day=min(today.day, calendar.monthrange(today.year - 3, today.month)[1]))
    cases = await _closed_cases(client, cutoff, today)
    if not cases:
        raise ValueError('Neos returned no closed cases in the last 36 months; snapshot not changed.')
    sem, seen = asyncio.Semaphore(CONCURRENCY), set()

    async def work(row, end):
        async with sem:
            try: return await _read_case(client, row, end)
            except NeosRequestError:
                log.warning('Skipping closed case %s: Neos read failed', row.get('CaseNumber'))
                return None

    unique = []
    for row, end in cases:
        if row.get('Id') and row['Id'] not in seen:
            seen.add(row['Id']); unique.append((row, end))
    results = [r for r in await asyncio.gather(*(work(r, e) for r, e in unique)) if r]
    log.info('Projection inputs: %d of %d closed cases readable', len(results), len(unique))
    groups = {}
    for key, expense, won, life in results:
        g = groups.setdefault(key, {'expenses': [], 'lives': [], 'wins': 0})
        g['expenses'].append(expense); g['wins'] += int(won)
        if life is not None: g['lives'].append(life)
    if not groups: raise ValueError('No closed case could be read from Neos; snapshot not changed.')
    result = []
    for key, g in sorted(groups.items()):
        n = len(g['expenses'])
        result.append({'case_type': key, 'n_cases': n,
                       'average_case_expense': float(sum(g['expenses']) / n),
                       'average_draw_life_days': sum(g['lives']) / len(g['lives']) if g['lives'] else None,
                       'n_draw_life_cases': len(g['lives']), 'win_rate_pct': 100 * g['wins'] / n,
                       'draw_life_end_basis': 'DateClosed'})
    snapshot = ProjectionInput(computed_at=datetime.utcnow(), n_cases=len(results), data=result)
    db.add(snapshot); db.commit()
    return latest_inputs(db)


def latest_inputs(db):
    row = db.query(ProjectionInput).order_by(ProjectionInput.id.desc()).first()
    return None if row is None else {'snapshot_id': row.id, 'computed_at': row.computed_at.isoformat() + 'Z',
                                    'n_cases': row.n_cases, 'data': row.data}
