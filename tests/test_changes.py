"""New/gone chain locations between two releases (turf_app.changes)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import duckdb

from turf_app.changes import build_changes

COLS = (
    "id VARCHAR, name VARCHAR, chain_key VARCHAR, operating_status VARCHAR, address VARCHAR, "
    "locality VARCHAR, state VARCHAR, postcode VARCHAR, lat DOUBLE, lon DOUBLE, is_duplicate BOOLEAN"
)


def write(path: Path, rows: Sequence[tuple[object, ...]]) -> Path:
    con = duckdb.connect()
    con.execute(f"CREATE TABLE t ({COLS})")
    con.executemany(
        "INSERT INTO t VALUES (?, ?, ?, 'open', ?, 'Austin', 'TX', '78701', ?, ?, ?)",
        [(r[0], r[1], r[2], r[6] if len(r) > 6 else f"{r[0]} St", *r[3:6]) for r in rows],
    )
    con.execute(f"COPY t TO '{path}' (FORMAT parquet)")
    return path


def test_new_and_gone_ignore_id_churn(tmp_path: Path) -> None:
    previous = write(
        tmp_path / "prev.parquet",
        [
            ("a1", "Gym", "gym", 30.0, -97.0, False),  # same ID next month
            ("a2", "Gym", "gym", 30.1, -97.0, False),  # new ID next month, ~50 m away
            ("a3", "Gym", "gym", 30.2, -97.0, False),  # gone
            ("c1", "Cafe", "cafe", 30.3, -97.0, False),  # another chain at the spot of g9
            ("ind", "Joe's", None, 30.4, -97.0, False),  # independent: not compared
        ],
    )
    current = write(
        tmp_path / "cur.parquet",
        [
            ("a1", "Gym", "gym", 30.0, -97.0, False),
            ("b2", "Gym", "gym", 30.1004, -97.0, False),  # ~45 m from a2: the same gym
            ("g9", "Gym", "gym", 30.3, -97.0, False),  # new, though a Cafe was right there
            ("g8", "Gym", "gym", 30.0, -97.003, False),  # new: ~290 m from a1, beyond 150 m
            ("dup", "Gym", "gym", 30.5, -97.0, True),  # duplicates are ignored
            ("c1", "Cafe", "cafe", 30.3, -97.0, False),
        ],
    )
    out = tmp_path / "changes.parquet"
    con = duckdb.connect()
    report = build_changes(con, current, previous, out)
    rows = con.execute(f"SELECT change, id FROM '{out}' ORDER BY change, id").fetchall()
    assert rows == [("gone", "a3"), ("new", "g8"), ("new", "g9")]
    assert (report.new, report.gone, report.same_id, report.matched_nearby) == (2, 1, 2, 1)
    assert (report.previous_chain_locations, report.current_chain_locations) == (4, 5)


def test_same_address_with_shifted_coordinates_is_unchanged(tmp_path: Path) -> None:
    previous = write(
        tmp_path / "prev.parquet",
        [("s1", "Cafe", "cafe", 30.0, -97.0, False, "100 Main St.")]
        + [(f"k{i}", "Cafe", "cafe", 31 + i, -97.0, False) for i in range(20)],
    )
    current = write(
        tmp_path / "cur.parquet",
        # New ID, ~600 m away (re-geocoded), same address written differently.
        [("s2", "Cafe", "cafe", 30.0054, -97.0, False, "100 MAIN ST")]
        + [(f"k{i}", "Cafe", "cafe", 31 + i, -97.0, False) for i in range(20)],
    )
    out = tmp_path / "changes.parquet"
    report = build_changes(duckdb.connect(), current, previous, out)
    assert (report.new, report.gone, report.matched_nearby) == (0, 0, 1)


def test_unstable_chains_are_left_out(tmp_path: Path) -> None:
    # "hair" appears from nowhere (brand tags changed upstream); "gym" adds one.
    gyms = [(f"g{i}", "Gym", "gym", 30 + i / 10, -97.0, False) for i in range(10)]
    previous = write(tmp_path / "prev.parquet", gyms)
    current = write(
        tmp_path / "cur.parquet",
        gyms
        + [("g-new", "Gym", "gym", 35.0, -97.0, False)]
        + [(f"h{i}", "Hair", "hair", 40 + i / 10, -90.0, False) for i in range(8)],
    )
    out = tmp_path / "changes.parquet"
    con = duckdb.connect()
    report = build_changes(con, current, previous, out)
    assert con.execute(f"SELECT id FROM '{out}'").fetchall() == [("g-new",)]
    assert report.unstable_chains == 1 and report.unstable_examples == [("hair", 8, 0)]
