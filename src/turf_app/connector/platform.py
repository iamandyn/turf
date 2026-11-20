"""A simulated ad platform: an insights API shaped like Meta's Marketing API.

It stands in for a real ad account so the connector can be demonstrated
without one (or any API spend). It is a separate ASGI app, mounted at
``/mock/ads`` for anyone to look at, and reached by the connector through
httpx's ASGI transport: a real HTTP exchange (status codes, headers, JSON),
just without a network hop.

What it simulates, because a production connector has to handle each:

- **OAuth**: ``POST /oauth/access_token`` with the app's client credentials
  returns a short-lived bearer token for one account (stateless, HMAC-signed,
  so every worker accepts every other worker's tokens). Expired or invalid
  tokens get Meta's error code 190.
- **Insights by day and market**, like Meta's DMA breakdown: numbers as
  strings, conversions as ``actions`` / ``action_values`` arrays, plus an
  ``Unknown`` market row each day (Meta reports spend it can't place).
- **Cursor paging** with opaque cursors tied to the query.
- **Rate limits and outages**: the first request for a page is sometimes
  refused (429 with Retry-After and Meta's code 17, or a transient 503); a
  retry succeeds.
- **Late-arriving conversions**: the last three days are still filling in
  (attribution windows), so re-fetching them returns higher numbers. A
  connector that doesn't re-fetch them under-reports recent days forever.
- **Time**: each account has a simulated "today". ``POST
  /_demo/accounts/{id}/advance`` moves it forward, so a demo can show
  incremental syncs without waiting days. (``_demo`` routes are demo
  controls, not part of the simulated API.)

The numbers come from the sample brands (``samples.metro_totals``): each
metro's monthly totals, spread over days with weekday shape, noise, and a slow
per-metro trend, so the report shifts a little as days arrive. Deterministic:
the same account, day, and "today" always give the same rows.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import random
import secrets
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Form, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from turf_app.samples import SAMPLES, MetroTotal, Sample

HISTORY_DAYS = 90  # data exists this far before the account's first "today"
MAX_ADVANCE_DAYS = 60  # how far an account's "today" can move forward in total
MAX_PAGE = 500
DEFAULT_PAGE = 50
ACCOUNT_TTL_S = 24 * 3600
MAX_ACCOUNTS = 1000
# Share of each day's conversions reported so far, by age in days (1 = yesterday).
MATURITY = {1: 0.72, 2: 0.9, 3: 0.97}
AVERAGE_ORDER_VALUE = 65.0

TotalsProvider = Callable[[str], list[MetroTotal]]


@dataclass(frozen=True)
class PlatformConfig:
    db_path: Path
    secret: bytes
    client_id: str = "turf-demo"
    client_secret: str = "turf-demo-secret"
    token_ttl_s: int = 600
    fault_rate: float = 0.2  # share of first requests for a page that fail
    retry_after_s: int = 1


class PlatformError(Exception):
    """An error in Meta's format: ``{"error": {"message", "type", "code", ...}}``."""

    def __init__(
        self,
        status: int,
        code: int,
        message: str,
        type_: str = "OAuthException",
        transient: bool = False,
        subcode: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.type = status, code, message, type_
        self.transient, self.subcode, self.headers = transient, subcode, headers or {}

    def response(self) -> JSONResponse:
        body: dict[str, Any] = {
            "message": self.message,
            "type": self.type,
            "code": self.code,
            "is_transient": self.transient,
            "fbtrace_id": secrets.token_hex(8),
        }
        if self.subcode is not None:
            body["error_subcode"] = self.subcode
        return JSONResponse({"error": body}, status_code=self.status, headers=self.headers)


# ---------- Simulated numbers ----------


def _seed(*parts: object) -> int:
    digest = hashlib.sha256(":".join(map(str, parts)).encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _stochastic_round(x: float, rng: random.Random) -> int:
    return math.floor(x + rng.random())


def day_rows(
    sample: Sample, totals: list[MetroTotal], start: date, day: date, today: date
) -> list[dict[str, Any]]:
    """One day of insights for every market, as the API reports them on ``today``.

    Every random draw happens whatever ``today`` is, so a day's final numbers
    don't depend on when it's fetched; only the share reported so far does.
    """
    maturity = MATURITY.get((today - day).days, 1.0)
    weekday = 0.85 if day.weekday() >= 5 else 1.06
    rows: list[dict[str, Any]] = []
    total_spend = 0.0
    for t in totals:
        trend = random.Random(_seed(sample.seed, "trend", t.metro_code)).gauss(0, 0.12)
        rng = random.Random(_seed(sample.seed, t.metro_code, day.toordinal()))
        spend = t.spend / 30 * weekday * rng.lognormvariate(0, 0.12)
        # > 1 means better results than the monthly average: higher ROAS, lower CPA.
        quality = math.exp(trend * (day - start).days / 30 + rng.gauss(0, 0.18))
        if sample.kpi == "roas":
            value = spend * (t.revenue / t.spend) * quality
            purchases = _stochastic_round(value / AVERAGE_ORDER_VALUE, rng)
        else:
            value = 0.0
            purchases = _stochastic_round(spend / (t.spend / t.conversions) * quality, rng)
        total_spend += spend
        rows.append(_row(day, t.label, spend, math.floor(purchases * maturity), value * maturity))
    # Spend the platform couldn't place in a market (Meta's "Unknown" DMA).
    rng = random.Random(_seed(sample.seed, "unknown", day.toordinal()))
    unknown = total_spend * 0.004 * rng.uniform(0.5, 1.5)
    rows.append(_row(day, "Unknown", unknown, math.floor(rng.uniform(0, 3) * maturity), 0.0))
    return rows


def _row(day: date, region: str, spend: float, purchases: int, value: float) -> dict[str, Any]:
    row: dict[str, Any] = {
        "date_start": day.isoformat(),
        "date_stop": day.isoformat(),
        "region": region,
        "spend": f"{spend:.2f}",
        "actions": [{"action_type": "purchase", "value": str(purchases)}],
    }
    if value:
        row["action_values"] = [{"action_type": "purchase", "value": f"{value:.2f}"}]
    return row


# ---------- Accounts (simulated platform state, shared by workers via SQLite) ----------


class Account(BaseModel):
    id: str
    template: str
    start: date  # the account's first "today"
    today: date


class _Accounts:
    def __init__(self, path: Path) -> None:
        self.path = path
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, template TEXT NOT NULL, "
                "start TEXT NOT NULL, today TEXT NOT NULL, created_at REAL NOT NULL)"
            )

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            yield db
        finally:
            db.close()

    def create(self, template: str) -> Account:
        today = datetime.now(UTC).date()
        account = Account(
            id=f"act_{secrets.token_hex(6)}", template=template, start=today, today=today
        )
        now = time.time()
        with self._db() as db:
            db.execute("DELETE FROM accounts WHERE created_at < ?", [now - ACCOUNT_TTL_S])
            db.execute(
                "DELETE FROM accounts WHERE id NOT IN "
                "(SELECT id FROM accounts ORDER BY created_at DESC LIMIT ?)",
                [MAX_ACCOUNTS - 1],
            )
            db.execute(
                "INSERT INTO accounts VALUES (?, ?, ?, ?, ?)",
                [account.id, template, today.isoformat(), today.isoformat(), now],
            )
        return account

    def get(self, account_id: str) -> Account | None:
        with self._db() as db:
            row = db.execute(
                "SELECT id, template, start, today FROM accounts WHERE id = ?", [account_id]
            ).fetchone()
        if row is None:
            return None
        return Account(id=row[0], template=row[1], start=row[2], today=row[3])

    def advance(self, account: Account, days: int) -> Account:
        today = min(
            account.today + timedelta(days=days), account.start + timedelta(days=MAX_ADVANCE_DAYS)
        )
        with self._db() as db:
            db.execute(
                "UPDATE accounts SET today = ? WHERE id = ?", [today.isoformat(), account.id]
            )
        return account.model_copy(update={"today": today})


# ---------- Tokens (stateless) ----------


def _sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()[:32]


def issue_token(secret: bytes, account_id: str, ttl_s: int) -> str:
    payload = f"{account_id}.{int(time.time()) + ttl_s}"
    return f"{payload}.{_sign(secret, payload)}"


def check_token(secret: bytes, authorization: str | None, account_id: str) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise PlatformError(401, 190, "An access token is required to request this resource.")
    token = authorization[7:].strip()
    payload, _, sig = token.rpartition(".")
    owner, _, exp = payload.rpartition(".")
    if not payload or not hmac.compare_digest(_sign(secret, payload), sig) or not exp.isdigit():
        raise PlatformError(401, 190, "Invalid OAuth access token.")
    if int(exp) < time.time():
        raise PlatformError(401, 190, "Session has expired.", subcode=463)
    if owner != account_id:
        raise PlatformError(403, 200, "This token can't access that ad account.")


# ---------- Cursors ----------


def _encode_cursor(offset: int, query: str) -> str:
    raw = json.dumps({"o": offset, "q": query}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str, query: str) -> int:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        offset, q = int(raw["o"]), raw["q"]
    except (ValueError, KeyError, TypeError) as e:
        raise PlatformError(400, 100, "Invalid cursor.", type_="GraphMethodException") from e
    if q != query or offset < 0:
        raise PlatformError(
            400, 100, "Cursor doesn't belong to this query.", type_="GraphMethodException"
        )
    return offset


# ---------- The app ----------


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class AccountOut(BaseModel):
    id: str
    name: str
    currency: str = "USD"
    timezone_name: str = "America/New_York"
    # Simulation extensions: the account's simulated date, and the last day with data.
    current_date: date
    data_through: date


class NewAccount(BaseModel):
    template: str = Field(description="A sample brand key, e.g. 'pets'")


class Advance(BaseModel):
    days: int = Field(ge=1, le=7)


def create_platform_app(config: PlatformConfig, totals: TotalsProvider) -> FastAPI:
    app = FastAPI(
        title="Simulated ad platform",
        description="A stand-in for an ad platform's insights API (shaped like Meta's), "
        "serving Turf's fictional sample brands. See connector/platform.py.",
    )
    accounts = _Accounts(config.db_path)
    # Pages already served once, for fault injection: a page's first request
    # may fail, its retry won't. Per process; the connector's requests for one
    # sync all go to the process running it.
    seen: OrderedDict[str, None] = OrderedDict()

    @app.exception_handler(PlatformError)
    async def _platform_error(request: Request, exc: PlatformError) -> JSONResponse:
        return exc.response()

    def _account(account_id: str) -> Account:
        account = accounts.get(account_id)
        if account is None:
            raise PlatformError(
                404, 803, f"Ad account {account_id} doesn't exist.", type_="GraphMethodException"
            )
        return account

    def _account_out(a: Account) -> AccountOut:
        sample = SAMPLES[a.template]
        name = sample.name.replace("Sample ", "").capitalize()
        return AccountOut(
            id=a.id,
            name=f"{name} (simulated)",
            current_date=a.today,
            data_through=a.today - timedelta(days=1),
        )

    def _maybe_fail(key: str) -> None:
        if key in seen or config.fault_rate <= 0:
            return
        seen[key] = None
        if len(seen) > 20_000:
            seen.popitem(last=False)
        roll = _seed("fault", key) % 1000 / 1000
        if roll < config.fault_rate * 0.75:
            raise PlatformError(
                429,
                17,
                "User request limit reached",
                transient=True,
                headers={"Retry-After": str(config.retry_after_s)},
            )
        if roll < config.fault_rate:
            raise PlatformError(
                503, 2, "Service temporarily unavailable", type_="OAuthException", transient=True
            )

    @app.post("/_demo/accounts", status_code=201, tags=["demo controls"])
    def create_account(body: NewAccount) -> AccountOut:
        """Create a simulated ad account from a sample brand."""
        if body.template not in SAMPLES:
            raise PlatformError(
                400, 100, f"Unknown template: {body.template}", type_="GraphMethodException"
            )
        return _account_out(accounts.create(body.template))

    @app.post("/_demo/accounts/{account_id}/advance", tags=["demo controls"])
    def advance(account_id: str, body: Advance) -> AccountOut:
        """Move the account's simulated date forward (at most 60 days in total)."""
        return _account_out(accounts.advance(_account(account_id), body.days))

    @app.post("/oauth/access_token", tags=["oauth"])
    def access_token(
        client_id: Annotated[str, Form()],
        client_secret: Annotated[str, Form()],
        account_id: Annotated[str, Form()],
    ) -> TokenOut:
        ok_id = hmac.compare_digest(client_id, config.client_id)
        if not (ok_id and hmac.compare_digest(client_secret, config.client_secret)):
            raise PlatformError(401, 101, "Error validating client secret.")
        _account(account_id)
        token = issue_token(config.secret, account_id, config.token_ttl_s)
        return TokenOut(access_token=token, expires_in=config.token_ttl_s)

    @app.get("/v1/{account_id}", tags=["insights"])
    def get_account(
        account_id: str, authorization: Annotated[str | None, Header()] = None
    ) -> AccountOut:
        check_token(config.secret, authorization, account_id)
        return _account_out(_account(account_id))

    @app.get("/v1/{account_id}/insights", tags=["insights"])
    def insights(
        account_id: str,
        since: date,
        until: date,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
        after: str | None = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Daily results by market for ``since``..``until`` (inclusive), paged."""
        check_token(config.secret, authorization, account_id)
        account = _account(account_id)
        if until < since:
            raise PlatformError(400, 100, "until is before since.", type_="GraphMethodException")
        query = f"{account_id}|{since}|{until}|{limit}"
        offset = _decode_cursor(after, query) if after else 0
        _maybe_fail(f"{query}|{offset}")

        sample = SAMPLES[account.template]
        first = max(since, account.start - timedelta(days=HISTORY_DAYS))
        last = min(until, account.today - timedelta(days=1))  # no data for today yet
        days = [first + timedelta(days=i) for i in range(max(0, (last - first).days + 1))]
        per_day = len(totals(account.template)) + 1
        page_days = days[offset // per_day : (offset + limit) // per_day + 1]
        rows = [
            r
            for d in page_days
            for r in day_rows(sample, totals(account.template), account.start, d, account.today)
        ]
        skip = offset - (offset // per_day) * per_day
        data = rows[skip : skip + limit]
        total = len(days) * per_day
        out: dict[str, Any] = {"data": data, "paging": {}}
        if offset + limit < total:
            cursor = _encode_cursor(offset + limit, query)
            out["paging"] = {
                "cursors": {"after": cursor},
                "next": f"/v1/{account_id}/insights?since={since}&until={until}"
                f"&limit={limit}&after={cursor}",
            }
        return out

    return app
