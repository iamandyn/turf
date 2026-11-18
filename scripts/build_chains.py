"""Step 2: assign places to chains, flag duplicates, and fill in states.

Reads data/<release>/us_places.parquet (from extract_us.py) and
us_states.parquet (from extract_states.py), and writes
places.parquet, chains.parquet, and chains_manifest.json next to it.

Usage:
    uv run python scripts/build_chains.py --release 2026-11-18.0
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

import duckdb

from turf_app.chains import ChainRules, build
from turf_app.states import fill_states

SPOT_CHECK_CHAINS = [
    "orangetheory fitness",
    "planet fitness",
    "starbucks",
    "chipotle mexican grill",
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--memory-limit", default="8GB")
    args = parser.parse_args()

    release_dir = Path(args.data_dir) / args.release
    src = release_dir / "us_places.parquet"

    con = duckdb.connect()
    con.execute(f"SET memory_limit='{args.memory_limit}'; SET enable_progress_bar=false;")
    con.execute(f"CREATE TABLE places AS SELECT * FROM read_parquet('{src}')")

    started = time.monotonic()
    build(con, ChainRules())
    con.execute("INSTALL spatial; LOAD spatial;")
    states_file = release_dir / "us_states.parquet"
    con.execute(f"CREATE TABLE us_states AS SELECT * FROM read_parquet('{states_file}')")
    state_report = fill_states(con)
    elapsed = time.monotonic() - started

    con.execute(
        f"COPY places_out TO '{release_dir / 'places.parquet'}' (FORMAT parquet, COMPRESSION zstd)"
    )
    con.execute(
        f"COPY chains TO '{release_dir / 'chains.parquet'}' (FORMAT parquet, COMPRESSION zstd)"
    )

    totals = con.execute("""
        SELECT count(*),
               count(chain_key),
               count(*) FILTER (WHERE chain_match = 'brand'),
               count(*) FILTER (WHERE chain_match = 'name'),
               count(*) FILTER (WHERE is_duplicate),
               count(state)
        FROM places_out
    """).fetchone()
    assert totals is not None
    n_chains = con.execute("SELECT count(*) FROM chains").fetchone()
    assert n_chains is not None

    spot = {}
    for key in SPOT_CHECK_CHAINS:
        row = con.execute(
            """
            SELECT count(*) FILTER (WHERE chain_match = 'brand'),
                   count(*) FILTER (WHERE chain_match = 'name'),
                   count(*) FILTER (WHERE is_duplicate),
                   count(*) FILTER (WHERE NOT is_duplicate)
            FROM places_out WHERE chain_key = ?
            """,
            [key],
        ).fetchone()
        assert row is not None
        spot[key] = dict(zip(("by_brand", "by_name", "duplicates", "unique"), row, strict=True))

    manifest = {
        "release": args.release,
        "places": totals[0],
        "in_a_chain": totals[1],
        "matched_by_brand": totals[2],
        "matched_by_name": totals[3],
        "duplicates": totals[4],
        "with_state": totals[5],
        "chains": n_chains[0],
        "state_fill": asdict(state_report),
        "spot_checks": spot,
        "build_seconds": round(elapsed, 1),
    }
    (release_dir / "chains_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
