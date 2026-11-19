import os
import time
import hmac
import uuid
import asyncio
import logging
import threading

from datetime import datetime, timedelta, time as dtime, timezone
from zoneinfo import ZoneInfo

import httpx

from fastapi import HTTPException
from sqlalchemy import exists, func, and_
from sqlalchemy.exc import IntegrityError

from db import SessionLocal, LeadIntake, LeadOwlReview

from MVA_Platform.lead_utils import (
    DECISIONS,
    current_prompt_version,
)

from MVA_Platform.outlook_utils import build_classifier_summary


logger = logging.getLogger("owl_service")


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

OWL_URL = (
    os.getenv(
        "OWL_API_URL",
        "https://nebula-whatsapp.onrender.com/api/intake/review",
    )
    or ""
).strip()

OWL_KEY = (
    os.getenv("INTAKEOPS_REVIEW_KEY")
    or ""
).strip()

OWL_EDIT_CODE = (
    os.getenv("OWL_EDIT_CODE")
    or ""
).strip()


SECONDS_BETWEEN_CALLS = 2.1
MAX_502_RETRIES = 2

MAX_CODE_FAILS = 5
FAIL_WINDOW_SECONDS = 900

# No hard-coded per-run lead cap. limit=None means all eligible leads.

# A run whose worker has made no progress for this long is treated as
# dead and released automatically so it can never block new runs.
# One lead can legitimately take ~3 minutes (60s timeout + 429 wait +
# 502 retries), so keep this comfortably above that.
STALE_RUN_SECONDS = int(os.getenv("OWL_STALE_RUN_SECONDS", "600"))

# Grace period between reserving a run and its worker task starting.
QUEUE_GRACE_SECONDS = 60

ACTIVE_STATES = ("queued", "running", "stopping")


NAFI_TO_OWL = {
    "likely_nafi": "Likely NAFI",
    "not_nafi": "Not NAFI",
}

OWL_SCHEDULER_ENABLED = (
    (os.getenv("OWL_SCHEDULER_ENABLED") or "true").strip().lower() == "true"
)
MST = ZoneInfo("America/Denver")
OWL_RUN_TIMES = [dtime(6, 0), dtime(16, 0)]

OWL_BUSY_RETRIES = 6
OWL_BUSY_RETRY_SECONDS = 600

OWL_LIMIT_FILLS_OUTSIDE_RANGE = False  # Date-range runs never expand outside their dates.

RUNS = {}

ACTIVE_RUN_ID = None

RUN_LOCK = threading.RLock()

_CODE_FAILS = {}

_TASKS = {}


class OwlAuthError(Exception):
    """Owl API authentication failed."""

def _finalize_run(run, status, reason=None):

    global ACTIVE_RUN_ID

    run["status"] = status
    run["running"] = False
    run["finished_at"] = run["finished_at"] or datetime.utcnow()

    if reason and not run.get("stopped_reason"):
        run["stopped_reason"] = reason

    if ACTIVE_RUN_ID == run["run_id"]:
        ACTIVE_RUN_ID = None


def _touch(run):
    """Record worker progress (heartbeat)."""
    run["last_activity_at"] = datetime.utcnow()


def _task_alive(run_id):
    task = _TASKS.get(run_id)
    return task is not None and not task.done()


def _cancel_task(run_id):
    task = _TASKS.get(run_id)
    if task is not None and not task.done():
        task.cancel()


def _stuck_reason(run):

    if run["status"] not in ACTIVE_STATES:
        return None

    now = datetime.utcnow()

    if not _task_alive(run["run_id"]):
        age = (now - run["started_at"]).total_seconds()
        if run["run_id"] in _TASKS or age > QUEUE_GRACE_SECONDS:
            return "Worker is not running (crashed or never started)."
        return None

    idle = (now - run["last_activity_at"]).total_seconds()
    if idle > STALE_RUN_SECONDS:
        return f"No progress for {int(idle)} seconds."

    return None


def refresh_owl_runs():
    with RUN_LOCK:
        _reap_stuck_runs()


def _reap_stuck_runs():

    for run in RUNS.values():

        reason = _stuck_reason(run)

        if not reason:
            continue

        _cancel_task(run["run_id"])

        _finalize_run(
            run,
            "stopped" if run["stop_requested"] else "failed",
            f"Auto-released: {reason}",
        )

        logger.warning(
            "[OWL] Auto-released stuck run. run_id=%s reason=%s",
            run["run_id"],
            reason,
        )

def denver_date_bounds_utc(start_date=None, end_date=None):
    start_utc = None
    end_utc = None

    if start_date:
        local_start = datetime.combine(
            start_date, dtime.min, tzinfo=MST
        )
        start_utc = local_start.astimezone(timezone.utc).replace(tzinfo=None)

    if end_date:
        local_end = datetime.combine(
            end_date + timedelta(days=1), dtime.min, tzinfo=MST
        )
        end_utc = local_end.astimezone(timezone.utc).replace(tzinfo=None)

    return start_utc, end_utc



