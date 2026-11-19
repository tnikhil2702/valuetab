from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Iterable

import httpx

BASE_URL_DEFAULT = "https://nc-api.neos-cloud.com"
REQUEST_TIMEOUT_S = 20.0
MAX_RETRIES = 2
BACKOFF_S = (0.5, 1.5)           # delay before retry 1 and retry 2
ASSUMED_TOKEN_LIFETIME_S = 12 * 3600   # plan says roughly 12 h
RENEW_MARGIN_S = 15 * 60               # renew when < 15 min left

log = logging.getLogger("peaktrac.neos")

ALLOWED: tuple[tuple[str, str], ...] = (
    ("POST", "/advancedSearch/search/results"),
    ("GET", "/cases/v2/{caseId}"),
    ("GET", "/cases/v2/{caseId}/staffAssignments"),
    ("GET", "/cases/{caseId}/phases"),
    ("GET", "/cases/v2/{caseId}/caseDates"),
    ("GET", "/neos/cases/{caseId}/lists/insurance"),
    ("GET", "/timesandvalues"),
    ("GET", "/valueTransactions"),
    ("GET", "/cases/{caseId}/settlement/summary"),
    ("GET", "/cases/{caseId}/value_items/settlement"),
    ("GET", "/v2/utbmsexpensecodes"),
    ("GET", "/valuecode/types"),
    ("POST", "/refreshtoken"),
)

_SEGMENT = r"[A-Za-z0-9_-]+"


def _compile(template: str) -> re.Pattern:
    pattern = re.sub(r"\\\{\w+\\\}", _SEGMENT, re.escape(template))
    return re.compile(f"^{pattern}$")


_COMPILED = tuple((m, t, _compile(t)) for m, t in ALLOWED)


class NeosBlockedError(Exception):
    """Raised before any network call when a method + path is not allowlisted."""


class NeosAuthError(Exception):
    """Token missing, renewal failed, or Neos refused the credentials."""


class NeosRequestError(Exception):
    """Neos answered with an error status, or the network failed after retries."""

    def __init__(self, method: str, template: str, status: int | None):
        self.method, self.template, self.status = method, template, status
        super().__init__(f"Neos {method} {template} failed (status={status})")


def match_allowed(method: str, path: str) -> str:
    """Return the path template if allowed, else raise NeosBlockedError."""
    m = method.upper()
    if (
        not path.startswith("/")
        or "?" in path or "#" in path or "%" in path or "\\" in path
    ):
        raise NeosBlockedError(f"Blocked malformed Neos path: {m}")
    for allowed_method, template, rx in _COMPILED:
        if m == allowed_method and rx.match(path):
            return template
    raise NeosBlockedError(f"Blocked Neos call (not on read-only allowlist): {m} {path}")


