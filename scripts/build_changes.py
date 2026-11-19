"""Step 2b: new and gone chain locations since the previous Overture release.

Reads places.parquet for both releases (run build_chains.py for each first) and
writes data/<release>/changes.parquet and changes_manifest.json, with totals and
the biggest movers so a coverage change upstream (thousands of "new" locations
for one chain) is easy to spot before it reaches users.

Usage:
    uv run python scripts/build_changes.py --release 2026-11-18.0 --previous 2026-10-21.0
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

import duckdb

from turf_app.changes import DEFAULT_MATCH_M, build_changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--match-m", type=float, default=DEFAULT_MATCH_M)
    parser.add_argument("--memory-limit", default="6GB")
    args = parser.parse_args()

    data = Path(args.data_dir)
    out = data / args.release / "changes.parquet"
    con = duckdb.connect(config={"memory_limit": args.memory_limit})
    con.execute("SET enable_progress_bar=false")
    started = time.monotonic()
    report = build_changes(
        con,
        data / args.release / "places.parquet",
        data / args.previous / "places.parquet",
        out,
        args.match_m,
    )
    movers = con.execute(f"""
        SELECT chain_key,
               count(*) FILTER (WHERE change = 'new') AS new,
               count(*) FILTER (WHERE change = 'gone') AS gone
        FROM read_parquet('{out}') GROUP BY 1 ORDER BY new + gone DESC LIMIT 15
    """).fetchall()
    manifest = {
        "release": args.release,
        "previous": args.previous,
        "match_m": args.match_m,
        **asdict(report),
        "top_movers": [{"chain_key": c, "new": n, "gone": g} for c, n, g in movers],
        "seconds": round(time.monotonic() - started, 1),
    }
    (data / args.release / "changes_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
