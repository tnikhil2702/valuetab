"""
graph_sync.py — Microsoft Graph calendar sync for DeadlineOps.
"""
from __future__ import annotations
import os
import logging
import asyncio
from datetime import date, timedelta, datetime
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TENANT_ID = os.getenv("Tenant_ID")
CLIENT_ID = os.getenv("Application_ID")
CLIENT_SECRET = os.getenv("Secret_Value")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"

# COLOR_CATEGORY_MAP = {
#     "red": "DeadlineOps - Critical",
#     "orange": "DeadlineOps - Discovery",
#     "yellow": "DeadlineOps - Motions",
#     "blue": "DeadlineOps - Trial Prep",
#     "green": "DeadlineOps - General",
# }

COLOR_CATEGORY_MAP = {
    "red": "Red category",
    "orange": "Orange category",
    "yellow": "Yellow category",
    "blue": "Blue category",
    "green": "Green category",
}

MATTER_TAG_PROPERTY = "String {00020329-0000-0000-C000-000000000046} Name DeadlineOpsMatterTag"

# Token refresh buffer (refresh 5 minutes before expiry)
TOKEN_REFRESH_BUFFER_SECONDS = 300

_graph_client = httpx.AsyncClient(
    timeout=30.0,
    http2=True,
    limits=httpx.Limits(
        max_connections=100,
        max_keepalive_connections=20,
    ),
)


# ─── Token Management with Expiration Handling ─────────
class TokenManager:
    """
    Manages OAuth2 access tokens with automatic refresh and expiration handling.
    """
    
    def __init__(self):
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()
    
    def _is_token_valid(self) -> bool:
        """Check if current token is valid and not expired."""
        import time
        if not self._token:
            return False
        # Check if token expires within the buffer period
        return time.time() < (self._expires_at - TOKEN_REFRESH_BUFFER_SECONDS)
    
    async def get_access_token(self, force_refresh: bool = False) -> str:
        """
        Get a valid access token, refreshing if necessary.
        """
        async with self._lock:
            # Return cached token if valid and not forcing refresh
            if not force_refresh and self._is_token_valid():
                return self._token
            
            return await self._refresh_token()
    
    async def _refresh_token(self) -> str:
        """
        Refresh the access token.
        """
        import time
        
        logger.info("Refreshing Microsoft Graph access token...")
        
        try:
            # async with httpx.AsyncClient(timeout=30.0) as client:
            response = await _graph_client.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
            response.raise_for_status()
                
            body = response.json()
            self._token = body["access_token"]
            expires_in = body.get("expires_in", 3600)
            self._expires_at = time.time() + expires_in
            
            logger.info(f"Token refreshed successfully. Expires in {expires_in} seconds.")
            return self._token
            
        except httpx.HTTPStatusError as exc:
            logger.error(
                f"Token refresh failed with status {exc.response.status_code}: {exc.response.text}"
            )
            raise
        except Exception as exc:
            logger.error(f"Unexpected error during token refresh: {exc}")
            raise
    
    async def invalidate_token(self):
        """Invalidate the current token, forcing a refresh on next request."""
        async with self._lock:
            logger.warning("Token invalidated - will refresh on next request")
            self._token = None
            self._expires_at = 0.0


# Global token manager instance
_token_manager = TokenManager()


async def _get_headers() -> Dict[str, str]:
    """Get headers with valid access token."""
    token = await _token_manager.get_access_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


@asynccontextmanager
async def _handle_token_expiration():
    """
    Context manager to handle token expiration errors and retry with fresh token.
    """
    try:
        yield
    except httpx.HTTPStatusError as exc:
        # Handle 401 Unauthorized - token may have expired
        if exc.response.status_code == 401:
            logger.warning("Received 401 Unauthorized - attempting token refresh")
            
            # Check if error is specifically about token expiration
            error_body = exc.response.text.lower()
            if "token" in error_body or "expired" in error_body or "unauthorized" in error_body:
                # Invalidate current token and let caller retry
                await _token_manager.invalidate_token()
                raise TokenExpiredError("Access token expired, please retry the operation") from exc
        
        # Re-raise other HTTP errors
        raise


class TokenExpiredError(Exception):
    """Exception raised when access token has expired."""
    pass