def prepare_owl_run(
    db,
    requested_by,
    start_date,
    end_date,
    statuses,
    instruction,
    limit=None,
    lead_ids=None,
):
    global ACTIVE_RUN_ID

    if not OWL_URL:
        raise HTTPException(
            status_code=500,
            detail="OWL_API_URL is not configured.",
        )

    if not OWL_KEY:
        raise HTTPException(
            status_code=500,
            detail="INTAKEOPS_REVIEW_KEY is not configured.",
        )

    effective_statuses = (
        list(statuses) if statuses else list(DECISIONS)
    )

    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer.")

    if lead_ids is not None:
        if not lead_ids:
            raise ValueError("lead_ids cannot be empty.")

        if len(lead_ids) != len(set(lead_ids)):
            raise ValueError("Duplicate lead IDs are not allowed.")

        if limit is not None:
            raise ValueError(
                "Use either lead_ids or limit, not both."
            )

    # None means all eligible leads. An integer means that exact maximum.
    effective_limit = limit

    with RUN_LOCK:

        _reap_stuck_runs()

        if ACTIVE_RUN_ID:
            active = RUNS.get(ACTIVE_RUN_ID)

            if active and active["status"] in ACTIVE_STATES:
                raise RuntimeError(
                    "An Owl review is already running. "
                    f"Active run ID: {ACTIVE_RUN_ID}"
                )

        # ---------------------------------------
        # Build the base date/status query.
        # This query includes already-reviewed leads.
        # ---------------------------------------

        base_query = db.query(LeadIntake).filter(
            LeadIntake.status.in_(effective_statuses)
        )

        start_utc, end_utc = denver_date_bounds_utc(
            start_date,
            end_date,
        )

        if start_utc is not None:
            base_query = base_query.filter(
                LeadIntake.received_at >= start_utc
            )

        if end_utc is not None:
            base_query = base_query.filter(
                LeadIntake.received_at < end_utc
            )

        # ---------------------------------------
        # Explicit lead ID rerun.
        # Previously reviewed Owl leads are allowed.
        # ---------------------------------------

        if lead_ids is not None:

            base_query = base_query.filter(
                LeadIntake.id.in_(lead_ids)
            )

            available_count = base_query.count()

            selected_leads = (
                unreviewed_query(
                    db=db,
                    start_date=start_date,
                    end_date=end_date,
                    statuses=effective_statuses,
                    lead_ids=lead_ids,
                )
                .order_by(
                    LeadIntake.received_at.desc(),
                    LeadIntake.id.desc(),
                )
                .all()
            )

            selected_ids = {
                lead.id for lead in selected_leads
            }

            invalid_ids = set(lead_ids) - selected_ids

            if invalid_ids:
                raise ValueError(
                    "Some requested lead IDs are missing, "
                    "outside the selected filters, or protected "
                    "by a human review: "
                    + ", ".join(
                        map(str, sorted(invalid_ids))
                    )
                )

            selected_ids = [
                lead.id for lead in selected_leads
            ]

            considered_count = len(lead_ids)

            previously_reviewed_count = (
                db.query(LeadOwlReview.lead_id)
                .filter(
                    LeadOwlReview.lead_id.in_(selected_ids)
                )
                .count()
            )

            already_reviewed_skipped = 0
            human_reviewed_skipped = 0
            human_reviewed = []
            beyond_limit = 0

            # Explicit reruns process previously reviewed leads.
            selected_count = len(selected_ids)

        # ---------------------------------------
        # Normal date-range review.
        # Exclude Owl-reviewed and human-reviewed leads BEFORE applying
        # the optional limit, so the run fills with eligible leads.
        else:

            available_count = base_query.count()

            eligible_query = unreviewed_query(
                db=db,
                start_date=start_date,
                end_date=end_date,
                statuses=effective_statuses,
            )

            eligible_count = eligible_query.count()

            selected_query = eligible_query.order_by(
                LeadIntake.received_at.desc(),
                LeadIntake.id.desc(),
            )

            if effective_limit is not None:
                selected_query = selected_query.limit(effective_limit)

            selected_leads = selected_query.all()
            selected_ids = [lead.id for lead in selected_leads]

            considered_count = len(selected_ids)
            selected_count = considered_count

            # Count exclusions for run reporting.
            already_reviewed_skipped = (
                db.query(func.count(LeadOwlReview.lead_id))
                .filter(
                    LeadOwlReview.lead_id.in_(
                        base_query.with_entities(LeadIntake.id).subquery()
                    )
                )
                .scalar()
                or 0
            )

            # Collect the exact human-protected leads and the reason for each.
            human_reviewed = []

            for (hid,) in (
                base_query
                .filter(LeadIntake.reviewer_decision.isnot(None))
                .with_entities(LeadIntake.id)
                .order_by(LeadIntake.id)
                .all()
            ):
                human_reviewed.append({
                    "lead_id": hid,
                    "skip_reason": "human_reviewed",
                })

            legacy_col = getattr(LeadIntake, "agree_disagree", None)
            if legacy_col is not None:
                for (hid,) in (
                    base_query
                    .filter(
                        legacy_col.isnot(None),
                        LeadIntake.reviewer_decision.is_(None),
                    )
                    .with_entities(LeadIntake.id)
                    .order_by(LeadIntake.id)
                    .all()
                ):
                    human_reviewed.append({
                        "lead_id": hid,
                        "skip_reason": "legacy_human_validation",
                    })

            human_reviewed_skipped = len(human_reviewed)

            previously_reviewed_count = 0
            beyond_limit = (
                max(0, eligible_count - selected_count)
                if effective_limit is not None
                else 0
            )

        # Reserve the run.
        # ---------------------------------------

        run_id = str(uuid.uuid4())
        now = datetime.utcnow()

        RUNS[run_id] = {
            "run_id": run_id,

            "running": True,
            "status": "queued",

            "started_by": requested_by,
            "started_at": now,
            "finished_at": None,

            "lead_ids": selected_ids,

            "total": selected_count,
            "done": 0,
            "skipped": 0,
            "failed": 0,

            "considered": considered_count,
            "available": available_count,

            "already_reviewed_skipped": (
                already_reviewed_skipped
            ),

            "human_reviewed_skipped": (
                human_reviewed_skipped
            ),

            "previously_reviewed_selected": (
                previously_reviewed_count
            ),

            "human_reviewed": human_reviewed,

            "beyond_limit": beyond_limit,

            "lead_results": [],

            "stop_requested": False,
            "stop_requested_at": None,
            "last_activity_at": now,

            "stopped_reason": None,
            "last_error": None,

            "start_date": (
                start_date.isoformat()
                if start_date else None
            ),

            "end_date": (
                end_date.isoformat()
                if end_date else None
            ),

            "statuses": effective_statuses,
            "instruction": instruction,
        }

        if selected_count == 0:
            _finalize_run(
                RUNS[run_id],
                "completed",
                "No pending leads matched the filters.",
            )
        else:
            ACTIVE_RUN_ID = run_id

        logger.info(
            "[OWL] Run reserved. run_id=%s "
            "available=%s considered=%s "
            "already_reviewed=%s selected=%s",
            run_id,
            available_count,
            considered_count,
            already_reviewed_skipped,
            selected_count,
        )

        return {
            "run_id": run_id,
            "lead_ids": selected_ids,
            "selected": selected_count,

            "in_range": available_count,
            "considered": considered_count,

            "already_reviewed_skipped": (
                already_reviewed_skipped
            ),

            "human_reviewed_skipped": (
                human_reviewed_skipped
            ),

            "previously_reviewed_selected": (
                previously_reviewed_count
            ),

            "human_reviewed": human_reviewed,

            "beyond_limit": beyond_limit,

            "effective_limit": effective_limit,
            "outside_range": 0,
            "statuses": effective_statuses,
        }


