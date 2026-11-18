"""Step 1b: extract US state boundaries (50 states + DC) from Overture divisions.

Writes data/<release>/us_states.parquet with one row per boundary polygon:
land areas, plus coastal water areas so places on piers and shorelines still
get a state.

Usage:
    uv run python scripts/extract_states.py --release 2026-11-18.0
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--out-dir", default="data")
    args = parser.parse_args()

    out_dir = Path(args.out_dir) / args.release
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "us_states.parquet"
    src = f"s3://overturemaps-us-west-2/release/{args.release}/theme=divisions/type=division_area/*"

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
    con.execute("SET s3_region='us-west-2'; SET enable_progress_bar=false;")
    con.execute(f"""
        COPY (
            SELECT replace(region, 'US-', '') AS state,
                   class,           -- 'land' or 'maritime'
                   geometry
            FROM read_parquet('{src}')
            WHERE country = 'US' AND subtype = 'region' AND region LIKE 'US-%'
        ) TO '{out_file}' (FORMAT parquet, COMPRESSION zstd)
    """)
    rows = con.execute(f"""
        SELECT class, count(*), count(DISTINCT state)
        FROM read_parquet('{out_file}') GROUP BY 1 ORDER BY 1
    """).fetchall()
    for cls, n, states in rows:
        print(f"{cls}: {n} polygons, {states} states")
    print(f"wrote {out_file} ({out_file.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