# ─── Event Body Builder ────────
async def _build_event_body(
    label: str,
    due_date: date,
    reminder_dates: List[str],
    color: str,
    notes: str,
    matter_id: str,
    case_name: str,
    rule_id: str,
) -> Dict[str, Any]:
    """Build Microsoft Graph event body."""
    start_str = due_date.isoformat()
    end_str = (due_date + timedelta(days=1)).isoformat()

    reminder_minutes: List[int] = []
    for r in reminder_dates:
        r_date = date.fromisoformat(r)
        delta = due_date - r_date
        if delta.days > 0:
            reminder_minutes.append(delta.days * 24 * 60)

    earliest_reminder = max(reminder_minutes) if reminder_minutes else 1440

    category = COLOR_CATEGORY_MAP.get(color, "DeadlineOps - General")
    tag_value = f"{rule_id}:{due_date.isoformat()}"

    return {
        # "subject": f"[DeadlineOps] {label}",
        "subject": f"{case_name} • {label}",
        "body": {
            "contentType": "text",
            "content": (
                f"Case: {case_name}\n"
                f"Deadline: {label}\n"
                f"Rule ID: {rule_id}\n"
                f"Due Date: {due_date.isoformat()}\n"
                f"Reminder Dates: {', '.join(reminder_dates) if reminder_dates else 'None'}\n\n"
                f"Notes: {notes}\n\n"
                f"This event was created automatically by DeadlineOps."
            ),
        },
        "start": {"dateTime": f"{start_str}T00:00:00", "timeZone": "UTC"},
        "end": {"dateTime": f"{end_str}T00:00:00", "timeZone": "UTC"},
        "isAllDay": True,
        "isReminderOn": True,
        "reminderMinutesBeforeStart": earliest_reminder,
        "categories": [category],
        "showAs": "free",
        "singleValueExtendedProperties": [
            {"id": MATTER_TAG_PROPERTY, "value": tag_value}
        ],
    }


# ─── Core Async Sync Functions ────────────────────────────────────────────────

async def sync_deadline_to_calendar(
    attorney_email: str,
    label: str,
    due_date: date,
    reminder_dates: List[str],
    color: str,
    notes: str,
    matter_id: str,
    case_name: str,
    rule_id: str,
    existing_graph_event_id: Optional[str] = None,
    max_retries: int = 2,
) -> str:
    """
    Sync a deadline to an attorney's calendar with automatic retry on token expiration.
    
    Args:
        attorney_email: Email of the attorney
        label: Deadline label
        due_date: Due date
        reminder_dates: List of reminder dates
        color: Color category
        notes: Additional notes
        matter_id: Matter ID
        rule_id: Rule ID
        existing_graph_event_id: Existing event ID for updates
        max_retries: Maximum retry attempts on token expiration
        
    Returns:
        Graph event ID
        
    Raises:
        httpx.HTTPStatusError: If API call fails after retries
    """
    event_body = await _build_event_body(
        label=label,
        due_date=due_date,
        reminder_dates=reminder_dates,
        color=color,
        notes=notes,
        matter_id=matter_id,
        rule_id=rule_id,
        case_name=case_name,
    )

    for attempt in range(max_retries + 1):
        try:
            async with _handle_token_expiration():
                headers = await _get_headers()
                
                # async with httpx.AsyncClient(timeout=30.0) as client:
                    # Update existing event
                if existing_graph_event_id:
                    url = f"{GRAPH_BASE}/users/{attorney_email}/events/{existing_graph_event_id}"
                    resp = await _graph_client.patch(url, headers=headers, json=event_body)

                    if resp.status_code == 404:
                        logger.warning(
                            f"Graph event {existing_graph_event_id} not found for {attorney_email}, will recreate."
                        )
                        # Fall through to create new event
                    else:
                        resp.raise_for_status()
                        logger.info(
                            f"Updated Graph event {existing_graph_event_id} for {attorney_email} — {label}"
                        )
                        return existing_graph_event_id

                # Create new event
                url = f"{GRAPH_BASE}/users/{attorney_email}/events"
                resp = await _graph_client.post(url, headers=headers, json=event_body)
                resp.raise_for_status()

                graph_event_id = resp.json()["id"]
                logger.info(
                    f"Created Graph event {graph_event_id} for {attorney_email} — {label} on {due_date}"
                )
                return graph_event_id
                    
        except TokenExpiredError:
            if attempt < max_retries:
                logger.info(f"Token expired, retrying ({attempt + 1}/{max_retries})...")
                await asyncio.sleep(1)  # Brief delay before retry
                continue
            else:
                logger.error(f"Failed after {max_retries} retries due to token expiration")
                raise
        except httpx.HTTPStatusError as exc:
            logger.error(
                f"HTTP error syncing deadline for {attorney_email}: "
                f"{exc.response.status_code} - {exc.response.text}"
            )
            raise
    
    raise Exception("Unexpected: exceeded max retries without raising exception")


