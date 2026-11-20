"""HTTP client for an ad platform's insights API (Meta-shaped).

Handles what a production connector must, and reports each step through an
``on_event`` callback so a sync can show its progress:

- **Auth**: client-credentials token, cached; on 401 with Meta's code 190
  (expired or invalid) it gets a new token and retries once.
- **Retries**: 429 and 5xx, and network errors and timeouts, retried with
  exponential backoff and jitter, honoring ``Retry-After``; capped attempts and
  a cap on any single wait. 4xx other than 401/429 are not retried: they won't
  fix themselves.
- **Paging**: follows opaque ``paging.cursors.after`` until there's no next page.
- **Validation**: every row is parsed into a typed model (the API sends numbers
  as strings, conversions inside ``actions`` arrays). Rows that don't parse are
  counted and skipped with a reason, rather than failing the sync or being
  stored half-read.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

EventSink = Callable[[str, dict[str, Any]], None]
Sleep = Callable[[float], Awaitable[None]]

CONVERSION_ACTION = "purchase"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_s: float = 0.5
    max_wait_s: float = 8.0

    def backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self.max_wait_s)
        # Full jitter: spread retries so many clients don't retry in lockstep.
        return random.uniform(0, min(self.max_wait_s, self.base_s * 2 ** (attempt - 1)))


class PlatformAPIError(Exception):
    """A request the platform refused for good (or that ran out of retries)."""

    def __init__(self, message: str, status: int | None = None, code: int | None = None) -> None:
        super().__init__(message)
        self.status, self.code = status, code


class _Action(BaseModel):
    action_type: str
    value: str


class InsightRow(BaseModel):
    """One day in one market, as the connector stores it."""

    day: date = Field(alias="date_start")
    geo: str = Field(alias="region", min_length=1, max_length=120)
    spend: float
    conversions: float
    revenue: float

    @field_validator("spend", "conversions", "revenue", mode="before")
    @classmethod
    def _decimal_string(cls, v: object) -> float:
        try:
            d = Decimal(str(v))
        except InvalidOperation as e:
            raise ValueError(f"not a number: {v!r}") from e
        if not d.is_finite() or d < 0:
            raise ValueError(f"not a non-negative number: {v!r}")
        return float(d)

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> InsightRow:
        def action_total(key: str) -> str:
            items = [_Action.model_validate(a) for a in raw.get(key) or []]
            return str(
                sum(
                    (Decimal(a.value) for a in items if a.action_type == CONVERSION_ACTION),
                    Decimal(0),
                )
            )

        if raw.get("date_start") != raw.get("date_stop"):
            raise ValueError("expected one row per day (date_start == date_stop)")
        return cls.model_validate(
            {
                "date_start": raw.get("date_start"),
                "region": raw.get("region"),
                "spend": raw.get("spend"),
                "conversions": action_total("actions"),
                "revenue": action_total("action_values"),
            }
        )


class Page(BaseModel):
    number: int
    rows: list[InsightRow]
    rejected: list[str]  # reasons, one per skipped row


class AdsClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        account_id: str,
        client_id: str,
        client_secret: str,
        on_event: EventSink = lambda kind, data: None,
        retry: RetryPolicy | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = http
        self._account = account_id
        self._client_id, self._client_secret = client_id, client_secret
        self._emit = on_event
        self._retry = retry or RetryPolicy()
        self._sleep = sleep
        self._token: str | None = None
        self.requests = 0
        self.retries = 0

    async def _authenticate(self, reason: str) -> None:
        resp = await self._send(
            "POST",
            "/oauth/access_token",
            auth=False,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "account_id": self._account,
            },
        )
        body = resp.json()
        self._token = body["access_token"]
        self._emit("auth", {"reason": reason, "expires_in": body.get("expires_in")})

    async def _send(
        self, method: str, url: str, auth: bool = True, **kwargs: Any
    ) -> httpx.Response:
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            headers = {"Authorization": f"Bearer {self._token}"} if auth and self._token else {}
            self.requests += 1
            try:
                resp = await self._http.request(method, url, headers=headers, **kwargs)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if attempt >= self._retry.max_attempts:
                    raise PlatformAPIError(f"Network error after {attempt} attempts: {e}") from e
                await self._wait(attempt, None, "network error", type(e).__name__)
                continue
            if resp.status_code < 400:
                return resp
            error = _error_body(resp)
            if resp.status_code == 401 and auth and error.get("code") == 190 and not refreshed:
                refreshed = True
                await self._authenticate(str(error.get("message") or "token rejected"))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= self._retry.max_attempts:
                    raise PlatformAPIError(
                        f"Gave up after {attempt} attempts: {error.get('message')}",
                        resp.status_code,
                        error.get("code"),
                    )
                retry_after = _retry_after(resp)
                await self._wait(
                    attempt,
                    retry_after,
                    str(resp.status_code),
                    str(error.get("message") or resp.reason_phrase),
                )
                continue
            raise PlatformAPIError(
                str(error.get("message") or f"HTTP {resp.status_code}"),
                resp.status_code,
                error.get("code"),
            )

    async def _wait(self, attempt: int, retry_after: float | None, status: str, why: str) -> None:
        wait = self._retry.backoff(attempt, retry_after)
        self.retries += 1
        self._emit(
            "retry",
            {
                "status": status,
                "reason": why,
                "attempt": attempt,
                "wait_s": round(wait, 2),
                "retry_after": retry_after is not None,
            },
        )
        await self._sleep(wait)

    async def account(self) -> dict[str, Any]:
        if self._token is None:
            await self._authenticate("first request")
        resp = await self._send("GET", f"/v1/{self._account}")
        data: dict[str, Any] = resp.json()
        return data

    async def insights(self, since: date, until: date, page_size: int = 100) -> AsyncIterator[Page]:
        """Every row for ``since``..``until``, a page at a time."""
        if self._token is None:
            await self._authenticate("first request")
        params: dict[str, str | int] = {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "limit": page_size,
        }
        number = 0
        while True:
            number += 1
            resp = await self._send("GET", f"/v1/{self._account}/insights", params=params)
            body = resp.json()
            rows: list[InsightRow] = []
            rejected: list[str] = []
            for raw in body.get("data") or []:
                try:
                    rows.append(InsightRow.parse(raw))
                except (ValidationError, ValueError, InvalidOperation) as e:
                    rejected.append(_reason(e))
            yield Page(number=number, rows=rows, rejected=rejected)
            after = ((body.get("paging") or {}).get("cursors") or {}).get("after")
            if not after or not (body.get("paging") or {}).get("next"):
                return
            params["after"] = after


def _error_body(resp: httpx.Response) -> dict[str, Any]:
    try:
        error = resp.json().get("error")
    except ValueError:
        return {}
    return error if isinstance(error, dict) else {}


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None


def _reason(e: Exception) -> str:
    if isinstance(e, ValidationError):
        first = e.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "row"
        return f"{where}: {first['msg']}"
    return str(e)
