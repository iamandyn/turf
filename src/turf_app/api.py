"""Turf HTTP API.

Run locally:
    TURF_DB=data/2026-11-18.0/turf.duckdb uv run uvicorn turf_app.api:app --reload
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

import duckdb
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel

from turf_app.normalize import normalize_text

# Similarity needed for a fuzzy (typo) match, e.g. "starbuks" -> "starbucks".
FUZZY_MIN_SIMILARITY = 0.88


class Meta(BaseModel):
    release: str
    built_at_utc: datetime
    places: int
    chains: int


class ChainSummary(BaseModel):
    chain_key: str
    display_name: str
    category: str | None
    n_open: int
    n_total: int


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


def create_app(db_path: Path | None = None) -> FastAPI:
    path = db_path or Path(os.environ.get("TURF_DB", "data/turf.duckdb"))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        con = duckdb.connect(str(path), read_only=True)
        app.state.db = con
        try:
            yield
        finally:
            con.close()

    app = FastAPI(title="Turf", lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @app.get("/healthz")
    def healthz(db: Db) -> dict[str, str]:
        db.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.get("/api/meta")
    def meta(db: Db) -> Meta:
        row = db.execute("""
            SELECT m.release, m.built_at_utc,
                   (SELECT count(*) FROM places), (SELECT count(*) FROM chains)
            FROM meta m
        """).fetchone()
        assert row is not None
        return Meta(release=row[0], built_at_utc=row[1], places=row[2], chains=row[3])

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

    @app.get("/api/chains/{chain_key}/locations")
    def chain_locations(
        db: Db,
        chain_key: str,
        include_unverified: bool = False,
        state: Annotated[str | None, Query(pattern="^[A-Za-z]{2}$")] = None,
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
            ORDER BY state, locality, id
            """,
            {
                "chain": chain_key,
                "unverified": include_unverified,
                "state": state.upper() if state else None,
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

    return app


def __getattr__(name: str) -> FastAPI:
    # `uvicorn turf_app.api:app` builds the app lazily, so importing this
    # module (e.g. in tests) doesn't need a database file.
    if name == "app":
        return create_app()
    raise AttributeError(name)
