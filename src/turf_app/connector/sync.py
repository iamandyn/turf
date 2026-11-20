"""One sync job: fetch from the ad platform, store, recompute the report.

- **Backfill** (first sync): the last ``BACKFILL_DAYS`` days.
- **Incremental** (later syncs): from the day after the last one synced,
  starting ``LOOKBACK_DAYS`` earlier. Platforms keep adding conversions to
  recent days for a while (attribution windows); re-fetching them, and
  upserting, is how stored numbers catch up instead of staying low forever.
  A sync with no new days still re-fetches the lookback window.
- **Report**: the brand's results over the last ``REPORT_DAYS`` days through
  the newest synced day go through the same analysis as an upload
  (``performance.analyze``), and the recommendations are compared with the
  previous sync's: which metros entered, left, or moved.

Progress goes to the store as events (auth, retries, each page and what it
changed, the report), which the API streams to the page.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import httpx
from pydantic import ValidationError

from turf_app.connector.client import AdsClient, PlatformAPIError, RetryPolicy, Sleep
from turf_app.connector.store import Connection, Store
from turf_app.markets import UniverseTooSmallError
from turf_app.performance import (
    NoUsableMetrosError,
    PerformanceRequest,
    PerformanceResponse,
    ResultRow,
)
from turf_app.samples import SAMPLES

log = logging.getLogger(__name__)

BACKFILL_DAYS = 28
LOOKBACK_DAYS = 3
REPORT_DAYS = 28
REPORT_LIMIT = 15  # recommendations computed, as the page asks for
DIFF_TOP = 10  # recommendations compared between syncs
PAGE_SIZE = 100

Analyze = Callable[[PerformanceRequest], PerformanceResponse]


@dataclass
class SyncDeps:
    store: Store
    http: Callable[[], AbstractAsyncContextManager[httpx.AsyncClient]]
    analyze: Analyze  # blocking; run in a thread
    client_id: str
    client_secret: str
    retry: RetryPolicy
    sleep: Sleep
    # Extra PerformanceRequest fields for the report (tests use a tiny metro universe).
    report_options: dict[str, Any] = field(default_factory=dict)


def sync_range(synced_through: date | None, data_through: date) -> tuple[str, date, date]:
    """(mode, since, until) for a sync, given the last synced day and the platform's."""
    if synced_through is None:
        return "backfill", data_through - timedelta(days=BACKFILL_DAYS - 1), data_through
    next_day = min(synced_through, data_through) + timedelta(days=1)
    return "incremental", next_day - timedelta(days=LOOKBACK_DAYS), data_through


def report_window(through: date) -> tuple[date, date]:
    return through - timedelta(days=REPORT_DAYS - 1), through


def report_request(
    store: Store, connection: Connection, through: date, options: dict[str, Any] | None = None
) -> PerformanceRequest:
    sample = SAMPLES[connection.sample_key]
    since, until = report_window(through)
    rows = [
        ResultRow(
            geo=geo,
            spend=round(spend, 2),
            conversions=conversions or None,
            revenue=round(revenue, 2) or None,
        )
        for geo, spend, conversions, revenue in store.window_totals(connection.id, since, until)
    ]
    return PerformanceRequest(
        rows=rows,
        geo_type="metro",
        kpi=sample.kpi,
        preset=sample.preset,
        limit=REPORT_LIMIT,
        **(options or {}),
    )


def summarize(report: PerformanceResponse, since: date, until: date) -> dict[str, Any]:
    spend = sum(m.spend for m in report.metros)
    conversions = sum(m.conversions for m in report.metros)
    revenue = sum(m.revenue for m in report.metros)
    if report.kpi == "roas":
        kpi = revenue / spend if spend else None
    else:
        kpi = spend / conversions if conversions else None
    return {
        "window": [since.isoformat(), until.isoformat()],
        "kpi": report.kpi,
        "kpi_value": round(kpi, 4) if kpi is not None else None,
        "verdict": report.check.verdict,
        "recs": [
            {"code": r.metro_code, "name": r.name, "rank": r.rank}
            for r in report.recommendations.results
        ],
    }


def diff(before: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, Any]:
    """What changed in the report between two syncs (top ``DIFF_TOP`` recommendations)."""
    if before is None:
        return {"first": True}
    old = {r["code"]: r for r in before["recs"][:DIFF_TOP]}
    new = {r["code"]: r for r in after["recs"][:DIFF_TOP]}
    moved = sorted(
        (
            {"code": c, "name": r["name"], "from": old[c]["rank"], "to": r["rank"]}
            for c, r in new.items()
            if c in old and old[c]["rank"] != r["rank"]
        ),
        key=lambda m: (-abs(m["from"] - m["to"]), m["to"]),
    )
    return {
        "first": False,
        "entered": [
            {"code": c, "name": r["name"], "rank": r["rank"]}
            for c, r in new.items()
            if c not in old
        ],
        "left": [
            {"code": c, "name": r["name"], "was": r["rank"]} for c, r in old.items() if c not in new
        ],
        "moved": moved[:5],  # the biggest moves, for the summary
        # Every top-10 recommendation's previous rank (None = new), to mark the cards.
        "previous_rank": {c: old[c]["rank"] if c in old else None for c in new},
        "kpi": {"before": before.get("kpi_value"), "after": after.get("kpi_value")},
        "verdict": {"before": before.get("verdict"), "after": after.get("verdict")},
        "window": {"before": before.get("window"), "after": after.get("window")},
    }


