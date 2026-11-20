"""The ad-account connector: simulated platform, HTTP client, store, sync jobs, API."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from turf_app.api import create_app
from turf_app.connector import sync as sync_mod
from turf_app.connector.client import AdsClient, InsightRow, PlatformAPIError, RetryPolicy
from turf_app.connector.platform import (
    MATURITY,
    PlatformConfig,
    create_platform_app,
    issue_token,
)
from turf_app.connector.store import STALE_HEARTBEAT_S, ActiveSyncError, Store
from turf_app.ratelimit import RateLimiter
from turf_app.samples import MetroTotal

SECRET = b"test-secret"
TOTALS = [
    MetroTotal("10000", "Metro 0, ST", 3000.0, 100.0, 0.0),
    MetroTotal("10001", "Metro 1, ST", 1500.0, 30.0, 0.0),
]


async def no_sleep(_: float) -> None:
    return None


# ---------- Simulated platform ----------


@pytest.fixture
def platform(tmp_path: Path) -> TestClient:
    config = PlatformConfig(db_path=tmp_path / "platform.sqlite3", secret=SECRET, fault_rate=0)
    return TestClient(create_platform_app(config, lambda key: TOTALS))


def _account(platform: TestClient) -> tuple[str, dict[str, str]]:
    account = platform.post("/_demo/accounts", json={"template": "pets"}).json()
    token = platform.post(
        "/oauth/access_token",
        data={
            "client_id": "turf-demo",
            "client_secret": "turf-demo-secret",
            "account_id": account["id"],
        },
    ).json()["access_token"]
    return account["id"], {"Authorization": f"Bearer {token}"}


def test_platform_requires_a_valid_token(platform: TestClient) -> None:
    account_id, auth = _account(platform)
    for headers in ({}, {"Authorization": "Bearer nope"}):
        resp = platform.get(f"/v1/{account_id}", headers=headers)
        assert resp.status_code == 401 and resp.json()["error"]["code"] == 190
    expired = issue_token(SECRET, account_id, ttl_s=-10)
    resp = platform.get(f"/v1/{account_id}", headers={"Authorization": f"Bearer {expired}"})
    assert resp.json()["error"]["error_subcode"] == 463
    other, _ = _account(platform)
    assert platform.get(f"/v1/{other}", headers=auth).status_code == 403
    bad_secret = {"client_id": "turf-demo", "client_secret": "x", "account_id": account_id}
    assert platform.post("/oauth/access_token", data=bad_secret).status_code == 401


def test_platform_pages_cover_every_row_once(platform: TestClient) -> None:
    account_id, auth = _account(platform)
    today = date.fromisoformat(
        platform.get(f"/v1/{account_id}", headers=auth).json()["current_date"]
    )
    since, until = today - timedelta(days=10), today + timedelta(days=5)  # future days: no data
    params: dict[str, Any] = {"since": since, "until": until, "limit": 7}
    seen: list[tuple[str, str]] = []
    while True:
        body = platform.get(f"/v1/{account_id}/insights", params=params, headers=auth).json()
        seen += [(r["date_start"], r["region"]) for r in body["data"]]
        if "next" not in body["paging"]:
            break
        params["after"] = body["paging"]["cursors"]["after"]
    assert len(seen) == len(set(seen)) == 10 * (len(TOTALS) + 1)  # 10 days through yesterday
    assert {r for _, r in seen} == {"Metro 0, ST", "Metro 1, ST", "Unknown"}
    # A cursor only works for the query it came from.
    params["since"] = since + timedelta(days=1)
    resp = platform.get(f"/v1/{account_id}/insights", params=params, headers=auth)
    assert resp.status_code == 400


def test_recent_days_fill_in_later(platform: TestClient) -> None:
    account_id, auth = _account(platform)
    today = date.fromisoformat(
        platform.get(f"/v1/{account_id}", headers=auth).json()["current_date"]
    )
    yesterday = today - timedelta(days=1)

    def conversions() -> int:
        body = platform.get(
            f"/v1/{account_id}/insights",
            headers=auth,
            params={"since": str(yesterday), "until": str(yesterday)},
        ).json()
        return sum(int(r["actions"][0]["value"]) for r in body["data"] if r["region"] != "Unknown")

    early = conversions()
    assert early == conversions()  # deterministic
    platform.post(f"/_demo/accounts/{account_id}/advance", json={"days": 7})
    final = conversions()
    assert final > early
    assert early <= final * MATURITY[1] + len(TOTALS)  # floor per market


def test_platform_injects_faults_once_per_page(tmp_path: Path) -> None:
    config = PlatformConfig(db_path=tmp_path / "p.sqlite3", secret=SECRET, fault_rate=1.0)
    platform = TestClient(create_platform_app(config, lambda key: TOTALS))
    account_id, auth = _account(platform)
    params = {"since": "2026-01-01", "until": "2026-01-02"}
    first = platform.get(f"/v1/{account_id}/insights", params=params, headers=auth)
    assert first.status_code in (429, 503) and first.json()["error"]["is_transient"]
    if first.status_code == 429:
        assert first.headers["Retry-After"] == "1"
    assert (
        platform.get(f"/v1/{account_id}/insights", params=params, headers=auth).status_code == 200
    )


# ---------- HTTP client ----------


def _client(handler: Callable[[httpx.Request], httpx.Response], events: list[Any]) -> AdsClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ads")
    return AdsClient(
        http,
        "act_1",
        "id",
        "secret",
        on_event=lambda k, d: events.append((k, d)),
        retry=RetryPolicy(max_attempts=3),
        sleep=no_sleep,
    )


def _ok_token(request: httpx.Request) -> httpx.Response | None:
    if request.url.path == "/oauth/access_token":
        return httpx.Response(200, json={"access_token": "t1", "expires_in": 600})
    return None


def test_client_retries_then_gives_up() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _ok_token(request) or httpx.Response(
            429, headers={"Retry-After": "2"}, json={"error": {"code": 17, "message": "slow down"}}
        )

    events: list[Any] = []
    with pytest.raises(PlatformAPIError, match="Gave up after 3 attempts"):
        asyncio.run(_client(handler, events).account())
    retries = [d for k, d in events if k == "retry"]
    assert len(retries) == 2 and all(r["wait_s"] == 2 and r["retry_after"] for r in retries)


def test_client_does_not_retry_client_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_token(request) or httpx.Response(400, json={"error": {"message": "bad field"}})

    events: list[Any] = []
    with pytest.raises(PlatformAPIError, match="bad field"):
        asyncio.run(_client(handler, events).account())
    assert not [k for k, _ in events if k == "retry"]


def test_client_refreshes_a_rejected_token() -> None:
    tokens = iter(["stale", "fresh"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/access_token":
            return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 600})
        if request.headers["Authorization"] == "Bearer stale":
            return httpx.Response(401, json={"error": {"code": 190, "message": "expired"}})
        return httpx.Response(200, json={"id": "act_1"})

    events: list[Any] = []
    assert asyncio.run(_client(handler, events).account()) == {"id": "act_1"}
    assert [d["reason"] for k, d in events if k == "auth"] == ["first request", "expired"]


def test_client_skips_rows_that_dont_parse() -> None:
    good = {
        "date_start": "2026-01-01",
        "date_stop": "2026-01-01",
        "region": "Austin, TX",
        "spend": "10.50",
        "actions": [
            {"action_type": "purchase", "value": "2"},
            {"action_type": "link_click", "value": "40"},
        ],
    }
    bad = [
        {**good, "spend": "-1"},
        {**good, "spend": "lots"},
        {**good, "date_stop": "2026-01-07"},
        {**good, "region": ""},
        None,  # not an object at all
        "row",
        {**good, "actions": 5},  # not a list
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_token(request) or httpx.Response(200, json={"data": [good, *bad], "paging": {}})

    async def pages() -> list[Any]:
        return [p async for p in _client(handler, []).insights(date(2026, 1, 1), date(2026, 1, 1))]

    (page,) = asyncio.run(pages())
    assert page.rows == [InsightRow.parse(good)]
    assert page.rows[0].conversions == 2 and page.rows[0].revenue == 0  # only purchases count
    assert len(page.rejected) == 7


# ---------- Store ----------


def test_upserts_are_idempotent(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.sqlite3")
    conn = store.create_connection("pets", "act_1")
    day = date(2026, 1, 1)
    rows = [(day, "A", 10.0, 1.0, 0.0), (day, "B", 5.0, 0.0, 0.0)]
    assert store.upsert_insights(conn.id, rows).model_dump() == {
        "inserted": 2,
        "updated": 0,
        "unchanged": 0,
    }
    assert store.upsert_insights(conn.id, rows).unchanged == 2
    restated = [(day, "A", 10.0, 3.0, 0.0)]
    assert store.upsert_insights(conn.id, restated).updated == 1
    assert store.row_count(conn.id) == 2
    assert store.window_totals(conn.id, day, day) == [("A", 10.0, 3.0, 0.0), ("B", 5.0, 0.0, 0.0)]
    assert store.delete_connection(conn.id) and store.row_count(conn.id) == 0  # cascades


def test_one_active_sync_per_connection(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.sqlite3")
    conn = store.create_connection("pets", "act_1")
    first = store.create_sync(conn.id)
    with pytest.raises(ActiveSyncError) as e:
        store.create_sync(conn.id)
    assert e.value.sync_id == first.id
    assert store.finish(first.id, "succeeded")
    assert not store.finish(first.id, "failed")  # already ended: no second "done" event
    assert [ev.kind for ev in store.events(first.id)] == ["done"]
    assert store.create_sync(conn.id).id != first.id


def test_client_stops_on_a_repeated_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = {"data": [], "paging": {"cursors": {"after": "same"}, "next": "/more"}}
        return _ok_token(request) or httpx.Response(200, json=page)

    async def drain() -> None:
        async for _ in _client(handler, []).insights(date(2026, 1, 1), date(2026, 1, 2)):
            pass

    with pytest.raises(PlatformAPIError, match="same page cursor"):
        asyncio.run(drain())


def test_a_reaped_job_stays_dead(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.sqlite3")
    conn = store.create_connection("pets", "act_1")
    later = time.time() + 3600
    day = date(2026, 1, 1)

    # Reaped while queued: it may not start (and can't clash with a newer job).
    queued = store.create_sync(conn.id)
    assert store.reap_stale(now=later) == 1
    newer = store.create_sync(conn.id)
    assert not store.start(queued.id, "backfill", day, day)
    assert store.start(newer.id, "backfill", day, day)

    # Reaped while running: heartbeats fail, and it can't succeed or move the connection.
    assert store.heartbeat(newer.id)
    assert store.reap_stale(now=later) == 1
    assert not store.heartbeat(newer.id, {"pages": 1})
    assert not store.succeed(newer.id, conn.id, day, {"recs": []}, {"changes": {}})
    fresh = store.get_connection(conn.id)
    assert fresh is not None and fresh.synced_through is None
    assert store.last_report(conn.id) is None


def test_eviction_spares_connections_mid_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("turf_app.connector.store.MAX_CONNECTIONS", 2)
    store = Store(tmp_path / "s.sqlite3")
    busy = store.create_connection("pets", "act_1")
    store.create_sync(busy.id)
    idle = store.create_connection("pets", "act_2")
    store.create_connection("pets", "act_3")  # over the cap: the oldest idle one goes
    assert store.get_connection(busy.id, touch=False) is not None
    assert store.get_connection(idle.id, touch=False) is None


def test_stale_jobs_are_reaped(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.sqlite3")
    conn = store.create_connection("pets", "act_1")
    sync = store.create_sync(conn.id)
    store.start(sync.id, "backfill", date(2026, 1, 1), date(2026, 1, 2))
    assert store.reap_stale() == 0
    assert store.reap_stale(now=time.time() + STALE_HEARTBEAT_S + 1) == 1
    reaped = store.get_sync(sync.id)
    assert reaped is not None and reaped.status == "failed"
    store.create_sync(conn.id)  # the connection is free again


def test_sync_range() -> None:
    through = date(2026, 3, 10)
    assert sync_mod.sync_range(None, through) == ("backfill", date(2026, 2, 11), through)
    # Three new days, re-fetching the last three synced days for late conversions.
    assert sync_mod.sync_range(date(2026, 3, 7), through) == (
        "incremental",
        date(2026, 3, 5),
        through,
    )
    # Nothing new: still re-fetch the lookback window.
    assert sync_mod.sync_range(through, through) == ("incremental", date(2026, 3, 8), through)


def test_diff_reports_entries_exits_and_moves() -> None:
    def summary(*codes: str) -> dict[str, Any]:
        return {
            "recs": [{"code": c, "name": c.upper(), "rank": i + 1} for i, c in enumerate(codes)],
            "kpi_value": 1.0,
            "verdict": "strong",
            "window": None,
        }

    assert sync_mod.diff(None, summary("a")) == {"first": True}
    d = sync_mod.diff(summary("a", "b", "c"), summary("c", "a", "d"))
    assert d["entered"] == [{"code": "d", "name": "D", "rank": 3}]
    assert d["left"] == [{"code": "b", "name": "B", "was": 2}]
    assert d["moved"] == [
        {"code": "c", "name": "C", "from": 3, "to": 1},
        {"code": "a", "name": "A", "from": 1, "to": 2},
    ]
    assert d["previous_rank"] == {"c": 3, "a": 1, "d": None}


# ---------- API, end to end ----------


@pytest.fixture
def app_client(serving_db: Path, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        serving_db,
        rate_limiter=RateLimiter(rules=()),
        state_dir=tmp_path,
        platform_fault_rate=1.0,
        connector_sleep=no_sleep,
    )
    # The test database's metros are few and small; let the report use all of them.
    app.state.connector.deps.report_options = {"min_population": 0, "include_micro": True}
    with TestClient(app) as c:
        yield c


def _wait(client: TestClient, cid: str, sid: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        sync: dict[str, Any] = client.get(f"/api/connections/{cid}/syncs/{sid}").json()
        if sync["status"] in ("succeeded", "failed"):
            return sync
        time.sleep(0.05)
    raise AssertionError("sync didn't finish")


def _events(client: TestClient, cid: str, sid: str, last: int = 0) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    headers = {"Last-Event-ID": str(last)} if last else {}
    with client.stream("GET", f"/api/connections/{cid}/syncs/{sid}/events", headers=headers) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        kind = ""
        for line in r.iter_lines():
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                out.append((kind, json.loads(line[6:])))
    return out


def test_connect_backfill_then_incremental(app_client: TestClient) -> None:
    created = app_client.post("/api/connections", json={"sample": "pets"})
    assert created.status_code == 201
    cid, sid = created.json()["connection"]["id"], created.json()["sync"]["id"]
    account_id = created.json()["connection"]["account_id"]

    backfill = _wait(app_client, cid, sid)
    assert backfill["status"] == "succeeded", backfill["error"]
    assert backfill["mode"] == "backfill"
    assert backfill["stats"]["inserted"] == backfill["stats"]["rows"] > 0
    # Fault rate 1: every page is refused once (429 or 503), then served.
    assert backfill["stats"]["retries"] == backfill["stats"]["pages"] > 0
    assert backfill["result"]["changes"] == {"first": True}

    kinds = [k for k, _ in _events(app_client, cid, sid)]
    assert kinds[0] == "queued" and kinds[-1] == "done"
    assert {"auth", "started", "retry", "page", "fetched", "report"} <= set(kinds)
    # Resuming after an event id returns only what came after it.
    assert len(_events(app_client, cid, sid, last=len(kinds) - 1)) == 1

    results = app_client.get(f"/api/connections/{cid}/results").json()
    assert results["sample"]["key"] == "pets" and results["rows"]
    assert (date.fromisoformat(results["until"]) - date.fromisoformat(results["since"])).days == 27
    report = app_client.post(
        "/api/performance",
        json={
            "rows": results["rows"],
            "geo_type": "metro",
            "kpi": "cpa",
            "preset": "pets",
            "min_population": 0,
            "include_micro": True,
        },
    )
    assert report.status_code == 200
    assert "Unknown" in report.json()["unmatched_examples"]

    # The platform moves on two days: new days arrive and recent ones fill in.
    app_client.post(f"/mock/ads/_demo/accounts/{account_id}/advance", json={"days": 2})
    started = app_client.post(f"/api/connections/{cid}/syncs")
    assert started.status_code == 202
    duplicate = app_client.post(f"/api/connections/{cid}/syncs")
    assert duplicate.status_code in (202, 409)  # 202 only if the first already finished
    if duplicate.status_code == 409:
        assert duplicate.json()["sync"]["id"] == started.json()["id"]
    inc = _wait(app_client, cid, started.json()["id"])
    assert inc["status"] == "succeeded" and inc["mode"] == "incremental"
    per_day = backfill["stats"]["rows"] // 28
    assert inc["stats"]["inserted"] == 2 * per_day
    assert inc["stats"]["updated"] > 0  # late conversions on the re-fetched days
    assert inc["result"]["changes"]["first"] is False

    conn = app_client.get(f"/api/connections/{cid}").json()
    assert conn["stored_rows"] == 30 * per_day
    assert app_client.delete(f"/api/connections/{cid}").status_code == 204
    assert app_client.get(f"/api/connections/{cid}").status_code == 404


def test_connection_errors(app_client: TestClient) -> None:
    assert app_client.post("/api/connections", json={"sample": "nope"}).status_code == 404
    assert app_client.get("/api/connections/missing").status_code == 404
    assert app_client.post("/api/connections/missing/syncs").status_code == 404
    created = app_client.post("/api/connections", json={"sample": "fitness"}).json()
    cid = created["connection"]["id"]
    assert app_client.get(f"/api/connections/{cid}/syncs/other").status_code == 404
    _wait(app_client, cid, created["sync"]["id"])


def test_mock_platform_is_mounted_and_documented(app_client: TestClient) -> None:
    assert app_client.get("/mock/ads/openapi.json").json()["info"]["title"] == (
        "Simulated ad platform"
    )
    assert app_client.get("/mock/ads/v1/act_x").json()["error"]["code"] == 190
