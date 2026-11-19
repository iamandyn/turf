"""Turf HTTP API.

Run locally:
    TURF_DB=data/2026-11-18.0/turf.duckdb uv run uvicorn turf_app.api:app --reload
"""

from __future__ import annotations

import hashlib
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

from turf_app.normalize import normalize_text
from turf_app.ratelimit import RateLimiter
from turf_app.targets import (
    ChangesUnavailableError,
    ExportFormat,
    TargetRequest,
    TargetResponse,
    TooManyTargetsError,
    UnknownCategoryError,
    UnknownChainError,
    UnknownMetroError,
    build_targets,
    to_csv,
)

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

# Chains and categories as one searchable list; $kind optionally limits it to one.
_SEARCH_CANDIDATES = """
    WITH candidates AS (
        SELECT 'chain' AS kind, chain_key AS key, chain_key AS search_key,
               display_name, categories[1] AS detail, n_open, n_total
        FROM chains WHERE $kind IS NULL OR $kind = 'chain'
        UNION ALL
        SELECT 'category', category_key, search_key, label, parent, n_open, n_total
        FROM categories WHERE $kind IS NULL OR $kind = 'category'
    )
"""

# Similarity needed for a fuzzy (typo) match, e.g. "starbuks" -> "starbucks".
FUZZY_MIN_SIMILARITY = 0.88


class Meta(BaseModel):
    commit: str
    release: str
    # The release "new since" compares against; null when the file has no change data.
    previous_release: str | None
    built_at_utc: datetime
    places: int
    chains: int


class ChainSummary(BaseModel):
    chain_key: str
    display_name: str
    category: str | None
    n_open: int
    n_total: int


class SearchResult(BaseModel):
    kind: Literal["chain", "category"]
    key: str
    display_name: str
    detail: str | None
    n_open: int
    n_total: int


class Metro(BaseModel):
    metro_code: str
    name: str
    kind: Literal["metro", "micro"]
    n_open: int
    n_total: int
    # Extent of the area's places, to frame it on the map.
    bounds: tuple[float, float, float, float]  # min_lon, min_lat, max_lon, max_lat


_METRO_COLUMNS = "metro_code, name, kind, n_open, n_total, min_lon, min_lat, max_lon, max_lat"


def _metro(row: tuple[object, ...]) -> Metro:
    code, name, kind, n_open, n_total, *bounds = row
    return Metro.model_validate(
        {"metro_code": code, "name": name, "kind": kind, "n_open": n_open, "n_total": n_total,
         "bounds": bounds}
    )  # fmt: skip


class Location(BaseModel):
    id: str
    name: str
    status: str | None
    confidence: float
    address: str | None
    locality: str | None
    state: str | None
    postcode: str | None
    lat: float
    lon: float


class ChainLocations(BaseModel):
    chain: ChainSummary
    include_unverified: bool
    locations: list[Location]


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


