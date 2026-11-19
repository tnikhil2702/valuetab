import asyncio
import csv
import io
import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Body
from fastapi.responses import StreamingResponse
from db import get_db, PeakTracAccount
from auth_utils import get_current_user
from db import NeosCode
from PeakTrac.neos_categories import mapping_list, save_mapping, refresh_codes, expense_code_options
from PeakTrac.neos_projections import latest_inputs, compute_inputs
from PeakTrac.neos_payoff import comparison, save_review
from PeakTrac.neos_session import get_neos_client
from PeakTrac.neos_client import NeosAuthError, NeosRequestError
from PeakTrac.neos_expenses import account_expense_report, accounts_with_unmatched_expenses

router = APIRouter(dependencies=[Depends(get_current_user)])


def finance(user=Depends(get_current_user)):
    return str(user.get('email') or user.get('sub') or 'unknown')

def account(db, account_id):
    row = db.get(PeakTracAccount, account_id)
    if row is None: raise HTTPException(404, 'Account not found.')
    return row


async def neos_job(job, db):
    try: return await job(db, get_neos_client())
    except NeosAuthError:
        db.rollback(); raise HTTPException(503, 'Neos sign-in unavailable.')
    except NeosRequestError:
        db.rollback(); raise HTTPException(502, 'Neos request failed; previous data retained.')
    except ValueError as exc:
        db.rollback(); raise HTTPException(409, str(exc))


@router.get('/categories/neos')
def categories(db=Depends(get_db)):
    return {'data': mapping_list(db)}


@router.get('/categories/expense-codes')
async def expense_codes():
    try:
        return {'data': await expense_code_options(get_neos_client())}
    except NeosAuthError:
        raise HTTPException(503, 'Neos sign-in unavailable. Try again shortly.')
    except (NeosRequestError, ValueError):
        raise HTTPException(502, 'Expense categories are unavailable from Neos right now. Try again shortly.')


@router.get('/admin/neos/codes')
def codes(db=Depends(get_db), actor=Depends(finance)):
    return {'data': [{'kind': r.kind, 'code': r.code} for r in db.query(NeosCode).all()]}


@router.post('/admin/neos/codes/refresh')
async def code_refresh(db=Depends(get_db), actor=Depends(finance)):
    return await neos_job(refresh_codes, db)


@router.put('/admin/neos/categories')
def mapping(body: dict = Body(...), db=Depends(get_db), actor=Depends(finance)):
    try: return {'data': save_mapping(db, body.get('category'), str(body.get('neos_code') or ''), actor)}
    except ValueError as exc: raise HTTPException(400, str(exc))


@router.get('/projections/inputs')
def inputs(db=Depends(get_db)):
    return {'data': latest_inputs(db)}


_refresh = {'running': False, 'error': None, 'finished_at': None}


async def _refresh_in_background():
    # Own DB session: the request that started this has already returned.
    from db import SessionLocal
    db = SessionLocal()
    try:
        await compute_inputs(db, get_neos_client())
        _refresh['error'] = None
    except NeosAuthError:
        db.rollback(); _refresh['error'] = 'Neos sign-in unavailable.'
    except NeosRequestError:
        db.rollback(); _refresh['error'] = 'Neos request failed; previous data retained.'
    except ValueError as exc:
        db.rollback(); _refresh['error'] = str(exc)
    except Exception:
        db.rollback(); _refresh['error'] = 'Unexpected error; see server log.'
        logging.getLogger('peaktrac.neos').exception('Projection refresh failed')
    finally:
        db.close()
        _refresh['running'] = False
        _refresh['finished_at'] = datetime.utcnow().isoformat() + 'Z'


@router.post('/projections/inputs/refresh', status_code=202)
async def inputs_refresh(actor=Depends(finance)):
    # Walking hundreds of closed cases takes minutes: longer than a web request may last.
    if not _refresh['running']:
        _refresh.update(running=True, error=None)
        asyncio.create_task(_refresh_in_background())
    return {'status': 'running'}


@router.get('/projections/inputs/refresh/status')
def inputs_refresh_status(db=Depends(get_db)):
    return {**_refresh, 'data': latest_inputs(db)}


@router.get('/payoff-queue')
def payoff_queue(db=Depends(get_db), actor=Depends(finance)):
    try:
        return {'data': [comparison(db, a)[0] for a in db.query(PeakTracAccount).filter(
            PeakTracAccount.account_status == 'Payoff Pending').all()]}
    except ValueError as exc: raise HTTPException(409, str(exc))


@router.post('/accounts/{account_id}/payoff-review')
def review(account_id: int, body: dict = Body(...), db=Depends(get_db), actor=Depends(finance)):
    try: return {'data': save_review(db, account(db, account_id), body.get('note'), actor)}
    except ValueError as exc: raise HTTPException(400, str(exc))


@router.get('/accounts/{account_id}/payoff/export')
def export_payoff(account_id: int, db=Depends(get_db), actor=Depends(finance)):
    try: data, _ = comparison(db, account(db, account_id))
    except ValueError as exc: raise HTTPException(409, str(exc))
    if not data['can_export']: raise HTTPException(409, 'Save a reviewer note for the current figures before export.')
    out = io.StringIO(); writer = csv.writer(out)
    writer.writerow(['Account', 'Good through', 'Principal', 'Interest', 'Payoff', 'Neos due to firm', 'Difference'])
    q = data['quote']
    writer.writerow([account_id, q['good_through'], q['principal'], q['accrued_interest'], q['total_payoff'],
                     data['neos_due_to_firm'], data['difference']])
    return StreamingResponse(iter([out.getvalue()]), media_type='text/csv',
                             headers={'Content-Disposition': 'attachment; filename="payoff.csv"'})


@router.get('/neos/action-queue')
def alerts(db=Depends(get_db)):
    rows = db.query(PeakTracAccount).all()
    return {'settlement_phase': [a.id for a in rows if a.settlement_phase],
            'closed_unrecovered': [a.id for a in rows if a.closed_unrecovered],
            'unmatched_expenses': list(accounts_with_unmatched_expenses(db)['by_account'])}


@router.get('/neos/parallel-run/export')
def parallel_export(db=Depends(get_db), actor=Depends(finance)):
    out = io.StringIO(); writer = csv.writer(out)
    writer.writerow(['Account', 'Case', 'PeakTrac funded', 'Neos due to firm', 'Difference', 'Neos last seen'])
    from db import NeosExpenseLine
    for a in db.query(PeakTracAccount).all():
        t = account_expense_report(db, a.id)['totals']
        if t['difference'] != 0:
            lines = db.query(NeosExpenseLine).filter(NeosExpenseLine.account_id == a.id).all()
            seen = max((r.last_seen_at for r in lines if r.last_seen_at), default=None)
            writer.writerow([a.id, a.case_number, t['peaktrac_funded'], t['neos_due_to_firm'], t['difference'], seen])
    return StreamingResponse(iter([out.getvalue()]), media_type='text/csv',
                             headers={'Content-Disposition': 'attachment; filename="parallel-run-differences.csv"'})