def unreviewed_query(
    db,
    start_date=None,
    end_date=None,
    statuses=None,
    lead_ids=None,
    exclude_ids=None,
):
    """Build the eligible-lead query.

    Normal runs exclude leads already reviewed by Owl. Explicit lead_ids
    permit an Owl rerun, but human reviewer decisions and legacy human
    validation values remain protected.
    """
    selected_statuses = statuses or DECISIONS
    query = db.query(LeadIntake)

    query = query.filter(LeadIntake.status.in_(selected_statuses))
    query = query.filter(LeadIntake.reviewer_decision.is_(None))

    # Only an explicit ID selection is allowed to rerun an existing Owl row.
    if lead_ids is None:
        query = query.filter(
            ~exists().where(LeadOwlReview.lead_id == LeadIntake.id)
        )

    legacy = getattr(LeadIntake, "agree_disagree", None)
    if legacy is not None:
        query = query.filter(legacy.is_(None))

    start_utc, end_utc = denver_date_bounds_utc(start_date, end_date)
    if start_utc is not None:
        query = query.filter(LeadIntake.received_at >= start_utc)
    if end_utc is not None:
        query = query.filter(LeadIntake.received_at < end_utc)

    if lead_ids is not None:
        query = query.filter(LeadIntake.id.in_(lead_ids))

    if exclude_ids:
        query = query.filter(~LeadIntake.id.in_(exclude_ids))

    logger.info(
        "[OWL] Eligible lead count=%s start=%s end=%s explicit_ids=%s",
        query.count(), start_date, end_date, lead_ids is not None,
    )
    return query


def build_owl_payload(lead, run_id=None):
    """Build the Owl request, including traceable lead/run identifiers."""
    first_name = (getattr(lead, "first_name", None) or "").strip()
    last_name = (getattr(lead, "last_name", None) or "").strip()
    lead_name = " ".join(part for part in (first_name, last_name) if part).strip()

    payload = {
        "lead_id": lead.id,
        "lead_name": lead_name or None,
        "run_id": run_id,
        "lead_text": build_classifier_summary(lead),
        "case_type": lead.case_type,
        "nafi_result": NAFI_TO_OWL.get(lead.nafi_tool_recommendation),
        "nafi_reason": lead.nafi_tool_reason,
        "received_date": (
            lead.received_at
            .replace(tzinfo=timezone.utc)
            .astimezone(MST)
            .date()
            .isoformat()
            if lead.received_at else None
        ),
        "state": lead.state_of_incident,
        "agent_decision": {
            "status": lead.status,
            "decline_type": (
                lead.decline_type if lead.status == "declined" else None
            ),
            "reason": lead.reviewer_note,
        },
    }
    return {key: value for key, value in payload.items() if value is not None}


