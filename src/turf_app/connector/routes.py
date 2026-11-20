"""Connector API: connect a (simulated) ad account, sync it, stream progress.

    POST   /api/connections                         connect a sample account; starts a backfill
    GET    /api/connections/{id}                    the connection and its latest sync
    DELETE /api/connections/{id}                    disconnect and delete its data
    POST   /api/connections/{id}/syncs              start a sync (409 if one is running)
    GET    /api/connections/{id}/syncs/{sync}       a sync's status, stats, and result
    GET    /api/connections/{id}/syncs/{sync}/events  progress as server-sent events
    GET    /api/connections/{id}/results            the report window's rows, ready for
                                                    /api/performance

A connection's id is an unguessable token, and the only way to reach it: there
are no accounts in the demo.

Jobs run as asyncio tasks in the worker that received the request, at most
``MAX_CONCURRENT_SYNCS`` at a time per worker (the rest wait as "queued"), so a
burst of visitors can't starve the report API of CPU. The progress stream
reads events from the store, so it works from either worker.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from datetime import date
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from turf_app import samples
from turf_app.connector.client import RetryPolicy, Sleep
from turf_app.connector.store import ActiveSyncError, Connection, Store, Sync
from turf_app.connector.sync import Analyze, SyncDeps, report_request, report_window, run_sync
from turf_app.performance import ResultRow

MAX_CONCURRENT_SYNCS = 2
STREAM_POLL_S = 0.25
STREAM_KEEPALIVE_S = 15.0
STREAM_MAX_S = 300.0


class ConnectorService:
    """Owns the running sync tasks of this worker."""

    def __init__(
        self,
        store: Store,
        http: Callable[[], AbstractAsyncContextManager[httpx.AsyncClient]],
        analyze: Analyze,
        client_id: str,
        client_secret: str,
        retry: RetryPolicy | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.store = store
        self.http = http
        self.deps = SyncDeps(
            store=store,
            http=http,
            analyze=analyze,
            client_id=client_id,
            client_secret=client_secret,
            retry=retry or RetryPolicy(),
            sleep=sleep,
        )
        self._tasks: set[asyncio.Task[None]] = set()
        self._slots: asyncio.Semaphore | None = None

    async def create_account(self, sample_key: str) -> str:
        """Create the simulated ad account (the platform's demo control)."""
        async with self.http() as http:
            resp = await http.post("/_demo/accounts", json={"template": sample_key})
            resp.raise_for_status()
            account_id: str = resp.json()["id"]
            return account_id

    def start_sync(self, connection: Connection) -> Sync:
        sync = self.store.create_sync(connection.id)  # raises ActiveSyncError
        self.store.add_event(sync.id, "queued", {})
        try:
            task = asyncio.create_task(self._run(connection, sync.id))
        except BaseException:
            # Don't leave a queued job behind that blocks the connection.
            self.store.finish(sync.id, "failed", error="The sync couldn't be started.")
            raise
        self._tasks.add(task)  # keep a reference until it's done
        task.add_done_callback(self._tasks.discard)
        return sync

    async def _run(self, connection: Connection, sync_id: str) -> None:
        if self._slots is None:
            self._slots = asyncio.Semaphore(MAX_CONCURRENT_SYNCS)
        async with self._slots:
            # Re-read: another sync may have advanced it while this one waited.
            fresh = self.store.get_connection(connection.id, touch=False)
            if fresh is None:
                self.store.finish(sync_id, "failed", error="The connection was deleted.")
                return
            await run_sync(self.deps, fresh, sync_id)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


class NewConnection(BaseModel):
    sample: str = Field(description="A sample brand key (see /api/samples)")


class SyncOut(BaseModel):
    id: str
    status: str
    mode: str | None
    since: date | None
    until: date | None
    stats: dict[str, int]
    result: dict[str, Any] | None
    error: str | None
    created_at: float
    finished_at: float | None


class ConnectionOut(BaseModel):
    id: str
    sample: samples.SampleInfo
    account_id: str
    synced_through: date | None
    stored_rows: int
    latest_sync: SyncOut | None


class CreatedConnection(BaseModel):
    connection: ConnectionOut
    sync: SyncOut


class ConnectionResults(BaseModel):
    sample: samples.SampleInfo
    since: date
    until: date
    rows: list[ResultRow]


def _sync_out(s: Sync) -> SyncOut:
    return SyncOut(
        id=s.id,
        status=s.status,
        mode=s.mode,
        since=s.since,
        until=s.until,
        stats=s.stats,
        result=s.result,
        error=s.error,
        created_at=s.created_at,
        finished_at=s.finished_at,
    )


def build_router(service: Callable[[], ConnectorService]) -> APIRouter:
    router = APIRouter(prefix="/api/connections", tags=["connector"])

    def _connection(connection_id: str) -> Connection:
        conn = service().store.get_connection(connection_id)
        if conn is None:
            raise HTTPException(404, "No such connection (connections expire after a day).")
        return conn

    def _out(conn: Connection) -> ConnectionOut:
        store = service().store
        latest = store.latest_sync(conn.id)
        return ConnectionOut(
            id=conn.id,
            sample=samples.info(samples.SAMPLES[conn.sample_key]),
            account_id=conn.account_id,
            synced_through=conn.synced_through,
            stored_rows=store.row_count(conn.id),
            latest_sync=_sync_out(latest) if latest else None,
        )

    @router.post("", status_code=201)
    async def create_connection(body: NewConnection) -> CreatedConnection:
        """Connect a simulated ad account for a sample brand, and start its backfill."""
        if body.sample not in samples.SAMPLES:
            raise HTTPException(404, f"Unknown sample: {body.sample}")
        svc = service()
        account_id = await svc.create_account(body.sample)
        conn = svc.store.create_connection(body.sample, account_id)
        sync = svc.start_sync(conn)
        return CreatedConnection(connection=_out(conn), sync=_sync_out(sync))

    @router.get("/{connection_id}")
    def get_connection(connection_id: str) -> ConnectionOut:
        service().store.reap_stale()
        return _out(_connection(connection_id))

    @router.delete("/{connection_id}", status_code=204)
    def delete_connection(connection_id: str) -> Response:
        """Disconnect: the connection, its syncs, and every stored row are deleted."""
        if not service().store.delete_connection(connection_id):
            raise HTTPException(404, "No such connection.")
        return Response(status_code=204)

    @router.post(
        "/{connection_id}/syncs",
        status_code=202,
        response_model=SyncOut,
        responses={409: {"description": "A sync is already queued or running"}},
    )
    async def start_sync(connection_id: str) -> SyncOut | JSONResponse:
        # async: the job is an asyncio task on this worker's event loop.
        svc = service()
        conn = _connection(connection_id)
        try:
            return _sync_out(svc.start_sync(conn))
        except ActiveSyncError as e:
            active = svc.store.get_sync(e.sync_id)
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "A sync is already running for this connection.",
                    "sync": _sync_out(active).model_dump(mode="json") if active else None,
                },
            )

    def _sync(connection_id: str, sync_id: str) -> Sync:
        sync = service().store.get_sync(sync_id)
        if sync is None or sync.connection_id != connection_id:
            raise HTTPException(404, "No such sync.")
        return sync

    @router.get("/{connection_id}/syncs/{sync_id}")
    def get_sync(connection_id: str, sync_id: str) -> SyncOut:
        service().store.reap_stale()
        return _sync_out(_sync(connection_id, sync_id))

    @router.get("/{connection_id}/syncs/{sync_id}/events", response_class=StreamingResponse)
    async def sync_events(connection_id: str, sync_id: str, request: Request) -> StreamingResponse:
        """Server-sent events: ``queued``, ``auth``, ``started``, ``retry``, ``page``,
        ``fetched``, ``report``, then ``done`` or ``failed``. Resumes after
        ``Last-Event-ID``, so a dropped connection picks up where it left off."""
        _sync(connection_id, sync_id)
        store = service().store
        header = request.headers.get("last-event-id", "0")
        last = int(header) if header.isdigit() else 0

        async def stream() -> AsyncIterator[str]:
            nonlocal last
            yield "retry: 2000\n\n"
            started = quiet_since = time.monotonic()
            reaped_at = 0.0
            while time.monotonic() - started < STREAM_MAX_S:
                if await request.is_disconnected():
                    return
                for e in store.events(sync_id, last):
                    last = e.seq
                    payload = json.dumps({"at": e.at, **e.data})
                    yield f"id: {e.seq}\nevent: {e.kind}\ndata: {payload}\n\n"
                    quiet_since = time.monotonic()
                    if e.kind in ("done", "failed"):
                        return
                now = time.monotonic()
                if now - reaped_at > 5:
                    store.reap_stale()  # a job whose worker died ends with "failed"
                    reaped_at = now
                if now - quiet_since > STREAM_KEEPALIVE_S:
                    yield ": keepalive\n\n"
                    quiet_since = now
                await asyncio.sleep(STREAM_POLL_S)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            # Proxies (nginx, some CDNs) buffer responses unless told not to.
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/{connection_id}/results")
    def results(connection_id: str) -> ConnectionResults:
        """The report window's totals per market (last 28 synced days), shaped as
        ``/api/performance`` rows, so the page builds the report the same way as
        for an upload."""
        conn = _connection(connection_id)
        if conn.synced_through is None:
            raise HTTPException(409, "Nothing synced yet.")
        req = report_request(service().store, conn, conn.synced_through)
        since, until = report_window(conn.synced_through)
        return ConnectionResults(
            sample=samples.info(samples.SAMPLES[conn.sample_key]),
            since=since,
            until=until,
            rows=req.rows,
        )

    return router
