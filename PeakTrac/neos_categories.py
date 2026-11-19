import time
from datetime import datetime
from PeakTrac.neos_expenses import _records, _first
from db import NeosCode, CategoryMapping
from PeakTrac.category_map import CATEGORY_TO_NEOS_CODE

def seed_categories(db):
    for category in CATEGORY_TO_NEOS_CODE:
        if db.get(CategoryMapping, category) is None:
            db.add(CategoryMapping(category=category))
    db.flush()


def _code_set(rows, keys, skip_inactive=False):
    out = set()
    for row in rows:
        if skip_inactive and row.get('Active') is False:
            continue
        value = _first(row, keys)
        if value is not None and str(value).strip():
            out.add(str(value).strip())
    return out


async def refresh_codes(db, client):
    now = datetime.utcnow()
    incoming = []
    for kind, path, keys, skip_inactive in [
        ('expense', '/v2/utbmsexpensecodes', ('UtbmsExpenseCode',), True),
        ('type', '/valuecode/types', ('Code', 'ValueCode', 'Name'), False),
    ]:
        codes = _code_set(_records(await client.get(path)), keys, skip_inactive)
        if not codes:
            raise ValueError('Neos code list was empty or unrecognized; previous lookup retained.')
        incoming.extend((kind, code) for code in codes)
    db.query(NeosCode).delete()
    for kind, code in incoming:
        db.add(NeosCode(key=f'{kind}:{code}', kind=kind, code=code, refreshed_at=now))
    seed_categories(db)
    db.commit()
    return {'count': len(incoming), 'refreshed_at': now.isoformat() + 'Z'}


def mapping_list(db):
    return [{'category': row.category, 'neos_code': row.neos_code,
             'ready': bool(row.neos_code and db.get(NeosCode, 'expense:' + row.neos_code))}
            for row in db.query(CategoryMapping).order_by(CategoryMapping.category).all()]


def save_mapping(db, category, code, actor):
    row = db.get(CategoryMapping, category)
    if row is None:
        raise ValueError('Unknown category. Use an existing PeakTrac category.')
    if not db.get(NeosCode, 'expense:' + code):
        raise ValueError('Choose a code from the refreshed Neos expense list.')
    other = db.query(CategoryMapping).filter(CategoryMapping.neos_code == code,
                                            CategoryMapping.category != category).first()
    if other:
        raise ValueError('Code already mapped; one category per code keeps prefills unambiguous.')
    row.neos_code, row.updated_by, row.updated_at = code, actor, datetime.utcnow()
    db.commit()
    return mapping_list(db)


CODE_CACHE_SECONDS = 3600
_option_cache: dict = {"at": 0.0, "data": []}


def parse_expense_code_options(payload):
    options = []
    for row in _records(payload):
        if row.get('Active') is False:
            continue
        code = str(row.get('UtbmsExpenseCode') or '').strip()
        if not code:
            continue
        description = str(row.get('UtbmsExpenseCodeDescription') or '').strip()
        options.append({'code': code, 'description': description,
                        'label': f'{code} \u2013 {description}' if description else code})
    return sorted(options, key=lambda o: o['code'])


async def expense_code_options(client, ttl=CODE_CACHE_SECONDS):
    now = time.monotonic()
    if _option_cache['data'] and now - _option_cache['at'] < ttl:
        return _option_cache['data']
    try:
        options = parse_expense_code_options(await client.get('/v2/utbmsexpensecodes'))
    except Exception:
        if _option_cache['data']:
            return _option_cache['data']
        raise
    if not options:
        raise ValueError('Neos expense code list was empty or unrecognized.')
    _option_cache.update(at=now, data=options)
    return options