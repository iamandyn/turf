"""Shared fixture: a small serving database built with the real builder, and an API client."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from turf_app.api import create_app
from turf_app.ratelimit import RateLimiter
from turf_app.serving import write_serving_db

PLACES = [
    # id, name, chain_key, category, confidence, status, state, lat, lon, is_duplicate
    ("sb1", "Starbucks", "starbucks", "coffee_shop", 0.99, "open", "TX", 30.27, -97.74, False),
    ("sb2", "Starbucks Coffee Company", "starbucks", "coffee_shop", 0.9, "open", "CA", 37.7, -122.4, False),
    ("sb3", "Starbucks", "starbucks", "coffee_shop", 0.8, None, "TX", 30.3, -97.7, False),
    ("sb4", "Starbucks", "starbucks", "coffee_shop", 0.7, "open", "TX", 30.31, -97.71, True),
    ("otf1", "Orangetheory Fitness", "orangetheory fitness", "gym", 0.99, "open", "TX", 30.2, -97.8, False),
    ("otf2", "Orangetheory Fitness", "orangetheory fitness", "gym", 0.99, "open", "CA", 34.0, -118.2, False),
    ("ot1", "Orangetheory", "orangetheory", "gym", 0.9, "open", "TX", 30.5, -97.9, False),
    ("pf1", "Planet Fitness", "planet fitness", "gym", 0.99, "open", "TX", 30.4, -97.7, False),
    ("ss1", "Starship Bagel", "starship bagel", "bakery", 0.9, "open", "TX", 30.1, -97.6, False),
    ("x1", "Old Shop", "old shop", "retail", 0.9, None, "TX", 30.0, -97.0, False),
    # "Your own" chain for exclusion tests. sb1 is ~0.35 mi from dk1; sb3 is
    # ~0.6 mi from dk2, which is unverified; sb2 is ~7 mi from dk3.
    ("dk1", "Dunkin'", "dunkin", "coffee_shop", 0.99, "open", "TX", 30.275, -97.74, False),
    ("dk2", "Dunkin'", "dunkin", "coffee_shop", 0.9, None, "TX", 30.3, -97.71, False),
    ("dk3", "Dunkin'", "dunkin", "coffee_shop", 0.9, "open", "CA", 37.8, -122.4, False),
    ("ind", "Joe's Coffee", None, "coffee_shop", 0.9, "open", "TX", 30.0, -97.5, False),
]  # fmt: skip

# Two boxes standing in for Census metro areas: Austin (sb1, sb3, otf1, ... are
# inside) and San Francisco (sb2, dk3). otf2 (Los Angeles) and x1 (30.0, -97.0)
# are in neither.
METROS = [
    ("12420", "Austin-Round Rock-San Marcos, TX", "metro", "austin round rock san marcos tx",
     "POLYGON((-98.2 29.9, -97.3 29.9, -97.3 30.6, -98.2 30.6, -98.2 29.9))"),
    ("41860", "San Francisco-Oakland-Fremont, CA", "metro", "san francisco oakland fremont ca",
     "POLYGON((-122.6 37.5, -122.0 37.5, -122.0 38.0, -122.6 38.0, -122.6 37.5))"),
    ("99999", "Empty Micro, TX", "micro", "empty micro tx",
     "POLYGON((-100 40, -99 40, -99 41, -100 41, -100 40))"),
]  # fmt: skip

CHAINS = [
    ("starbucks", "Starbucks", ["coffee_shop"]),
    ("orangetheory fitness", "Orangetheory Fitness", ["gym"]),
    ("orangetheory", "Orangetheory", ["gym"]),
    ("planet fitness", "Planet Fitness", ["gym"]),
    ("starship bagel", "Starship Bagel", ["bakery"]),
    ("dunkin", "Dunkin'", ["coffee_shop"]),
    ("old shop", "Old Shop", ["retail"]),  # no open locations -> not served
]


@pytest.fixture(scope="session")
def serving_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("serving")
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE p (id VARCHAR, name VARCHAR, chain_key VARCHAR, basic_category VARCHAR, "
        "confidence DOUBLE, operating_status VARCHAR, state VARCHAR, lat DOUBLE, lon DOUBLE, "
        "is_duplicate BOOLEAN)"
    )
    con.executemany("INSERT INTO p VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", PLACES)
    con.execute(f"""
        COPY (SELECT *, basic_category AS taxonomy_primary, id || ' Main St' AS address, 'Austin' AS locality, '78701' AS postcode FROM p)
        TO '{d / "places.parquet"}' (FORMAT parquet)
    """)
    con.execute("CREATE TABLE c (chain_key VARCHAR, display_name VARCHAR, categories VARCHAR[])")
    con.executemany("INSERT INTO c VALUES (?, ?, ?)", CHAINS)
    con.execute(f"COPY c TO '{d / 'chains.parquet'}' (FORMAT parquet)")
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute(
        "CREATE TABLE m (code VARCHAR, name VARCHAR, kind VARCHAR, search_key VARCHAR, wkt VARCHAR)"
    )
    con.executemany("INSERT INTO m VALUES (?, ?, ?, ?, ?)", METROS)
    con.execute(f"""
        COPY (SELECT code, name, kind, search_key, ST_GeomFromText(wkt) AS geometry FROM m)
        TO '{d / "metros.parquet"}' (FORMAT parquet)
    """)
    db = d / "turf.duckdb"
    write_serving_db(
        d / "places.parquet", d / "chains.parquet", d / "metros.parquet", db, "test-release"
    )
    return db


@pytest.fixture(scope="session")
def client(serving_db: Path) -> Iterator[TestClient]:
    # Rate limiting off here; it has its own tests.
    with TestClient(create_app(serving_db, rate_limiter=RateLimiter(rules=()))) as c:
        yield c