class _Abandoned(Exception):
    """The job was reaped as stale (or deleted) while it ran: stop without a trace.
    Whoever reaped it already recorded the failure."""


async def run_sync(deps: SyncDeps, connection: Connection, sync_id: str) -> None:
    store = deps.store
    stats = {
        "pages": 0,
        "rows": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "rejected": 0,
        "retries": 0,
        "requests": 0,
    }
    running = False

    def alive() -> None:
        if running and not store.heartbeat(sync_id):
            raise _Abandoned

    def emit(kind: str, data: dict[str, Any]) -> None:
        # Retries and token refreshes happen between pages and can take a while
        # (Retry-After, backoff): each one also counts as a heartbeat.
        if kind in ("retry", "auth"):
            alive()
        store.add_event(sync_id, kind, data)

    try:
        async with deps.http() as http:
            client = AdsClient(
                http,
                connection.account_id,
                deps.client_id,
                deps.client_secret,
                on_event=emit,
                retry=deps.retry,
                sleep=deps.sleep,
            )
            account = await client.account()
            data_through = date.fromisoformat(account["data_through"])
            mode, since, until = sync_range(connection.synced_through, data_through)
            if not store.start(sync_id, mode, since, until):
                raise _Abandoned  # reaped while it waited for a slot
            running = True
            emit(
                "started",
                {
                    "mode": mode,
                    "since": since.isoformat(),
                    "until": until.isoformat(),
                    "account": account["name"],
                    "account_id": account["id"],
                },
            )
            async for page in client.insights(since, until, PAGE_SIZE):
                rows = [(r.day, r.geo, r.spend, r.conversions, r.revenue) for r in page.rows]
                up = await asyncio.to_thread(store.upsert_insights, connection.id, rows)
                stats["pages"] += 1
                stats["rows"] += len(rows)
                stats["inserted"] += up.inserted
                stats["updated"] += up.updated
                stats["unchanged"] += up.unchanged
                stats["rejected"] += len(page.rejected)
                stats["retries"], stats["requests"] = client.retries, client.requests
                if not store.heartbeat(sync_id, stats):
                    raise _Abandoned
                days = sorted({r.day for r in page.rows})
                emit(
                    "page",
                    {
                        "page": page.number,
                        "rows": len(rows),
                        "inserted": up.inserted,
                        "updated": up.updated,
                        "unchanged": up.unchanged,
                        "rejected": len(page.rejected),
                        "reasons": page.rejected[:3],
                        "first_day": days[0].isoformat() if days else None,
                        "last_day": days[-1].isoformat() if days else None,
                    },
                )
            emit("fetched", dict(stats))

        through = max(until, connection.synced_through or until)
        req = report_request(store, connection, through, deps.report_options)
        window = report_window(through)
        emit(
            "report",
            {
                "stage": "started",
                "markets": len(req.rows),
                "since": window[0].isoformat(),
                "until": window[1].isoformat(),
            },
        )
        alive()
        report = await asyncio.to_thread(deps.analyze, req)
        summary = summarize(report, *window)
        changes = diff(store.last_report(connection.id), summary)
        result = {"changes": changes, "summary": summary}
        # The job's status and the connection's progress change together, or not at all.
        if not store.succeed(sync_id, connection.id, through, summary, result):
            raise _Abandoned
        store.add_event(
            sync_id,
            "report",
            {"stage": "done", "verdict": report.check.verdict, "changes": changes},
        )
        store.add_event(sync_id, "done", {"status": "succeeded", "error": None})
    except _Abandoned:
        log.info("sync %s stopped: it was reaped or its connection deleted", sync_id)
    except PlatformAPIError as e:
        store.heartbeat(sync_id, stats)
        store.finish(sync_id, "failed", error=f"The ad platform refused the sync: {e}")
    except (NoUsableMetrosError, UniverseTooSmallError) as e:
        # The data synced; it just can't support a report (yet).
        store.finish(sync_id, "failed", error=f"Synced, but no report: {e}")
    except ValidationError:
        # The window has no rows, or none with the KPI's column (e.g. no conversions).
        store.finish(sync_id, "failed", error="Synced, but the last 28 days can't make a report.")
    except sqlite3.IntegrityError:
        if store.get_connection(connection.id, touch=False) is None:
            log.info("sync %s stopped: its connection was deleted", sync_id)
            return
        log.exception("sync %s failed", sync_id)
        store.finish(sync_id, "failed", error="The sync failed unexpectedly.")
    except asyncio.CancelledError:
        store.finish(sync_id, "failed", error="The server shut down during the sync.")
        raise
    except Exception:
        log.exception("sync %s failed", sync_id)
        store.finish(sync_id, "failed", error="The sync failed unexpectedly.")