async def call_owl(
    client: httpx.AsyncClient,
    payload: dict,
    lead_id=None,
):
    if not OWL_URL:
        raise RuntimeError(
            "OWL_API_URL is not configured."
        )

    if not OWL_KEY:
        raise RuntimeError(
            "INTAKEOPS_REVIEW_KEY is not configured."
        )

    headers = {
        "Authorization": f"Bearer {OWL_KEY}",
        "Content-Type": "application/json",
    }

    logger.info(
        "[OWL] Sending lead_id=%s to %s",
        lead_id,
        OWL_URL,
    )

    try:

        response = await client.post(
            OWL_URL,
            json=payload,
            headers=headers,
        )

        logger.info(
            "[OWL] lead_id=%s HTTP status=%s",
            lead_id,
            response.status_code,
        )

        if response.status_code == 401:
            raise OwlAuthError(
                "Owl API returned HTTP 401. "
                "Check INTAKEOPS_REVIEW_KEY."
            )

        # Retry rate limiting.
        if response.status_code == 429:

            logger.warning(
                "[OWL] Rate limited. Waiting 30 seconds. lead_id=%s",
                lead_id,
            )

            await asyncio.sleep(30)

            response = await client.post(
                OWL_URL,
                json=payload,
                headers=headers,
            )

            if response.status_code == 401:
                raise OwlAuthError(
                    "Owl API returned HTTP 401 on retry."
                )

        # Retry temporary gateway failures.
        retry_count = 0

        while (
            response.status_code in (502, 503, 504)
            and retry_count < MAX_502_RETRIES
        ):

            retry_count += 1

            logger.warning(
                "[OWL] Temporary HTTP %s. "
                "lead_id=%s Retry %s/%s",
                response.status_code,
                lead_id,
                retry_count,
                MAX_502_RETRIES,
            )

            await asyncio.sleep(
                5 * retry_count
            )

            response = await client.post(
                OWL_URL,
                json=payload,
                headers=headers,
            )

            if response.status_code == 401:
                raise OwlAuthError(
                    "Owl API returned HTTP 401 during retry."
                )

        if response.status_code != 200:

            logger.error(
                "[OWL] Request failed. lead_id=%s status=%s response=%s",
                lead_id,
                response.status_code,
                response.text[:1000],
            )

            return None

        try:
            result = response.json()

        except ValueError:

            logger.error(
                "[OWL] Invalid JSON response. lead_id=%s",
                lead_id,
            )

            return None

        if not isinstance(result, dict):

            logger.error(
                "[OWL] Expected JSON object. lead_id=%s",
                lead_id,
            )

            return None

        verdict = str(
            result.get("verdict", "")
        ).strip().lower()

        if verdict not in ("agree", "disagree"):

            logger.error(
                "[OWL] Invalid verdict. lead_id=%s verdict=%s",
                lead_id,
                verdict,
            )

            return None

        result["verdict"] = verdict

        logger.info(
            "[OWL] Valid response. lead_id=%s verdict=%s",
            lead_id,
            verdict,
        )

        return result

    except OwlAuthError:
        raise

    except httpx.RequestError:

        logger.exception(
            "[OWL] Network error. lead_id=%s",
            lead_id,
        )

        raise


def is_owl_run_stopped(run_id):

    with RUN_LOCK:

        run = RUNS.get(run_id)

        if not run:
            return True

        return bool(
            run["stop_requested"]
        )

