"""Shared fixture: a small serving database built with the real builder, and an API client.

Twelve synthetic metro areas sit side by side as 1°x1° boxes along latitude 35.
Metro ``i`` (codes 10000-10011) has 100 open restaurants plus ``i`` pet
groomers and ``12 - i`` dog parks, so pet-groomer share rises with ``i`` and
dog-park share falls: rankings are predictable. Metro 10011 is a micro area.
Every preset category appears once, in the micro area (outside the default
universe, so it doesn't skew rankings), so the presets load.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from turf_app.api import create_app
from turf_app.markets import PRESETS
from turf_app.ratelimit import RateLimiter
from turf_app.serving import write_serving_db

N_METROS = 12


def metro_code(i: int) -> str:
    return str(10000 + i)


def _metros() -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for i in range(N_METROS):
        lon = -100 + 2 * i
        kind = "micro" if i == N_METROS - 1 else "metro"
        population = 300_000 + 100_000 * i  # 300k .. 1.4M
        population_2020 = int(population / (1 + i / 100))  # growth rises with i: 0% .. 11%
        rows.append(
            (
                metro_code(i), f"Metro {i}, ST", kind, f"metro {i} st", population,
                population_2020,
                f"POLYGON(({lon} 35, {lon + 1} 35, {lon + 1} 36, {lon} 36, {lon} 35))",
            )
        )  # fmt: skip
    rows[3] = (*rows[3][:1], "Austin-Round Rock-San Marcos, TX", "metro",
               "austin round rock san marcos tx", *rows[3][4:])  # fmt: skip
    return rows


def _places() -> list[tuple[object, ...]]:
    # id, basic_category, taxonomy_primary, operating_status, postcode, lat, lon, is_duplicate
    rows: list[tuple[object, ...]] = []

    def add(i: int, category: str, n: int, status: str | None = "open", dup: bool = False) -> None:
        for k in range(n):
            lon = -100 + 2 * i + 0.2 + (k % 5) * 0.15  # spread inside the box
            # Three ZIPs per metro, all with at least 5 places.
            postcode = f"{70000 + 10 * i + k % 3}-1234"
            rows.append((f"{category}-{i}-{k}-{status}-{dup}", "x", category, status, postcode,
                         35.5 + (k % 3) * 0.1, lon, dup))  # fmt: skip

    for i in range(N_METROS):
        add(i, "restaurant", 100)
        add(i, "pet_groomer", i)
        add(i, "dog_park", N_METROS - i)
        add(i, "pet_groomer", 50, dup=True)  # duplicates never count
        add(i, "pet_groomer", 50, status=None)  # unverified: not open, not counted
    for preset in PRESETS.values():
        for category in preset.categories:
            if category not in ("pet_groomer", "dog_park"):
                add(N_METROS - 1, category, 1)
    # A stray ZIP: one place of metro 1 carries metro 0's ZIP; it must stay with metro 0.
    rows.append(("stray", "x", "restaurant", "open", "70000", 35.5, -100 + 2 * 1 + 0.5, False))
    # A ZIP with too few places in its metro to be exported.
    rows.append(("tiny", "x", "restaurant", "open", "79999", 35.5, -100 + 0.5, False))
    # Outside every metro: counted nowhere.
    rows.append(("rural", "x", "pet_groomer", "open", "80000", 45.0, -100.0, False))
    return rows


@pytest.fixture(scope="session")
def serving_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("serving")
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute(
        "CREATE TABLE p (id VARCHAR, basic_category VARCHAR, taxonomy_primary VARCHAR, "
        "operating_status VARCHAR, postcode VARCHAR, lat DOUBLE, lon DOUBLE, is_duplicate BOOLEAN)"
    )
    con.executemany("INSERT INTO p VALUES (?, ?, ?, ?, ?, ?, ?, ?)", _places())
    con.execute(f"COPY p TO '{d / 'places.parquet'}' (FORMAT parquet)")
    con.execute(
        "CREATE TABLE m (code VARCHAR, name VARCHAR, kind VARCHAR, search_key VARCHAR, "
        "population BIGINT, population_2020 BIGINT, wkt VARCHAR)"
    )
    con.executemany("INSERT INTO m VALUES (?, ?, ?, ?, ?, ?, ?)", _metros())
    con.execute(f"""
        COPY (SELECT * EXCLUDE (wkt), ST_GeomFromText(wkt) AS geometry FROM m)
        TO '{d / "metros.parquet"}' (FORMAT parquet)
    """)
    db = d / "turf.duckdb"
    write_serving_db(d / "places.parquet", d / "metros.parquet", db, "test-release")
    return db


@pytest.fixture(scope="session")
def client(serving_db: Path) -> Iterator[TestClient]:
    # Rate limiting off here; it has its own tests.
    with TestClient(create_app(serving_db, rate_limiter=RateLimiter(rules=()))) as c:
        yield c
