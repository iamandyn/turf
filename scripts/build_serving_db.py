"""Step 3: build the read-only database the API serves.

Reads places.parquet and chains.parquet from build_chains.py and
us_metros.parquet from extract_metros.py (and, with --previous, changes.parquet
from build_changes.py), and writes data/<release>/turf.duckdb.

Usage:
    uv run python scripts/build_serving_db.py --release 2026-11-18.0
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import duckdb

from turf_app.serving import write_serving_db


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--memory-limit", default="4GB")
    parser.add_argument(
        "--previous", help="release the changes were built against (adds the new/gone view)"
    )
    args = parser.parse_args()

    release_dir = Path(args.data_dir) / args.release
    out = release_dir / "turf.duckdb"
    started = time.monotonic()
    write_serving_db(
        release_dir / "places.parquet",
        release_dir / "chains.parquet",
        release_dir / "us_metros.parquet",
        out,
        args.release,
        memory_limit=args.memory_limit,
        changes=release_dir / "changes.parquet" if args.previous else None,
        previous_release=args.previous,
    )
    con = duckdb.connect(str(out), read_only=True)
    places = con.execute("SELECT count(*) FROM places").fetchone()
    chains = con.execute("SELECT count(*) FROM chains").fetchone()
    metros = con.execute(
        "SELECT (SELECT count(*) FROM metros), avg((metro IS NOT NULL)::INT) FROM places"
    ).fetchone()
    assert places is not None and chains is not None and metros is not None
    print(
        f"wrote {out}: {places[0]:,} places, {chains[0]:,} chains, "
        f"{metros[0]:,} metro areas ({metros[1]:.1%} of places in one), "
        f"{out.stat().st_size / 1e9:.2f} GB in {time.monotonic() - started:.0f}s"
    )


if __name__ == "__main__":
    main()