async def run_owl_review(
    started_by: str | None,
    instruction=None,
    run_id: str | None = None,
    lead_ids=None,
):
    db = None

    run = RUNS.get(run_id)

    if not run:

        logger.error(
            "[OWL] Run not found. run_id=%s",
            run_id,
        )

        return

    with RUN_LOCK:

        if run["status"] not in ACTIVE_STATES:
            # Already finalized (e.g. stopped before it began).
            return

        if run["stop_requested"]:

            _finalize_run(
                run,
                "stopped",
                "Stopped before the run started.",
            )

            return

        run["status"] = "running"
        _touch(run)

    try:

        db = SessionLocal()

        # Database identity logging.
        db_url = db.get_bind().url

        logger.info(
            "[OWL] Connected database host=%s database=%s",
            db_url.host,
            db_url.database,
        )

        logger.info(
            "[OWL] Starting review. run_id=%s started_by=%s",
            run_id,
            started_by,
        )

        # Process only the IDs selected when the run was created.
        selected_leads = (
            db.query(LeadIntake)
            .filter(
                LeadIntake.id.in_(lead_ids or [])
            )
            .order_by(
                LeadIntake.received_at.desc(),
                LeadIntake.id.desc(),
            )
            .all()
        )

        listed = [
            (
                lead.id,
                lead.reviewed_at,
                lead.status,
                lead.decline_type,
            )
            for lead in selected_leads
        ]

        logger.info(
            "[OWL] Selected leads for run_id=%s: %s",
            run_id,
            len(listed),
        )

        legacy_exists = hasattr(
            LeadIntake,
            "agree_disagree",
        )

        if not listed:

            logger.info(
                "[OWL] No leads selected. run_id=%s",
                run_id,
            )

            return

        async with httpx.AsyncClient(timeout=60) as client:

            for (
                lead_id,
                version,
                status_then,
                decline_then,
            ) in listed:


                with RUN_LOCK:
                    _touch(run)

                if is_owl_run_stopped(run_id):

                    with RUN_LOCK:

                        run["status"] = "stopped"

                        run["stopped_reason"] = (
                            "Stopped by external request."
                        )

                    logger.warning(
                        "[OWL] Run stopped. run_id=%s completed=%s",
                        run_id,
                        run["done"],
                    )

                    return

                try:

                    lead = db.get(
                        LeadIntake,
                        lead_id,
                    )

                    if lead is None:

                        with RUN_LOCK:
                            run["skipped"] += 1
                            run["lead_results"].append({
                                "lead_id": lead_id,
                                "status": "skipped",
                                "writeback": "not_attempted",
                                "error": "Lead no longer exists.",
                            })

                        logger.warning(
                            "[OWL] Lead no longer exists. lead_id=%s",
                            lead_id,
                        )

                        continue


                    existing_review = (
                        db.query(LeadOwlReview)
                        .filter(
                            LeadOwlReview.lead_id == lead_id
                        )
                        .first()
                    )

                    # Existing Owl rows are updated after a successful rerun.

                    logger.info(
                        "[OWL] Processing run_id=%s lead_id=%s",
                        run_id,
                        lead_id,
                    )

                    payload = build_owl_payload(lead, run_id=run_id)

                    result = await call_owl(
                        client,
                        payload,
                        lead_id=lead_id,
                    )

                    with RUN_LOCK:
                        _touch(run)

                    if result is None:

                        with RUN_LOCK:

                            run["failed"] += 1

                            run["last_error"] = (
                                f"Lead {lead_id}: "
                                "Owl returned no valid result."
                            )
                            run["lead_results"].append({
                                "lead_id": lead_id,
                                "status": "failed",
                                "writeback": "not_attempted",
                                "error": "Owl returned no valid result.",
                            })

                        continue


                    db.refresh(lead)

                    human_decision_exists = (
                        lead.reviewer_decision is not None
                    )

                    legacy_decision_exists = (
                        legacy_exists
                        and lead.agree_disagree is not None
                    )

                    lead_changed = (
                        lead.reviewed_at != version
                        or lead.status != status_then
                        or lead.decline_type != decline_then
                    )

                    if (
                        human_decision_exists
                        or legacy_decision_exists
                        or lead_changed
                    ):

                        with RUN_LOCK:
                            run["skipped"] += 1
                            run["lead_results"].append({
                                "lead_id": lead_id,
                                "status": "skipped",
                                "writeback": "not_attempted",
                                "error": "Lead changed or a human decision exists.",
                            })

                        logger.warning(
                            "[OWL] Lead changed or human decision exists. "
                            "Skipping lead_id=%s",
                            lead_id,
                        )

                        continue

                    # -------------------------------------
                    # Save or update the raw Owl review.
                    # Human override and LeadIntake HIL fields are not changed.
                    # -------------------------------------
                    now = datetime.utcnow()

                    if existing_review is None:
                        review = LeadOwlReview(
                            lead_id=lead_id,
                            verdict=result["verdict"],
                            reason=result.get("reason"),
                            confidence=result.get("confidence"),
                            rules_version=result.get("rules_version"),
                            owl_decision=result.get("owl_decision"),
                            agent_status_at_review=status_then,
                            agent_decline_type_at_review=decline_then,
                            agent_version_at_review=current_prompt_version(),
                            lead_version=version,
                            started_by=started_by,
                        )
                        db.add(review)
                        writeback_action = "created"
                    else:
                        # Keep an audit trail of each Owl rerun.
                        history = list(existing_review.edit_history or [])
                        history.append({
                            "at": now.isoformat(),
                            "by": "owl_rerun",
                            "run_id": run_id,
                            "from": {
                                "verdict": existing_review.verdict,
                                "reason": existing_review.reason,
                                "confidence": existing_review.confidence,
                            },
                            "to": {
                                "verdict": result["verdict"],
                                "reason": result.get("reason"),
                                "confidence": result.get("confidence"),
                            },
                        })
                        existing_review.edit_history = history
                        existing_review.verdict = result["verdict"]
                        existing_review.reason = result.get("reason")
                        existing_review.confidence = result.get("confidence")
                        existing_review.rules_version = result.get("rules_version")
                        existing_review.owl_decision = result.get("owl_decision")
                        existing_review.agent_status_at_review = status_then
                        existing_review.agent_decline_type_at_review = decline_then
                        existing_review.agent_version_at_review = current_prompt_version()
                        existing_review.lead_version = version
                        existing_review.started_by = started_by
                        # Bump the existing review timestamp so it represents
                        # the latest successful Owl evaluation.
                        existing_review.created_at = now
                        # Deliberately preserve overridden / override_* fields.
                        review = existing_review
                        writeback_action = "updated"

                    try:
                        db.commit()
                        db.refresh(review)

                        lead_name = " ".join(
                            part for part in (
                                (getattr(lead, "first_name", None) or "").strip(),
                                (getattr(lead, "last_name", None) or "").strip(),
                            ) if part
                        ).strip()

                        with RUN_LOCK:
                            run["done"] += 1
                            run["lead_results"].append({
                                "lead_id": lead_id,
                                "lead_name": lead_name or None,
                                "status": "success",
                                "writeback": writeback_action,
                                "owl_verdict": result["verdict"],
                                "error": None,
                            })
                            _touch(run)

                        logger.info(
                            "[OWL] Review %s. run_id=%s lead_id=%s verdict=%s",
                            writeback_action, run_id, lead_id, result["verdict"],
                        )

                    except IntegrityError as exc:
                        db.rollback()
                        with RUN_LOCK:
                            run["failed"] += 1
                            run["last_error"] = f"Lead {lead_id}: database write-back conflict."
                            run["lead_results"].append({
                                "lead_id": lead_id,
                                "status": "failed",
                                "writeback": "failed",
                                "error": "Database write-back conflict.",
                            })
                        logger.exception(
                            "[OWL] Write-back conflict for lead_id=%s", lead_id
                        )

                except OwlAuthError as exc:

                    db.rollback()

                    with RUN_LOCK:
                        run["status"] = "failed"
                        run["stopped_reason"] = str(exc)
                        run["last_error"] = str(exc)
                        run["failed"] += 1
                        run["lead_results"].append({
                            "lead_id": lead_id,
                            "status": "failed",
                            "writeback": "not_attempted",
                            "error": str(exc),
                        })

                    logger.exception(
                        "[OWL] Authentication failure. "
                        "Stopping run_id=%s",
                        run_id,
                    )

                    return

                except Exception as exc:

                    db.rollback()

                    with RUN_LOCK:
                        run["failed"] += 1
                        run["last_error"] = f"Lead {lead_id}: {str(exc)}"
                        run["lead_results"].append({
                            "lead_id": lead_id,
                            "status": "failed",
                            "writeback": "failed",
                            "error": str(exc),
                        })

                    logger.exception(
                        "[OWL] Failed processing lead_id=%s",
                        lead_id,
                    )

                # Maintain existing request interval.
                await asyncio.sleep(
                    SECONDS_BETWEEN_CALLS
                )

    except asyncio.CancelledError:

        # Force-stop or auto-release cancelled the worker.
        with RUN_LOCK:

            if run["status"] in ACTIVE_STATES:
                run["status"] = (
                    "stopped" if run["stop_requested"] else "failed"
                )

            if not run["stopped_reason"]:
                run["stopped_reason"] = "Run was cancelled."

        logger.warning(
            "[OWL] Run cancelled. run_id=%s",
            run_id,
        )

        raise

    except Exception as exc:

        with RUN_LOCK:

            run["status"] = "failed"

            run["stopped_reason"] = (
                f"Run crashed: {exc}"
            )

            run["last_error"] = str(exc)

        logger.exception(
            "[OWL] Review run crashed. run_id=%s",
            run_id,
        )

    finally:

        if db is not None:
            db.close()

        with RUN_LOCK:

            # Do not overwrite stopped or failed status.
            if run["status"] in ("stopped", "failed"):
                final_status = run["status"]
            elif run["stop_requested"]:
                final_status = "stopped"
            else:
                final_status = "completed"

            _finalize_run(run, final_status)

            logger.info(
                "[OWL] Run finished. "
                "run_id=%s total=%s done=%s "
                "skipped=%s failed=%s status=%s",
                run_id,
                run["total"],
                run["done"],
                run["skipped"],
                run["failed"],
                run["status"],
            )