async def delete_graph_event(
    attorney_email: str,
    graph_event_id: str,
    max_retries: int = 2,
) -> None:
    """
    Delete a calendar event with automatic retry on token expiration.
    
    Args:
        attorney_email: Email of the attorney
        graph_event_id: Graph event ID to delete
        max_retries: Maximum retry attempts on token expiration
    """
    for attempt in range(max_retries + 1):
        try:
            async with _handle_token_expiration():
                headers = await _get_headers()
                url = f"{GRAPH_BASE}/users/{attorney_email}/events/{graph_event_id}"
                
                # async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await _graph_client.delete(url, headers=headers)

                if resp.status_code == 404:
                    logger.warning(f"Graph event {graph_event_id} already gone for {attorney_email}")
                    return

                resp.raise_for_status()
                logger.info(f"Deleted Graph event {graph_event_id} for {attorney_email}")
                return
                    
        except TokenExpiredError:
            if attempt < max_retries:
                logger.info(f"Token expired, retrying deletion ({attempt + 1}/{max_retries})...")
                await asyncio.sleep(1)
                continue
            else:
                logger.error(f"Failed to delete event after {max_retries} retries")
                print("Failed to delete event after retries............")
                raise


async def verify_calendar_event(
    attorney_email: str,
    graph_event_id: str,
    max_retries: int = 2,
) -> Dict[str, Any]:
    """
    Verify that a Graph calendar event exists with automatic retry on token expiration.
    
    Args:
        attorney_email: Email of the attorney
        graph_event_id: Graph event ID to verify
        max_retries: Maximum retry attempts on token expiration
        
    Returns:
        Dict with event details or {"exists": False}
    """
    for attempt in range(max_retries + 1):
        try:
            async with _handle_token_expiration():
                headers = await _get_headers()
                url = f"{GRAPH_BASE}/users/{attorney_email}/events/{graph_event_id}"
                params = {"$select": "id,subject,start,end,categories"}
                
                # async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await _graph_client.get(url, headers=headers, params=params)
                
                if resp.status_code == 404:
                    return {"exists": False}
                
                resp.raise_for_status()
                body = resp.json()
                
                return {
                    "exists": True,
                    "subject": body.get("subject"),
                    "start": body.get("start", {}).get("dateTime"),
                    "end": body.get("end", {}).get("dateTime"),
                    "categories": body.get("categories", []),
                }
                    
        except TokenExpiredError:
            if attempt < max_retries:
                logger.info(f"Token expired, retrying verification ({attempt + 1}/{max_retries})...")
                await asyncio.sleep(1)
                continue
            else:
                logger.error(f"Failed to verify event after {max_retries} retries")
                return {"exists": False, "error": "Token refresh failed"}
        except httpx.HTTPStatusError as exc:
            logger.error(
                f"verify_calendar_event HTTP error for {attorney_email} / {graph_event_id}: {exc}"
            )
            return {"exists": False, "error": str(exc)}
    
    return {"exists": False, "error": "Max retries exceeded"}


