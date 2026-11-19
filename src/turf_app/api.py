"""Turf HTTP API: market selection for consumer brands.

Run locally:
    TURF_DB=data/2026-11-18.0/turf.duckdb uv run uvicorn turf_app.api:app --reload
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

import duckdb
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import RequestResponseEndpoint

from turf_app import briefs, mcp_server, samples
from turf_app.markets import (
    PRESETS,
    MarketRequest,
    MarketResponse,
    UniverseTooSmallError,
    UnknownCategoriesError,
    UnknownSeedsError,
    rank_markets,
    zip_rows,
)
from turf_app.normalize import normalize_text
from turf_app.performance import (
    NoUsableMetrosError,
    PerformanceRequest,
    PerformanceResponse,
    analyze,
)
from turf_app.ratelimit import RateLimiter

WEB_DIR = Path(__file__).parent / "web"

# Our own frontend files, referenced from index.html as "/<name>". The page is
# served with a content hash on each ("/app.js?v=3f9c..."): Cloudflare gives
# .js/.css a 4-hour browser cache whatever the origin says, so without it a
# deploy could pair the new page with yesterday's script. Vendored MapLibre
# already has its version in the path.
VERSIONED_ASSETS = ("theme.js", "styles.css", "app.js")
IMMUTABLE = "public, max-age=31536000, immutable"


def _asset_version(name: str) -> str:
    return hashlib.sha256((WEB_DIR / name).read_bytes()).hexdigest()[:12]


def render_index() -> str:
    """index.html with versioned asset URLs; re-read each time, so edits show up in dev."""
    html = (WEB_DIR / "index.html").read_text()
    for name in VERSIONED_ASSETS:
        ref = f'"/{name}"'
        if ref not in html:
            raise RuntimeError(f"index.html no longer references {ref}")
        html = html.replace(ref, f'"/{name}?v={_asset_version(name)}"')
    return html


# The page only loads its own scripts; the basemap's tiles, fonts, and sprites
# come from OpenFreeMap. MapLibre runs its worker from a blob: URL and sets
# inline styles on map elements. Cloudflare Web Analytics is enabled on the
# zone and injects its beacon into HTML responses, so its script and report
# endpoint are allowed too (turn off injection in Cloudflare to drop them).
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' https://static.cloudflareinsights.com",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob: https://tiles.openfreemap.org",
        "connect-src 'self' https://tiles.openfreemap.org https://cloudflareinsights.com",
        "worker-src blob:",
        "child-src blob:",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
    ]
)

# Similarity needed for a fuzzy (typo) match, e.g. "pet grommer" -> "pet groomer".
FUZZY_MIN_SIMILARITY = 0.88


class Meta(BaseModel):
    commit: str
    release: str
    built_at_utc: datetime
    metros: int
    categories: int
    briefs: int


class PresetOut(BaseModel):
    key: str
    label: str
    example: str
    categories: list[str]
    has_briefs: bool


class CategoryResult(BaseModel):
    key: str
    label: str
    parent: str | None
    n_open: int


class Metro(BaseModel):
    metro_code: str
    name: str
    kind: Literal["metro", "micro"]
    population: int
    growth_pct: float
    # Extent of the area's places, to frame it on the map.
    bounds: tuple[float, float, float, float]  # min_lon, min_lat, max_lon, max_lat


_METRO_COLUMNS = (
    "metro_code, name, kind, population, growth_pct, min_lon, min_lat, max_lon, max_lat"
)


def _metro(row: tuple[object, ...]) -> Metro:
    code, name, kind, population, growth, *bounds = row
    return Metro.model_validate(
        {"metro_code": code, "name": name, "kind": kind, "population": population,
         "growth_pct": growth, "bounds": bounds}
    )  # fmt: skip


def _cursor(request: Request) -> Iterator[duckdb.DuckDBPyConnection]:
    # One cursor per request: DuckDB connections aren't safe to share across
    # threads, cursors are.
    cur: duckdb.DuckDBPyConnection = request.app.state.db.cursor()
    try:
        yield cur
    finally:
        cur.close()


# Module level so FastAPI can resolve it from the string annotations.
Db = Annotated[duckdb.DuckDBPyConnection, Depends(_cursor)]


def _tiered_search(table: str, columns: str, order: str) -> str:
    """Prefix, then word-start, then substring, then fuzzy (typo) matches on search_key."""
    return f"""
        SELECT {columns} FROM (
            SELECT *,
                   CASE WHEN starts_with(search_key, $q) THEN 1
                        WHEN contains(search_key, ' ' || $q) THEN 2
                        WHEN contains(search_key, $q) THEN 3
                        ELSE 4 END AS tier,
                   jaro_winkler_similarity(search_key, $q) AS sim
            FROM {table}
        )
        WHERE tier < 4 OR sim >= $min_sim
        ORDER BY tier, {order}
        LIMIT $limit
    """


def create_app(db_path: Path | None = None, rate_limiter: RateLimiter | None = None) -> FastAPI:
    path = db_path or Path(os.environ.get("TURF_DB", "data/turf.duckdb"))
    # Baked into the image at build time, so CI can check the live site runs
    # the commit it just deployed.
    commit = os.environ.get("TURF_COMMIT", "dev")
    limiter = rate_limiter or RateLimiter()
    # Set in production to the header the reverse proxy writes the real client
    # IP into. Unset (local dev, tests), the TCP peer address is used.
    client_ip_header = os.environ.get("TURF_CLIENT_IP_HEADER")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The served file is a few MB of aggregates; a small cap is plenty.
        con = duckdb.connect(
            str(path),
            read_only=True,
            config={
                "memory_limit": os.environ.get("TURF_DB_MEMORY", "512MB"),
                "threads": int(os.environ.get("TURF_DB_THREADS", "2")),
            },
        )
        known = {r[0] for r in con.execute("SELECT category_key FROM categories").fetchall()}
        for preset in PRESETS.values():
            missing = set(preset.categories) - known
            if missing:
                raise RuntimeError(f"preset {preset.key}: unknown categories {sorted(missing)}")
        app.state.db = con
        app.state.briefs = briefs.load()
        app.state.samples = {}
        mcp_server.start(mcp)
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            con.close()

    app = FastAPI(title="Turf", lifespan=lifespan)
    # MCP tools for AI agents (see mcp_server.py), sharing the database.
    mcp = mcp_server.build_mcp(lambda: app.state.db.cursor(), lambda: app.state.briefs)
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @app.middleware("http")
    async def rate_limit(request: Request, call_next: RequestResponseEndpoint) -> Response:
        client = (client_ip_header and request.headers.get(client_ip_header)) or (
            request.client.host if request.client else "unknown"
        )
        retry_after = limiter.check(client, request.method, request.url.path)
        if retry_after is not None:
            return JSONResponse(
                {"detail": "Too many requests; try again shortly."},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        path = request.url.path
        if not (path.startswith("/api/") or path.startswith("/docs") or path == "/openapi.json"):
            response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
            if path.lstrip("/") in VERSIONED_ASSETS and request.query_params.get("v"):
                # A versioned URL's content never changes; a new version is a new URL.
                response.headers["Cache-Control"] = IMMUTABLE
            else:
                # The page and unversioned files: always revalidate (cheap, ETags).
                response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @app.get("/healthz")
    def healthz(db: Db) -> dict[str, str]:
        db.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.get("/api/meta")
    def meta(db: Db, request: Request) -> Meta:
        row = db.execute("""
            SELECT m.release, m.built_at_utc,
                   (SELECT count(*) FROM metros), (SELECT count(*) FROM categories)
            FROM meta m
        """).fetchone()
        assert row is not None
        return Meta(
            commit=commit,
            release=row[0],
            built_at_utc=row[1],
            metros=row[2],
            categories=row[3],
            briefs=len(request.app.state.briefs),
        )

    @app.get("/api/presets")
    def presets(request: Request) -> list[PresetOut]:
        with_briefs = {key[0] for key in request.app.state.briefs}
        return [
            PresetOut(
                key=p.key,
                label=p.label,
                example=p.example,
                categories=list(p.categories),
                has_briefs=p.key in with_briefs,
            )
            for p in PRESETS.values()
        ]

    @app.get("/api/categories/search")
    def search_categories(
        db: Db,
        q: Annotated[str, Query(min_length=2, max_length=80)],
        limit: Annotated[int, Query(ge=1, le=25)] = 10,
    ) -> list[CategoryResult]:
        """Overture categories to use as signals; bigger categories first within a tier."""
        query = normalize_text(q)
        if len(query) < 2:
            return []
        rows = db.execute(
            _tiered_search("categories", "category_key, label, parent, n_open", "n_open DESC"),
            {"q": query, "min_sim": FUZZY_MIN_SIMILARITY, "limit": limit},
        ).fetchall()
        return [CategoryResult(key=r[0], label=r[1], parent=r[2], n_open=r[3]) for r in rows]

    @app.get("/api/metros/search")
    def search_metros(
        db: Db,
        q: Annotated[str, Query(min_length=2, max_length=80)],
        limit: Annotated[int, Query(ge=1, le=25)] = 10,
    ) -> list[Metro]:
        """Census metro and micro areas; metros before micros, then bigger first."""
        query = normalize_text(q)
        if len(query) < 2:
            return []
        rows = db.execute(
            _tiered_search("metros", _METRO_COLUMNS, "kind, population DESC"),
            {"q": query, "min_sim": FUZZY_MIN_SIMILARITY, "limit": limit},
        ).fetchall()
        return [_metro(r) for r in rows]

    @app.get("/api/metros/{metro_code}")
    def get_metro(db: Db, metro_code: str) -> Metro:
        row = db.execute(
            f"SELECT {_METRO_COLUMNS} FROM metros WHERE metro_code = ?", [metro_code]
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Unknown metro area: {metro_code}")
        return _metro(row)

    def _markets(db: duckdb.DuckDBPyConnection, req: MarketRequest) -> MarketResponse:
        try:
            return rank_markets(db, req)
        except UnknownCategoriesError as e:
            raise HTTPException(status_code=404, detail=f"Unknown category: {e}") from e
        except UnknownSeedsError as e:
            raise HTTPException(status_code=404, detail=f"Unknown metro area: {e}") from e
        except UniverseTooSmallError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    @app.post("/api/markets")
    def markets(db: Db, req: MarketRequest) -> MarketResponse:
        return _markets(db, req)

    def _csv_response(
        db: duckdb.DuckDBPyConnection,
        result: MarketResponse,
        format: str,
        slug: str,
        status: dict[str, str] | None = None,
    ) -> Response:
        """``metros``: the ranked list with reasons. ``zips``: every ZIP code in
        the ranked metros, one per row, for pasting into Meta or Google Ads."""
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        if format == "metros":
            header = ["rank", "metro", "cbsa_code", "kind", "population", "growth_2020_25_pct",
                      "match", "zip_count", "top_signals"]  # fmt: skip
            writer.writerow([*header, "brand_status"] if status else header)
            for r in result.results:
                top = "; ".join(f"{x.label} {x.ratio:g}x typical" for x in r.reasons[:3])
                row = [r.rank, r.name, r.metro_code, r.kind, r.population, r.growth_pct,
                       "" if r.match is None else r.match, r.zip_count, top]  # fmt: skip
                writer.writerow([*row, status.get(r.metro_code, "")] if status else row)
        else:
            names = {r.metro_code: (r.rank, r.name) for r in result.results}
            writer.writerow(["zip", "country", "metro", "metro_rank"])
            for code, zip_code, _ in zip_rows(db, list(names)):
                writer.writerow([zip_code, "US", names[code][1], names[code][0]])
        slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="turf-{slug}-{format}.csv"'},
        )

    @app.post("/api/markets/export")
    def export_markets(
        db: Db, req: MarketRequest, format: Literal["metros", "zips"] = "metros"
    ) -> Response:
        result = _markets(db, req)
        mode = "lookalikes" if req.seeds else "top"
        return _csv_response(db, result, format, f"{req.preset or 'custom'}-{mode}")

    def _performance(db: duckdb.DuckDBPyConnection, req: PerformanceRequest) -> PerformanceResponse:
        try:
            return analyze(db, req)
        except UnknownCategoriesError as e:
            raise HTTPException(status_code=404, detail=f"Unknown category: {e}") from e
        except (NoUsableMetrosError, UniverseTooSmallError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    @app.post("/api/performance")
    def performance(db: Db, req: PerformanceRequest) -> PerformanceResponse:
        """Recommendations from a brand's own results by geography (see performance.py).
        The rows are used for this request only and never stored."""
        return _performance(db, req)

    @app.post("/api/performance/export")
    def export_performance(
        db: Db, req: PerformanceRequest, format: Literal["metros", "zips"] = "metros"
    ) -> Response:
        result = _performance(db, req)
        status = {s.metro_code: s.status.replace("_", " ") for s in result.status}
        return _csv_response(
            db, result.recommendations, format, f"{req.preset or 'custom'}-recommended", status
        )

    @app.get("/api/samples")
    def list_samples() -> list[samples.SampleInfo]:
        """Fictional sample brands with synthetic results (see samples.py)."""
        return [samples.info(s) for s in samples.SAMPLES.values()]

    @app.get("/api/samples/{key}")
    def get_sample(db: Db, request: Request, key: str) -> samples.SampleData:
        sample = samples.SAMPLES.get(key)
        if sample is None:
            raise HTTPException(status_code=404, detail=f"Unknown sample: {key}")
        cache: dict[str, samples.SampleData] = request.app.state.samples
        if key not in cache:  # deterministic, so generated once per process
            cache[key] = samples.generate(db, sample)
        return cache[key]

    @app.get("/api/briefs/{preset}/{metro_code}")
    def get_brief(request: Request, preset: str, metro_code: str) -> briefs.Brief:
        """A pre-written creative brief for a preset in a metro (see briefs.py)."""
        loaded: dict[tuple[str, str], briefs.Brief] = request.app.state.briefs
        brief = loaded.get((preset, metro_code))
        if brief is None:
            raise HTTPException(status_code=404, detail="No brief for this preset and metro yet.")
        return brief

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def index() -> HTMLResponse:
        return HTMLResponse(render_index())

    # Before the static mount, so /mcp isn't treated as a file.
    app.router.routes.append(mcp_server.mcp_route(mcp))

    # Last, so API routes and the page win.
    app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
    return app


def __getattr__(name: str) -> FastAPI:
    # `uvicorn turf_app.api:app` builds the app lazily, so importing this
    # module (e.g. in tests) doesn't need a database file.
    if name == "app":
        return create_app()
    raise AttributeError(name)


__all__ = ["create_app", "render_index"]
