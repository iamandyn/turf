"""Connector state in SQLite: connections, sync jobs, progress events, synced rows.

Why SQLite: production runs two uvicorn workers in a read-only container with
a writable /tmp. A sync runs in whichever worker received the request, but the
page's progress stream or status poll can land on the other one, so job state
can't live in process memory. One SQLite file in WAL mode gives both workers
the same view with no extra service to run. (Moving to Postgres or Redis would
change this module only.) /tmp is a tmpfs, so state is lost on restart, which
suits a demo whose data is synthetic; connections also expire after a day.

Invariants the schema enforces:

- **One active sync per connection**: a partial unique index on ``syncs``
  where status is queued or running. A second request gets the running job
  instead of starting a duplicate.
- **Idempotent rows**: ``insights`` is keyed by (connection, date, market) and
  written with upserts, so re-fetching a day (as every incremental sync does
  for late conversions) updates it instead of double-counting.
- **No zombie jobs**: a running job updates its heartbeat on every page; one
  whose heartbeat is stale (its worker died or restarted) is marked failed the
  next time anyone looks, which also frees the connection for a new sync.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

CONNECTION_TTL_S = 24 * 3600
MAX_CONNECTIONS = 300
STALE_HEARTBEAT_S = 30.0
# A queued job waits for a free slot; syncs take seconds, so minutes means lost.
STALE_QUEUED_S = 120.0

SyncStatus = Literal["queued", "running", "succeeded", "failed"]
ACTIVE: tuple[SyncStatus, ...] = ("queued", "running")

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    sample_key TEXT NOT NULL,
    account_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    synced_through TEXT,          -- last day fetched (ISO date)
    last_report TEXT              -- JSON: the previous sync's recommendations, for diffs
);
CREATE TABLE IF NOT EXISTS syncs (
    id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    mode TEXT,                    -- backfill | incremental
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    heartbeat REAL,
    since TEXT,
    until TEXT,
    stats TEXT NOT NULL DEFAULT '{}',
    result TEXT,                  -- JSON: what changed in the report
    error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_sync
    ON syncs(connection_id) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS syncs_by_connection ON syncs(connection_id, created_at);
CREATE TABLE IF NOT EXISTS sync_events (
    sync_id TEXT NOT NULL REFERENCES syncs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (sync_id, seq)
);
CREATE TABLE IF NOT EXISTS insights (
    connection_id TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    geo TEXT NOT NULL,
    spend REAL NOT NULL,
    conversions REAL NOT NULL,
    revenue REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (connection_id, date, geo)
) WITHOUT ROWID;
"""


class Connection(BaseModel):
    id: str
    sample_key: str
    account_id: str
    created_at: float
    synced_through: date | None


class Sync(BaseModel):
    id: str
    connection_id: str
    status: SyncStatus
    mode: str | None
    created_at: float
    started_at: float | None
    finished_at: float | None
    since: date | None
    until: date | None
    stats: dict[str, int]
    result: dict[str, Any] | None
    error: str | None


class Event(BaseModel):
    seq: int
    at: float
    kind: str
    data: dict[str, Any]


class ActiveSyncError(Exception):
    def __init__(self, sync_id: str) -> None:
        super().__init__(sync_id)
        self.sync_id = sync_id


class UpsertStats(BaseModel):
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


_SYNC_COLUMNS = (
    "id, connection_id, status, mode, created_at, started_at, finished_at, since, until, "
    "stats, result, error"
)