async def sync_matter_deadlines(
    db,
    matter_id: int,
    deadline_ids: List[int],
    attorney_emails: List[str],
) -> Dict[str, Any]:
    """
    Async sync specified deadlines to attorney calendars with concurrent processing.
    
    Args:
        db: Database session
        matter_id: Matter ID
        deadline_ids: List of deadline IDs to sync
        attorney_emails: List of attorney emails
        
    Returns:
        Dict with sync results and statistics
    """
    from db import ComputedDeadline as DeadlineDBRow, DeadlineAttorney

    # Fetch all deadlines
    deadlines = (
        db.query(DeadlineDBRow)
        .filter(
            DeadlineDBRow.id.in_(deadline_ids),
            DeadlineDBRow.matter_id == matter_id
        )
        .all()
    )

    existing_links = (
        db.query(DeadlineAttorney)
        .join(DeadlineDBRow)
        .filter(
            DeadlineDBRow.matter_id == matter_id,
            DeadlineAttorney.deadline_id.in_(deadline_ids),
            DeadlineAttorney.attorney_email.in_(attorney_emails)
        )
        .all()
    )

    existing_link_map = {
        (link.deadline_id, link.attorney_email): link
        for link in existing_links
    }

    if not deadlines:
        logger.warning(f"No deadlines found for matter {matter_id} with IDs {deadline_ids}")
        return {"created": 0, "updated": 0, "failed": 0, "results": {}}

    results = {}
    total_created = 0
    total_updated = 0
    total_failed = 0

    # Process each deadline
    for db_row in deadlines:
        deadline_result = {
            "deadline_id": db_row.id,
            "rule_id": db_row.rule_id,
            "attorneys": {}
        }

        reminder_dates = db_row.reminders_days if db_row.reminders_days else []

        # Create tasks for concurrent processing of all attorneys
        async def sync_for_attorney(email: str, db_row=db_row, reminder_dates=reminder_dates):
            try:
                # Check for existing sync record
                existing_link = existing_link_map.get((db_row.id, email))

                existing_graph_event_id = existing_link.graph_event_id if existing_link else None
                was_existing = bool(existing_graph_event_id)

                # Sync to calendar
                graph_id = await sync_deadline_to_calendar(
                    attorney_email=email,
                    label=db_row.label,
                    due_date=db_row.due_date,
                    reminder_dates=reminder_dates,
                    color=db_row.color,
                    notes=db_row.notes or "",
                    matter_id=str(matter_id),
                    case_name=db_row.matter.case_name,
                    rule_id=db_row.rule_id,
                    existing_graph_event_id=existing_graph_event_id,
                )

                return {
                    "email": email,
                    "success": True,
                    "graph_event_id": graph_id,
                    "was_existing": was_existing,
                    "deadline_id": db_row.id,
                }

            except Exception as exc:
                error_msg = str(exc)
                logger.error(f"Sync failed for {email} / {db_row.rule_id}: {error_msg}")
                return {
                    "email": email,
                    "success": False,
                    "error": error_msg
                }

        # Execute all attorney syncs concurrently
        attorney_tasks = [sync_for_attorney(email) for email in attorney_emails]
        attorney_results = await asyncio.gather(*attorney_tasks, return_exceptions=True)

        # Process results
        for result in attorney_results:
            if isinstance(result, Exception):
                email = "unknown"
                deadline_result["attorneys"][email] = {
                    "success": False,
                    "error": str(result)
                }
                total_failed += 1
            elif result["success"]:
                existing_link = existing_link_map.get(
                    (result["deadline_id"], result["email"])
                )
                if existing_link:
                    existing_link.graph_event_id = result["graph_event_id"]
                    existing_link.synced_at = datetime.utcnow()
                    db.add(existing_link)
                else:
                    db.add(
                        DeadlineAttorney(
                            deadline_id=result["deadline_id"],
                            attorney_email=result["email"],
                            graph_event_id=result["graph_event_id"],
                        )
                    )
                deadline_result["attorneys"][result["email"]] = {
                    "success": True,
                    "graph_event_id": result["graph_event_id"]
                }
                if result["was_existing"]:
                    total_updated += 1
                else:
                    total_created += 1
            else:
                deadline_result["attorneys"][result["email"]] = {
                    "success": False,
                    "error": result.get("error", "Unknown error")
                }
                total_failed += 1

        results[db_row.id] = deadline_result

    db.commit()
    return {
        "created": total_created,
        "updated": total_updated,
        "failed": total_failed,
        "results": results
    }


async def verify_graph_connection(attorney_email: str) -> Dict[str, Any]:
    """
    Verifies if Microsoft Graph connection is working for the given user.
    """
    try:
        headers = await _get_headers()

        # Lightweight Graph call, best option for app-only auth
        url = f"{GRAPH_BASE}/users/{attorney_email}/calendar"
        resp = await _graph_client.get(url, headers=headers)
        if resp.status_code == 200:
            return {
                "connected": True,
                "email": attorney_email,
                "status_code": 200,
                "error": None
            }
        return {
            "connected": False,
            "email": attorney_email,
            "status_code": resp.status_code,
            "error": resp.text
        }
    except Exception as exc:
        return {
            "connected": False,
            "email": attorney_email,
            "status_code": 500,
            "error": str(exc)
        }