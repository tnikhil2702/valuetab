from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateColumn
from db import engine, PeakTracAccount, Draw, NeosExpenseLine
from db import NeosCode, CategoryMapping, ProjectionInput, PayoffReview
from PeakTrac.neos_categories import seed_categories
from sqlalchemy.orm import Session


def fix_firm_draw_timestamps():
    inspector = inspect(engine)
    if 'firm_operating_draws' not in inspector.get_table_names():
        return
    cols = {c['name']: c for c in inspector.get_columns('firm_operating_draws')}
    with engine.begin() as conn:
        if 'created_at' in cols and cols['created_at'].get('default') is None:
            conn.execute(text('ALTER TABLE firm_operating_draws MODIFY created_at '
                              'DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP'))
        if 'updated_at' in cols and cols['updated_at'].get('default') is None:
            conn.execute(text('ALTER TABLE firm_operating_draws MODIFY updated_at '
                              'DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP '
                              'ON UPDATE CURRENT_TIMESTAMP'))


def migrate():
    existing = {c['name'] for c in inspect(engine).get_columns('peaktrac_accounts')}
    fields = {'office', 'sol_date', 'policy_limit_max', 'limit_above_policy', 'neos_synced_at',
              'settlement_phase', 'closed_unrecovered', 'neos_is_closed', 'neos_date_closed',
              'neos_settlement_amount', 'neos_due_to_firm', 'neos_case_expenses_on_statement',
              'neos_settlement_checked_at'}
    with engine.begin() as conn:
        for name in sorted(fields - existing):
            column = PeakTracAccount.__table__.c[name]._copy()
            if name == 'limit_above_policy':
                from sqlalchemy import DefaultClause
                column.server_default = DefaultClause('0')
            definition = str(CreateColumn(column).compile(dialect=engine.dialect))
            conn.execute(text('ALTER TABLE peaktrac_accounts ADD COLUMN ' + definition))
    NeosExpenseLine.__table__.create(engine, checkfirst=True)
    expense_columns = {c['name'] for c in inspect(engine).get_columns('neos_expense_lines')}
    with engine.begin() as conn:
        for name in ('matched_draw_id', 'match_source', 'manual_unlinked', 'is_current'):
            if name not in expense_columns:
                definition = str(CreateColumn(NeosExpenseLine.__table__.c[name]).compile(dialect=engine.dialect))
                conn.execute(text('ALTER TABLE neos_expense_lines ADD COLUMN ' + definition))
    inspector = inspect(engine)
    unique_sets = {tuple(c['column_names']) for c in inspector.get_unique_constraints('neos_expense_lines')}
    unique_sets.update(tuple(i['column_names']) for i in inspector.get_indexes('neos_expense_lines') if i.get('unique'))
    for name in ('neos_value_id', 'matched_draw_id'):
        if (name,) in unique_sets: continue
        with engine.begin() as conn:
            duplicate = conn.execute(text(
                f'SELECT {name} FROM neos_expense_lines WHERE {name} IS NOT NULL '
                f'GROUP BY {name} HAVING COUNT(*) > 1 LIMIT 1')).first()
            if duplicate is not None:
                raise ValueError(f'Resolve duplicate {name} values before adding the unique index.')
            conn.execute(text(f'CREATE UNIQUE INDEX uq_neos_expense_lines_{name}_migration '
                              f'ON neos_expense_lines ({name})'))
    if 'neos_code'  not in {c['name'] for c in inspect(engine).get_columns('peaktrac_draws')}:
        with engine.begin() as conn:
            definition = str(CreateColumn(Draw.__table__.c.neos_code).compile(dialect=engine.dialect))
            conn.execute(text('ALTER TABLE peaktrac_draws ADD COLUMN ' + definition))
    fix_firm_draw_timestamps()
    for model in (NeosCode, CategoryMapping, ProjectionInput, PayoffReview):
        model.__table__.create(engine, checkfirst=True)
    with Session(engine) as db:
        seed_categories(db); db.commit()