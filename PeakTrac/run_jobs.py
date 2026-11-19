"""Single-run entry point for Azure WebJob / a scheduler (one instance only)."""
import argparse
import asyncio
import json
from db import SessionLocal
from PeakTrac.neos_jobs import run_nightly_once
from PeakTrac.neos_session import get_neos_client
from PeakTrac.neos_categories import refresh_codes
from PeakTrac.neos_projections import compute_inputs


async def run(job):
    try:
        if job == 'nightly':
            return await run_nightly_once(SessionLocal)
        with SessionLocal() as db:
            try:
                return await {'codes': refresh_codes, 'projections': compute_inputs}[job](db, get_neos_client())
            except Exception:
                db.rollback()
                raise
    finally:
        await get_neos_client().aclose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('job', choices=['nightly', 'codes', 'projections'])
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.job)), default=str))