# ---------------------------------------------------------------- token helpers
def _jwt_exp(token: str) -> float | None:
    """Read the exp claim WITHOUT verifying (we only use it to schedule renewal)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return None


def _find_key(data: Any, name: str) -> Any:
    """Case-insensitive lookup, top level or one level down (e.g. under Data)."""
    if not isinstance(data, dict):
        return None
    for k, v in data.items():
        if k.lower() == name.lower():
            return v
    for v in data.values():
        if isinstance(v, dict):
            for k, vv in v.items():
                if k.lower() == name.lower():
                    return vv
    return None


TokenCallback = Callable[[str, str], Awaitable[None] | None]


class NeosClient:
    def __init__(
        self,
        access_token: str | None = None,
        refresh_token: str | None = None,
        *,
        token_provider: Callable[[bool], Awaitable[str] | str] | None = None,
        base_url: str = BASE_URL_DEFAULT,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
        on_tokens_renewed: TokenCallback | None = None,
    ):
        if not token_provider and not (access_token and refresh_token):
            raise NeosAuthError("Provide token_provider, or both access and refresh tokens.")
        self._provider = token_provider
        self._access = access_token or ""
        self._refresh_tok = refresh_token or ""
        self._sleep = sleep
        self._clock = clock
        self._on_renewed = on_tokens_renewed   # persist rotated tokens (Key Vault)
        self._lock = asyncio.Lock()
        self._expires_at = self._expiry_for(access_token) if access_token else 0.0
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=REQUEST_TIMEOUT_S, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ public API
    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def advanced_search(self, body: dict) -> Any:
        return await self.request("POST", "/advancedSearch/search/results", json_body=body)

    async def search_all(self, body: dict, *, max_rows: int = 20000) -> list[dict]:
        """Page a search with Skip += Take until Skip >= TotalResultsCount."""
        body = {**body, "GetTotalResultsCount": True}
        take = int(body.get("Take") or 25)
        skip = int(body.get("Skip") or 0)
        rows: list[dict] = []
        while True:
            data = await self.advanced_search({**body, "Take": take, "Skip": skip})
            page = _find_key(data, "Results") or []
            total = int(_find_key(data, "TotalResultsCount") or 0)
            rows.extend(page)
            skip += take
            if not page or skip >= total or len(rows) >= max_rows:
                return rows

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> Any:
        template = match_allowed(method, path)        # blocks BEFORE any network call
        if template == "/refreshtoken":
            raise NeosBlockedError("Token renewal is internal; not callable directly.")

        renewed_after_401 = False
        while True:
            await self._renew_if_needed()
            used = self._access
            resp = await self._send(method, template, path, params, json_body, used)
            if resp.status_code == 401 and not renewed_after_401:
                renewed_after_401 = True          # the one allowed replay of a 4xx
                await self._renew(stale=used)
                continue
            if resp.status_code >= 400:
                raise NeosRequestError(method.upper(), template, resp.status_code)
            return resp.json() if resp.content else None

    # ------------------------------------------------------------ internals
    def _expiry_for(self, token: str) -> float:
        return _jwt_exp(token) or (self._clock() + ASSUMED_TOKEN_LIFETIME_S)

    async def _renew_if_needed(self) -> None:
        if self._clock() >= self._expires_at - RENEW_MARGIN_S:
            await self._renew(stale=self._access, force=bool(self._access))

    async def _renew(self, *, stale: str, force: bool = True) -> None:
        async with self._lock:
            if self._access != stale:      # another task already renewed
                return
            if self._provider:
                token = self._provider(force)
                if asyncio.iscoroutine(token):
                    token = await token
                if not token:
                    raise NeosAuthError("Token provider returned no token.")
                self._access = token
                self._expires_at = self._expiry_for(token)
                return
            resp = await self._send(
                "POST", "/refreshtoken", "/refreshtoken", None,
                {"RefreshToken": self._refresh_tok, "AccessToken": self._access},
                token=None,
            )
            if resp.status_code >= 400:
                raise NeosAuthError(f"Neos token renewal failed (status={resp.status_code}).")
            data = resp.json()
            new_access = _find_key(data, "AccessToken")
            new_refresh = _find_key(data, "RefreshToken") or self._refresh_tok
            if not new_access:
                raise NeosAuthError("Neos token renewal returned no access token.")
            self._access, self._refresh_tok = new_access, new_refresh
            self._expires_at = self._expiry_for(new_access)
            if self._on_renewed:
                result = self._on_renewed(new_access, new_refresh)
                if asyncio.iscoroutine(result):
                    await result

    async def _send(
        self, method: str, template: str, path: str,
        params: dict | None, json_body: dict | None, token: str | None,
    ) -> httpx.Response:
        """One logical call: retries on timeout/5xx only. Never raises on 4xx."""
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        attempt = 0
        while True:
            start = time.monotonic()
            status: int | None = None
            try:
                resp = await self._http.request(
                    method.upper(), path, params=params, json=json_body, headers=headers
                )
                status = resp.status_code
            except httpx.TransportError:          # includes timeouts
                resp = None
            self._log(method, template, status, start)
            if resp is not None and (status < 500 or attempt >= MAX_RETRIES):
                return resp
            if resp is None and attempt >= MAX_RETRIES:
                raise NeosRequestError(method.upper(), template, None) from None
            await self._sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
            attempt += 1

    @staticmethod
    def _log(method: str, template: str, status: int | None, start: float) -> None:
        log.info(
            "neos %s %s status=%s ms=%d",
            method.upper(), template, status, int((time.monotonic() - start) * 1000),
        )


# ---------------------------------------------------------------- search builders
DEFAULT_COLUMNS = [
    "CaseNumber", "CaseType_Code", "DateOpened", "IsClosed", "DateClosed",
    "CaseDetails_CurrentStatus_Name", "StaffAssigned[Lead Attorney]",
    "UserCaseData[Office Location]", "LimitationDate",
]


def query_filter(binding: str, value: Any, operator: str = "Equal") -> dict:
    """One filter element, shaped exactly like Appendix B."""
    return {
        "QueryOperatorString": operator, "QueryConjunctionString": "And",
        "QueryElements": [], "EntityName": "Case",
        "PropertyBinding": binding, "FilterValue": value,
    }


def case_search_body(
    filters: Iterable[dict] = (), *, take: int = 25, skip: int = 0,
    columns: Iterable[str] = DEFAULT_COLUMNS,
    order_by: str = "DateOpened", descending: bool = True,
) -> dict:
    """Appendix B request body."""
    return {
        "SearchObject": "Case", "PrimaryRecordTypeId": None,
        "SearchQuery": {"EntityName": "Case", "QueryElements": list(filters)},
        "Columns": list(columns), "Aggregations": [], "SummaryFields": [],
        "CalculatedFields": [], "OrderByBinding": order_by, "Descending": descending,
        "Take": take, "Skip": skip, "GetTotalResultsCount": True, "GetSummary": False,
    }