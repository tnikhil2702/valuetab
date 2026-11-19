"""Add the Neos Direct columns to leads_intake. Safe to run more than once.

    python -m MVA_Platform.migrate_neos_direct      (from the repo root)
"""

from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateColumn

from db import engine, LeadIntake

TABLE = "leads_intake"
COLUMNS = (
    "intake_channel", "neos_intake_id", "scoring_status", "write_neos_notes",
    "send_emails", "link_review", "link_candidate_lead_ids", "neos_intake_meta",
    "neos_direct_loaded_at",
)
INDEXES = {
    # index name -> DDL
    "ix_leads_intake_intake_channel": f"CREATE INDEX ix_leads_intake_intake_channel ON {TABLE} (intake_channel)",
    "uq_leads_intake_neos_intake_id": f"CREATE UNIQUE INDEX uq_leads_intake_neos_intake_id ON {TABLE} (neos_intake_id)",
}


def migrate() -> list[str]:
    done = []
    insp = inspect(engine)
    existing_cols = {c["name"] for c in insp.get_columns(TABLE)}
    with engine.begin() as conn:
        for name in COLUMNS:
            if name in existing_cols:
                continue
            col = LeadIntake.__table__.c[name]
            # CreateColumn renders type/NULL/server_default; unique/index handled below.
            ddl = str(CreateColumn(col).compile(dialect=engine.dialect))
            conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {ddl}"))
            done.append(f"added column {name}")

    # Neos "Stage | Status" text runs past the original VARCHAR(50); widen (non-destructive).
    status_col = next(c for c in inspect(engine).get_columns(TABLE) if c["name"] == "neos_status")
    if (getattr(status_col["type"], "length", None) or 0) < 255:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {TABLE} MODIFY neos_status VARCHAR(255) NULL"))
        done.append("widened neos_status to VARCHAR(255)")

    insp = inspect(engine)
    existing_idx = {i["name"] for i in insp.get_indexes(TABLE)}
    existing_idx |= {u["name"] for u in insp.get_unique_constraints(TABLE)}
    indexed_cols = {tuple(i["column_names"]) for i in insp.get_indexes(TABLE)}
    with engine.begin() as conn:
        for name, sql in INDEXES.items():
            col = "intake_channel" if "intake_channel" in name else "neos_intake_id"
            if name in existing_idx or (col,) in indexed_cols:
                continue
            conn.execute(text(sql))
            done.append(f"created index {name}")
    return done


if __name__ == "__main__":
    steps = migrate()
    print("\n".join(steps) if steps else "leads_intake already up to date")