def _sync(row: tuple[Any, ...]) -> Sync:
    return Sync(
        id=row[0],
        connection_id=row[1],
        status=row[2],
        mode=row[3],
        created_at=row[4],
        started_at=row[5],
        finished_at=row[6],
        since=row[7],
        until=row[8],
        stats=json.loads(row[9]),
        result=json.loads(row[10]) if row[10] else None,
        error=row[11],
    )


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        with self._db() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        # A connection per operation: cheap for SQLite, and safe from any thread.
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=NORMAL")
            yield db
        finally:
            db.close()

    # ---------- Connections ----------

    def create_connection(self, sample_key: str, account_id: str) -> Connection:
        now = time.time()
        conn = Connection(
            id=secrets.token_urlsafe(16),
            sample_key=sample_key,
            account_id=account_id,
            created_at=now,
            synced_through=None,
        )
        with self._db() as db:
            # Bounded: expire idle connections, then cap the total (oldest go first).
            db.execute("DELETE FROM connections WHERE last_seen < ?", [now - CONNECTION_TTL_S])
            db.execute(
                "DELETE FROM connections WHERE id NOT IN "
                "(SELECT id FROM connections ORDER BY last_seen DESC LIMIT ?)",
                [MAX_CONNECTIONS - 1],
            )
            db.execute(
                "INSERT INTO connections (id, sample_key, account_id, created_at, last_seen) "
                "VALUES (?, ?, ?, ?, ?)",
                [conn.id, sample_key, account_id, now, now],
            )
        return conn

    def get_connection(self, connection_id: str, touch: bool = True) -> Connection | None:
        with self._db() as db:
            if touch:
                db.execute(
                    "UPDATE connections SET last_seen = ? WHERE id = ?",
                    [time.time(), connection_id],
                )
            row = db.execute(
                "SELECT id, sample_key, account_id, created_at, synced_through "
                "FROM connections WHERE id = ?",
                [connection_id],
            ).fetchone()
        if row is None:
            return None
        return Connection(
            id=row[0],
            sample_key=row[1],
            account_id=row[2],
            created_at=row[3],
            synced_through=row[4],
        )

    def delete_connection(self, connection_id: str) -> bool:
        with self._db() as db:
            return db.execute("DELETE FROM connections WHERE id = ?", [connection_id]).rowcount > 0

    def last_report(self, connection_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT last_report FROM connections WHERE id = ?", [connection_id]
            ).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    # ---------- Syncs ----------

    def reap_stale(self, now: float | None = None) -> int:
        """Fail jobs whose worker stopped updating them (or never picked them up)."""
        now = now or time.time()
        with self._db() as db:
            stale = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM syncs WHERE (status = 'running' AND heartbeat < ?) "
                    "OR (status = 'queued' AND created_at < ?)",
                    [now - STALE_HEARTBEAT_S, now - STALE_QUEUED_S],
                )
            ]
        for sync_id in stale:
            self.finish(sync_id, "failed", error="The sync stopped responding (worker restarted).")
        return len(stale)

    def create_sync(self, connection_id: str) -> Sync:
        """Queue a sync, or raise ActiveSyncError with the one already queued or running."""
        self.reap_stale()
        sync_id = secrets.token_urlsafe(12)
        with self._db() as db:
            try:
                db.execute(
                    "INSERT INTO syncs (id, connection_id, status, created_at) "
                    "VALUES (?, ?, 'queued', ?)",
                    [sync_id, connection_id, time.time()],
                )
            except sqlite3.IntegrityError as e:
                row = db.execute(
                    "SELECT id FROM syncs WHERE connection_id = ? "
                    "AND status IN ('queued', 'running')",
                    [connection_id],
                ).fetchone()
                if row is None:  # the connection itself is gone
                    raise
                raise ActiveSyncError(row[0]) from e
        sync = self.get_sync(sync_id)
        assert sync is not None
        return sync

    def get_sync(self, sync_id: str) -> Sync | None:
        with self._db() as db:
            row = db.execute(
                f"SELECT {_SYNC_COLUMNS} FROM syncs WHERE id = ?", [sync_id]
            ).fetchone()
        return _sync(row) if row else None

    def latest_sync(self, connection_id: str) -> Sync | None:
        with self._db() as db:
            row = db.execute(
                f"SELECT {_SYNC_COLUMNS} FROM syncs WHERE connection_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                [connection_id],
            ).fetchone()
        return _sync(row) if row else None

    def start(self, sync_id: str, mode: str, since: date, until: date) -> None:
        now = time.time()
        with self._db() as db:
            db.execute(
                "UPDATE syncs SET status = 'running', mode = ?, since = ?, until = ?, "
                "started_at = ?, heartbeat = ? WHERE id = ?",
                [mode, since.isoformat(), until.isoformat(), now, now, sync_id],
            )

    def heartbeat(self, sync_id: str, stats: dict[str, int]) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE syncs SET heartbeat = ?, stats = ? WHERE id = ?",
                [time.time(), json.dumps(stats), sync_id],
            )

    def finish(
        self,
        sync_id: str,
        status: SyncStatus,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        """End a job, once: False if it had already ended (e.g. reaped as stale)."""
        with self._db() as db:
            changed = db.execute(
                "UPDATE syncs SET status = ?, finished_at = ?, result = ?, error = ? "
                "WHERE id = ? AND status IN ('queued', 'running')",
                [status, time.time(), json.dumps(result) if result else None, error, sync_id],
            ).rowcount
        if not changed:
            return False
        self.add_event(
            sync_id,
            "done" if status == "succeeded" else "failed",
            {"status": status, "error": error},
        )
        return True

    # ---------- Events ----------

    def add_event(self, sync_id: str, kind: str, data: dict[str, Any]) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO sync_events (sync_id, seq, at, kind, data) VALUES (?, "
                "(SELECT coalesce(max(seq), 0) + 1 FROM sync_events WHERE sync_id = ?), ?, ?, ?)",
                [sync_id, sync_id, time.time(), kind, json.dumps(data)],
            )

    def events(self, sync_id: str, after: int = 0) -> list[Event]:
        with self._db() as db:
            rows = db.execute(
                "SELECT seq, at, kind, data FROM sync_events WHERE sync_id = ? AND seq > ? "
                "ORDER BY seq",
                [sync_id, after],
            ).fetchall()
        return [Event(seq=r[0], at=r[1], kind=r[2], data=json.loads(r[3])) for r in rows]

    # ---------- Synced rows ----------

    def upsert_insights(
        self,
        connection_id: str,
        rows: Iterable[tuple[date, str, float, float, float]],
    ) -> UpsertStats:
        """Insert or update (date, market) rows; counts what actually changed."""
        rows = list(rows)
        stats = UpsertStats()
        if not rows:
            return stats
        now = time.time()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                days = sorted({r[0].isoformat() for r in rows})
                existing = {
                    (d, g): (s, c, v)
                    for d, g, s, c, v in db.execute(
                        "SELECT date, geo, spend, conversions, revenue FROM insights "
                        "WHERE connection_id = ? AND date BETWEEN ? AND ?",
                        [connection_id, days[0], days[-1]],
                    )
                }
                for day, geo, spend, conversions, revenue in rows:
                    old = existing.get((day.isoformat(), geo))
                    if old is None:
                        stats.inserted += 1
                    elif old != (spend, conversions, revenue):
                        stats.updated += 1
                    else:
                        stats.unchanged += 1
                db.executemany(
                    "INSERT INTO insights VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (connection_id, date, geo) DO UPDATE SET "
                    "spend = excluded.spend, conversions = excluded.conversions, "
                    "revenue = excluded.revenue, updated_at = excluded.updated_at "
                    "WHERE (spend, conversions, revenue) IS NOT "
                    "(excluded.spend, excluded.conversions, excluded.revenue)",
                    [(connection_id, d.isoformat(), g, s, c, v, now) for d, g, s, c, v in rows],
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return stats

    def window_totals(
        self, connection_id: str, since: date, until: date
    ) -> list[tuple[str, float, float, float]]:
        """Per-market totals over ``since``..``until``: (geo, spend, conversions, revenue)."""
        with self._db() as db:
            return db.execute(
                "SELECT geo, sum(spend), sum(conversions), sum(revenue) FROM insights "
                "WHERE connection_id = ? AND date BETWEEN ? AND ? GROUP BY geo ORDER BY geo",
                [connection_id, since.isoformat(), until.isoformat()],
            ).fetchall()

    def row_count(self, connection_id: str) -> int:
        with self._db() as db:
            row = db.execute(
                "SELECT count(*) FROM insights WHERE connection_id = ?", [connection_id]
            ).fetchone()
        return int(row[0])

    def complete_sync(
        self, sync_id: str, connection_id: str, through: date, report: dict[str, Any]
    ) -> None:
        """Record what a successful sync reached and the report it produced."""
        with self._db() as db:
            db.execute(
                "UPDATE connections SET synced_through = ?, last_report = ? WHERE id = ?",
                [through.isoformat(), json.dumps(report), connection_id],
            )
