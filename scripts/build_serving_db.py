"""Step 3: build the read-only database the API serves.

Reads places.parquet from build_chains.py (for its duplicate flags) and
us_metros.parquet from extract_metros.py, and writes data/<release>/turf.duckdb:
per-metro aggregates only, a few megabytes.

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
    args = parser.parse_args()

    release_dir = Path(args.data_dir) / args.release
    out = release_dir / "turf.duckdb"
    started = time.monotonic()
    write_serving_db(
        release_dir / "places.parquet",
        release_dir / "us_metros.parquet",
        out,
        args.release,
        memory_limit=args.memory_limit,
    )
    con = duckdb.connect(str(out), read_only=True)
    row = con.execute("""
        SELECT (SELECT count(*) FROM metros), (SELECT sum(n_places) FROM metros),
               (SELECT count(*) FROM categories), (SELECT count(*) FROM metro_category),
               (SELECT count(*) FROM metro_zips)
    """).fetchone()
    assert row is not None
    metros, places, categories, cells, zips = row
    print(
        f"wrote {out}: {metros:,} metro areas, {places:,} places in them, "
        f"{categories:,} categories ({cells:,} metro x category counts), {zips:,} ZIPs, "
        f"{out.stat().st_size / 1e6:.1f} MB in {time.monotonic() - started:.0f}s"
    )


if __name__ == "__main__":
    main()