def _previous_release(con: duckdb.DuckDBPyConnection) -> str | None:
    """The change view's baseline, if this serving file has one (schema v4 built
    with changes). Older files work without it, so code can deploy before data."""
    has = con.execute("""
        SELECT count(*) = 2 FROM information_schema.columns
        WHERE (table_name = 'meta' AND column_name = 'previous_release')
           OR (table_name = 'places' AND column_name = 'is_new')
    """).fetchone()
    if not has or not has[0]:
        return None
    row = con.execute("SELECT previous_release FROM meta").fetchone()
    return str(row[0]) if row and row[0] else None


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
        # Each uvicorn worker opens its own connection; cap DuckDB so two
        # workers fit inside the container's memory limit.
        con = duckdb.connect(
            str(path),
            read_only=True,
            config={
                "memory_limit": os.environ.get("TURF_DB_MEMORY", "1GB"),
                "threads": int(os.environ.get("TURF_DB_THREADS", "2")),
            },
        )
        app.state.db = con
        app.state.previous_release = _previous_release(con)
        try:
            yield
        finally:
            con.close()

    app = FastAPI(title="Turf", lifespan=lifespan)
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
                   (SELECT count(*) FROM places), (SELECT count(*) FROM chains)
            FROM meta m
        """).fetchone()
        assert row is not None
        return Meta(
            commit=commit,
            release=row[0],
            previous_release=request.app.state.previous_release,
            built_at_utc=row[1],
            places=row[2],
            chains=row[3],
        )

    @app.get("/api/chains/search")
    def search_chains(
        db: Db,
        q: Annotated[str, Query(min_length=2, max_length=80)],
        limit: Annotated[int, Query(ge=1, le=25)] = 10,
    ) -> list[ChainSummary]:
        query = normalize_text(q)
        if len(query) < 2:
            return []
        # Rank prefix (including exact), then word-start, then substring, then
        # fuzzy matches; within a tier, bigger chains first. Exact matches share
        # the prefix tier so "orangetheory" finds Orangetheory Fitness (1,275
        # locations) before a 3-location chain named just "Orangetheory".
        rows = db.execute(
            """
            SELECT chain_key, display_name, category, n_open, n_total FROM (
                SELECT *, categories[1] AS category,
                       CASE WHEN starts_with(chain_key, $q) THEN 1
                            WHEN contains(chain_key, ' ' || $q) THEN 2
                            WHEN contains(chain_key, $q) THEN 3
                            ELSE 4 END AS tier,
                       jaro_winkler_similarity(chain_key, $q) AS sim
                FROM chains
            )
            WHERE tier < 4 OR sim >= $min_sim
            ORDER BY tier, n_open DESC, sim DESC
            LIMIT $limit
            """,
            {"q": query, "min_sim": FUZZY_MIN_SIMILARITY, "limit": limit},
        ).fetchall()
        return [
            ChainSummary(
                chain_key=r[0], display_name=r[1], category=r[2], n_open=r[3], n_total=r[4]
            )
            for r in rows
        ]

    @app.get("/api/search")
    def search(
        db: Db,
        q: Annotated[str, Query(min_length=2, max_length=80)],
        kind: Literal["chain", "category"] | None = None,
        limit: Annotated[int, Query(ge=1, le=25)] = 10,
    ) -> list[SearchResult]:
        """Chains and categories in one ranked list (same tiers as chain search)."""
        query = normalize_text(q)
        if len(query) < 2:
            return []
        rows = db.execute(
            _SEARCH_CANDIDATES
            + """
            SELECT kind, key, display_name, detail, n_open, n_total FROM (
                SELECT *,
                       CASE WHEN starts_with(search_key, $q) THEN 1
                            WHEN contains(search_key, ' ' || $q) THEN 2
                            WHEN contains(search_key, $q) THEN 3
                            ELSE 4 END AS tier,
                       jaro_winkler_similarity(search_key, $q) AS sim
                FROM candidates
            )
            WHERE tier < 4 OR sim >= $min_sim
            ORDER BY tier, n_open DESC, sim DESC
            LIMIT $limit
            """,
            {"q": query, "kind": kind, "min_sim": FUZZY_MIN_SIMILARITY, "limit": limit},
        ).fetchall()
        tokens = query.split()
        if not rows and len(tokens) > 1:
            # Nothing matched the whole phrase ("dog daycare"): fall back to
            # entries where any word starts a word, most words matched first.
            # Tokens are normalized to [a-z0-9], so they're safe in a regex.
            rows = db.execute(
                _SEARCH_CANDIDATES
                + """
                SELECT kind, key, display_name, detail, n_open, n_total FROM (
                    SELECT *,
                           len(list_filter(
                               $tokens, lambda t: regexp_matches(search_key, '(^| )' || t)
                           )) AS hits
                    FROM candidates
                )
                WHERE hits > 0
                ORDER BY hits DESC, n_open DESC
                LIMIT $limit
                """,
                {"tokens": tokens, "kind": kind, "limit": limit},
            ).fetchall()
        fields = list(SearchResult.model_fields)
        return [SearchResult(**dict(zip(fields, r, strict=True))) for r in rows]

    @app.get("/api/metros/search")
    def search_metros(
        db: Db,
        q: Annotated[str, Query(min_length=2, max_length=80)],
        limit: Annotated[int, Query(ge=1, le=25)] = 10,
    ) -> list[Metro]:
        """Census metro and micro areas; same tiers as chain search, metros before micros."""
        query = normalize_text(q)
        if len(query) < 2:
            return []
        rows = db.execute(
            f"""
            SELECT {_METRO_COLUMNS} FROM (
                SELECT *,
                       CASE WHEN starts_with(search_key, $q) THEN 1
                            WHEN contains(search_key, ' ' || $q) THEN 2
                            WHEN contains(search_key, $q) THEN 3
                            ELSE 4 END AS tier,
                       jaro_winkler_similarity(search_key, $q) AS sim
                FROM metros
            )
            WHERE tier < 4 OR sim >= $min_sim
            ORDER BY tier, kind, n_total DESC
            LIMIT $limit
            """,
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

    @app.get("/api/chains/{chain_key}/locations")
    def chain_locations(
        db: Db,
        chain_key: str,
        include_unverified: bool = False,
        state: Annotated[str | None, Query(pattern="^[A-Za-z]{2}$")] = None,
        metro: Annotated[str | None, Query(pattern="^[0-9]{5}$")] = None,
    ) -> ChainLocations:
        chain_row = db.execute(
            """SELECT chain_key, display_name, categories[1], n_open, n_total
               FROM chains WHERE chain_key = ?""",
            [chain_key],
        ).fetchone()
        if chain_row is None:
            raise HTTPException(status_code=404, detail=f"Unknown chain: {chain_key}")
        rows = db.execute(
            """
            SELECT id, name, status, confidence, address, locality, state, postcode, lat, lon
            FROM places
            WHERE chain_key = $chain
              AND ($unverified OR status = 'open')
              AND ($state IS NULL OR state = $state)
              AND ($metro IS NULL OR metro = $metro)
            ORDER BY state, locality, id
            """,
            {
                "chain": chain_key,
                "unverified": include_unverified,
                "state": state.upper() if state else None,
                "metro": metro,
            },
        ).fetchall()
        fields = list(Location.model_fields)
        return ChainLocations(
            chain=ChainSummary(
                chain_key=chain_row[0],
                display_name=chain_row[1],
                category=chain_row[2],
                n_open=chain_row[3],
                n_total=chain_row[4],
            ),
            include_unverified=include_unverified,
            locations=[Location(**dict(zip(fields, r, strict=True))) for r in rows],
        )

    def _targets_or_404(
        db: duckdb.DuckDBPyConnection, req: TargetRequest, request: Request
    ) -> TargetResponse:
        try:
            return build_targets(db, req, request.app.state.previous_release)
        except UnknownChainError as e:
            raise HTTPException(status_code=404, detail=f"Unknown chain: {e}") from e
        except UnknownCategoryError as e:
            raise HTTPException(status_code=404, detail=f"Unknown category: {e}") from e
        except UnknownMetroError as e:
            raise HTTPException(status_code=404, detail=f"Unknown metro area: {e}") from e
        except (TooManyTargetsError, ChangesUnavailableError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    @app.post("/api/targets")
    def targets(db: Db, req: TargetRequest, request: Request) -> TargetResponse:
        return _targets_or_404(db, req, request)

    @app.post("/api/targets/export")
    def export_targets(
        db: Db, req: TargetRequest, request: Request, format: ExportFormat = ExportFormat.generic
    ) -> Response:
        result = _targets_or_404(db, req, request)
        campaign = req.campaign_name or f"Turf - {req.target_key}"
        # Keep the filename header safe whatever the key contains.
        slug = re.sub(r"[^a-z0-9]+", "-", req.target_key.lower()).strip("-") or "targets"
        return Response(
            content=to_csv(result, format, campaign),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="turf-{slug}-{format.value}.csv"'
            },
        )

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def index() -> HTMLResponse:
        return HTMLResponse(render_index())

    # Last, so API routes and the page win.
    app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
    return app


def __getattr__(name: str) -> FastAPI:
    # `uvicorn turf_app.api:app` builds the app lazily, so importing this
    # module (e.g. in tests) doesn't need a database file.
    if name == "app":
        return create_app()
    raise AttributeError(name)
