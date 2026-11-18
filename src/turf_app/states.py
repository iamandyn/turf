"""Fill missing states from location, using Overture state boundaries.

Expects a table ``places_out`` (with ``state``, ``lat``, ``lon``) and a table
``us_states`` (``state``, ``class``, ``geometry``) on the connection.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb


@dataclass(frozen=True)
class StateFillReport:
    missing_before: int
    filled_from_land: int
    filled_from_water: int
    missing_after: int
    # Places that have an address state, checked against their location.
    sample_checked: int
    sample_disagree: int


def _count_missing(con: duckdb.DuckDBPyConnection) -> int:
    row = con.execute("SELECT count(*) FROM places_out WHERE state IS NULL").fetchone()
    assert row is not None
    return int(row[0])


def _fill(con: duckdb.DuckDBPyConnection, boundary_class: str) -> int:
    before = _count_missing(con)
    con.execute(
        """
        UPDATE places_out SET state = m.state
        FROM (
            SELECT p.id, min(s.state) AS state
            FROM places_out p
            JOIN us_states s
              ON s.class = $cls AND ST_Contains(s.geometry, ST_Point(p.lon, p.lat))
            WHERE p.state IS NULL
            GROUP BY p.id
        ) m
        WHERE places_out.id = m.id
        """,
        {"cls": boundary_class},
    )
    return before - _count_missing(con)


def fill_states(con: duckdb.DuckDBPyConnection, sample_size: int = 100_000) -> StateFillReport:
    con.execute("INSTALL spatial; LOAD spatial;")
    missing_before = _count_missing(con)

    # Check address states against location on a sample first, before filling,
    # so the sample only contains states that came from addresses.
    # DuckDB only accepts a literal sample size, so it's formatted in as an int.
    row = con.execute(f"""
        WITH sample AS (
            SELECT id, state, lat, lon FROM places_out
            WHERE state IS NOT NULL USING SAMPLE reservoir({int(sample_size)} ROWS) REPEATABLE (42)
        )
        SELECT count(DISTINCT x.id),
               count(DISTINCT x.id) FILTER (WHERE s.state <> x.state)
        FROM sample x
        JOIN us_states s
          ON s.class = 'land' AND ST_Contains(s.geometry, ST_Point(x.lon, x.lat))
    """).fetchone()
    assert row is not None
    sample_checked, sample_disagree = int(row[0]), int(row[1])

    from_land = _fill(con, "land")
    from_water = _fill(con, "maritime")
    return StateFillReport(
        missing_before=missing_before,
        filled_from_land=from_land,
        filled_from_water=from_water,
        missing_after=_count_missing(con),
        sample_checked=sample_checked,
        sample_disagree=sample_disagree,
    )
