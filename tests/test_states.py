"""State filling on toy boundaries: two square 'states' and a strip of water."""

from __future__ import annotations

import duckdb

from turf_app.states import fill_states


def setup(places: list[tuple[str, str | None, float, float]]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("""
        CREATE TABLE us_states AS
        SELECT * FROM (VALUES
            ('AA', 'land',     ST_GeomFromText('POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))')),
            ('BB', 'land',     ST_GeomFromText('POLYGON((10 0, 20 0, 20 10, 10 10, 10 0))')),
            ('AA', 'maritime', ST_GeomFromText('POLYGON((0 -2, 10 -2, 10 0, 0 0, 0 -2))'))
        ) t(state, class, geometry)
    """)
    con.execute("CREATE TABLE places_out (id VARCHAR, state VARCHAR, lat DOUBLE, lon DOUBLE)")
    con.executemany("INSERT INTO places_out VALUES (?, ?, ?, ?)", places)
    return con


def state(con: duckdb.DuckDBPyConnection, id: str) -> str | None:
    row = con.execute("SELECT state FROM places_out WHERE id = ?", [id]).fetchone()
    assert row is not None
    value: str | None = row[0]
    return value


def test_missing_state_filled_from_land() -> None:
    con = setup([("a", None, 5.0, 15.0)])  # lat 5, lon 15 -> inside BB
    report = fill_states(con)
    assert state(con, "a") == "BB"
    assert report.filled_from_land == 1


def test_coastal_place_filled_from_water() -> None:
    con = setup([("pier", None, -1.0, 5.0)])  # in AA's water strip
    report = fill_states(con)
    assert state(con, "pier") == "AA"
    assert report.filled_from_water == 1


def test_existing_state_is_kept_even_if_location_disagrees() -> None:
    # Address says AA, point is in BB. We report it but don't overwrite.
    con = setup([("a", "AA", 5.0, 15.0)])
    report = fill_states(con)
    assert state(con, "a") == "AA"
    assert (report.sample_checked, report.sample_disagree) == (1, 1)


def test_place_outside_every_boundary_stays_missing() -> None:
    con = setup([("sea", None, 50.0, 50.0)])
    report = fill_states(con)
    assert state(con, "sea") is None
    assert report.missing_after == 1
