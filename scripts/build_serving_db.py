"""Step 3: build the read-only database the API serves.

Reads places.parquet and chains.parquet from build_chains.py and writes
data/<release>/turf.duckdb.

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
    args = parser.parse_args()

    release_dir = Path(args.data_dir) / args.release
    out = release_dir / "turf.duckdb"
    started = time.monotonic()
    write_serving_db(
        release_dir / "places.parquet", release_dir / "chains.parquet", out, args.release
    )
    con = duckdb.connect(str(out), read_only=True)
    places = con.execute("SELECT count(*) FROM places").fetchone()
    chains = con.execute("SELECT count(*) FROM chains").fetchone()
    assert places is not None and chains is not None
    print(
        f"wrote {out}: {places[0]:,} places, {chains[0]:,} chains, "
        f"{out.stat().st_size / 1e9:.2f} GB in {time.monotonic() - started:.0f}s"
    )


if __name__ == "__main__":
    main()