# ---------------------------------------------------------
# Stop Owl run
# ---------------------------------------------------------

def start_owl_run_task(
    run_id: str,
    started_by: str | None,
    instruction=None,
    lead_ids=None,
):

    async def _guarded():
        try:
            await run_owl_review(
                started_by=started_by,
                instruction=instruction,
                run_id=run_id,
                lead_ids=lead_ids,
            )
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[OWL] Worker crashed. run_id=%s", run_id)
        finally:
            # Absolute safety net: never leave a run active once the
            # worker has exited, whatever happened inside it.
            with RUN_LOCK:
                run = RUNS.get(run_id)
                if run and run["status"] in ACTIVE_STATES:
                    _finalize_run(
                        run,
                        "stopped" if run["stop_requested"] else "failed",
                        "Worker exited unexpectedly.",
                    )

    task = asyncio.get_running_loop().create_task(_guarded())

    with RUN_LOCK:
        _TASKS[run_id] = task

    return task


def request_stop_owl_run(run_id: str | None = None, force: bool = False):

    with RUN_LOCK:

        if run_id is None:
            run_id = ACTIVE_RUN_ID

        run = RUNS.get(run_id) if run_id else None

        if not run:
            return None

        status = run["status"]

        if status not in ACTIVE_STATES:
            return {
                "run_id": run_id,
                "status": status,
                "message": f"Run already finished with status '{status}'.",
            }

        if not run["stop_requested"]:
            run["stop_requested"] = True
            run["stop_requested_at"] = datetime.utcnow()

        alive = _task_alive(run_id)
        stuck = _stuck_reason(run)

        if force or not alive or stuck or status == "queued":

            if force:
                reason = "Force-stopped by external request."
            elif stuck:
                reason = f"Stopped by external request ({stuck})"
            else:
                reason = "Stopped by external request."

            _cancel_task(run_id)
            _finalize_run(run, "stopped", reason)

            logger.warning(
                "[OWL] Run stopped immediately. run_id=%s force=%s alive=%s",
                run_id,
                force,
                alive,
            )

            return {
                "run_id": run_id,
                "status": "stopped",
                "message": (
                    "Run stopped. New runs can start now. "
                    "Completed lead reviews were retained."
                ),
            }

        run["status"] = "stopping"

        logger.warning(
            "[OWL] Stop requested. run_id=%s",
            run_id,
        )

        return {
            "run_id": run_id,
            "status": "stopping",
            "message": (
                "Stop request received. The run will stop after the "
                "current lead. Completed lead reviews will be retained. "
                "Send force=true to stop immediately."
            ),
        }

def owl_run_status(run_id: str):
    with RUN_LOCK:

        run = RUNS.get(run_id)

        if not run:
            return None

        remaining = max(
            0,
            run["total"]
            - run["done"]
            - run["skipped"]
            - run["failed"],
        )

        return {
            "run_id": run["run_id"],

            "status": run["status"],

            "total": run["total"],

            "done": run["done"],

            "completed": run["done"],

            "remaining": remaining,

            "skipped": run["skipped"],

            "failed": run["failed"],

            "started_by": run["started_by"],

            "started_at": run["started_at"],

            "finished_at": run["finished_at"],

            "last_error": run["last_error"],

            "stopped_reason": run["stopped_reason"],

            "running": run["status"] in ACTIVE_STATES,

            "stop_requested": run["stop_requested"],

            "stop_requested_at": run["stop_requested_at"],

            "last_activity_at": run["last_activity_at"],

            "lead_results": list(run.get("lead_results", [])),
            "considered": run.get("considered", 0),

            "available": run.get("available", 0),
            "already_reviewed_skipped": (
                run.get("already_reviewed_skipped", 0)
            ),
            "human_reviewed_skipped": (
                run.get("human_reviewed_skipped", 0)
            ),
            "previously_reviewed_selected": (
                run.get("previously_reviewed_selected", 0)
            ),
            "human_reviewed": list(run.get("human_reviewed", [])),
            "beyond_limit": run.get("beyond_limit", 0),
        }


def owl_status(db):

    active_run = None

    with RUN_LOCK:

        # Never report a dead/stalled run as running.
        _reap_stuck_runs()

        active_id = ACTIVE_RUN_ID

    if active_id:

        active_run = owl_run_status(
            active_id
        )

    is_running = bool(
        active_run
        and active_run["status"] in ACTIVE_STATES
    )

    with RUN_LOCK:
        latest = max(
            RUNS.values(),
            key=lambda r: r["started_at"],
            default=None,
        )

    last_run = (
        owl_run_status(latest["run_id"])
        if latest else None
    )

    return {
        "running": is_running,

        "active_run": active_run,

        "last_run": last_run,

        "still_unreviewed": (
            unreviewed_query(db).count()
        ),

        "owl_marks_total": (
            db.query(
                func.count(LeadOwlReview.id)
            ).scalar()
        ),

        "total_runs": len(RUNS),
    }


def load_owl_reviews(db, lead_ids):

    if not lead_ids:
        return {}

    try:

        rows = (
            db.query(LeadOwlReview)
            .filter(
                LeadOwlReview.lead_id.in_(lead_ids)
            )
            .all()
        )

        return {
            row.lead_id: row
            for row in rows
        }

    except Exception:

        db.rollback()

        logger.exception(
            "[OWL] Could not load Owl reviews."
        )

        return {}


def user_email(current_user):

    if isinstance(current_user, dict):

        return (
            current_user.get("email")
            or current_user.get("username")
        )

    return None


def verify_edit_code(code, who: str | None):

    if not OWL_EDIT_CODE:

        raise HTTPException(
            status_code=500,
            detail="OWL_EDIT_CODE is not configured on the server.",
        )

    key = (who or "unknown").lower()

    now = time.time()

    fails = [
        timestamp
        for timestamp in _CODE_FAILS.get(key, [])
        if now - timestamp < FAIL_WINDOW_SECONDS
    ]

    if len(fails) >= MAX_CODE_FAILS:

        raise HTTPException(
            status_code=429,
            detail="Too many wrong codes. Try again in 15 minutes.",
        )

    if not hmac.compare_digest(
        str(code or ""),
        OWL_EDIT_CODE,
    ):

        fails.append(now)

        _CODE_FAILS[key] = fails

        raise HTTPException(
            status_code=403,
            detail="Incorrect code.",
        )

    _CODE_FAILS.pop(key, None)


def _effective(row):

    if row.overridden:

        return {
            "verdict": row.override_verdict,
            "reason": row.override_reason,
            "confidence": row.override_confidence,
        }

    return {
        "verdict": row.verdict,
        "reason": row.reason,
        "confidence": row.confidence,
    }


def apply_owl_override(
    db,
    lead_id: int,
    verdict,
    reason,
    confidence,
    who: str | None,
):

    row = (
        db.query(LeadOwlReview)
        .filter(
            LeadOwlReview.lead_id == lead_id
        )
        .first()
    )

    if not row:

        raise HTTPException(
            status_code=404,
            detail="Owl has not reviewed this lead yet.",
        )

    if not who:

        raise HTTPException(
            status_code=401,
            detail="Unable to identify the human reviewer.",
        )

    if (
        verdict is None
        and reason is None
        and confidence is None
    ):

        raise HTTPException(
            status_code=400,
            detail="Nothing to change.",
        )

    before = _effective(row)

    after = {
        "verdict": (
            verdict if verdict is not None
            else before["verdict"]
        ),

        "reason": (
            reason if reason is not None
            else before["reason"]
        ),

        "confidence": (
            confidence if confidence is not None
            else before["confidence"]
        ),
    }

    if after == before:

        raise HTTPException(
            status_code=400,
            detail="No change from the current values.",
        )

    now = datetime.utcnow()

    history = list(row.edit_history or [])

    history.append({
        "at": now.isoformat(),
        "by": who,
        "from": before,
        "to": after,
    })

    row.edit_history = history

    row.overridden = True

    row.override_verdict = after["verdict"]

    row.override_reason = after["reason"]

    row.override_confidence = after["confidence"]

    row.overridden_by = who

    row.overridden_at = now

    db.commit()

    db.refresh(row)

    logger.info(
        "[OWL] Human override saved permanently. "
        "lead_id=%s by=%s",
        lead_id,
        who,
    )

    return owl_fields(row)


def owl_fields(row):
    keys = (
        "owl_decision",
        "owl_verdict",
        "owl_reason",
        "owl_confidence",
        "owl_rules_version",
        "owl_reviewed_at",
        "owl_overridden",
        "owl_overridden_by",
        "owl_overridden_at",
        "owl_original_verdict",
        "owl_original_reason",
        "owl_original_confidence",
    )

    if row is None:
        return {key: None for key in keys}

    effective = _effective(row)

    return {
        "owl_decision": row.owl_decision,

        "owl_verdict": effective["verdict"],
        "owl_reason": effective["reason"],
        "owl_confidence": effective["confidence"],

        "owl_rules_version": row.rules_version,
        "owl_reviewed_at": row.created_at,

        "owl_overridden": bool(row.overridden),
        "owl_overridden_by": (
            row.overridden_by if row.overridden else None
        ),
        "owl_overridden_at": (
            row.overridden_at if row.overridden else None
        ),

        "owl_original_verdict": row.verdict,
        "owl_original_reason": row.reason,
        "owl_original_confidence": row.confidence,
    }


def _next_owl_run(now):
    """Return the next scheduled run datetime after `now` (Denver time)."""
    for day_offset in (0, 1):
        for t in sorted(OWL_RUN_TIMES):
            candidate = datetime.combine(
                now.date() + timedelta(days=day_offset),
                t,
                tzinfo=MST,
            )
            if candidate > now:
                return candidate


async def run_in_review_sweep():
    """
    Review every remaining lead: no human decision and no Owl review.
    No date range, no limit.

    Returns False if another run is active (caller may retry),
    otherwise True.
    """

    db = SessionLocal()

    try:
        instruction = "Scheduled review of all remaining In review leads"

        run_data = prepare_owl_run(
            db=db,
            requested_by="scheduler",
            start_date=None,
            end_date=None,
            statuses=None,         
            instruction=instruction,
            limit=None,
            lead_ids=None,
        )

        if run_data["selected"]:
            start_owl_run_task(
                run_id=run_data["run_id"],
                started_by="scheduler",
                instruction=instruction,
                lead_ids=run_data["lead_ids"],
            )

        logger.info(
            "[OWL] Scheduled run %s selected=%s",
            run_data["run_id"],
            run_data["selected"],
        )

        return True

    except RuntimeError as exc:
        # A run is already active.
        logger.warning("[OWL] Scheduled run postponed: %s", exc)
        return False

    except Exception:
        logger.exception("[OWL] Scheduled run failed")
        return True

    finally:
        db.close()


async def owl_daily_review():

    if not OWL_SCHEDULER_ENABLED:
        logger.info("[OWL] Scheduler disabled (OWL_SCHEDULER_ENABLED=false).")
        return

    while True:
        try:
            now = datetime.now(MST)
            next_run = _next_owl_run(now)

            logger.info(
                "[OWL] Next scheduled run at %s",
                next_run.isoformat(),
            )

            await asyncio.sleep(
                max(0, (next_run - now).total_seconds())
            )

            for attempt in range(OWL_BUSY_RETRIES + 1):

                started = await run_in_review_sweep()

                if started:
                    break

                if attempt < OWL_BUSY_RETRIES:
                    await asyncio.sleep(OWL_BUSY_RETRY_SECONDS)

            # Avoid re-triggering the same slot if the timer woke early.
            await asyncio.sleep(5)

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception("[OWL] Scheduler loop error")
            await asyncio.sleep(60)