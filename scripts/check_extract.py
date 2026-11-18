"""Sanity checks on an extracted places file: state values, status mix, and known-chain counts.

These are the checks used to find the data problems listed in the README
(mixed state formats, missing brands, duplicate chain locations).

Usage:
    uv run python scripts/check_extract.py --release 2026-11-18.0
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

# Chains to spot-check. Name prefixes are matched case-insensitively.
SPOT_CHECK_CHAINS = ["orangetheory", "planet fitness", "starbucks", "chipotle"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    path = Path(args.data_dir) / args.release / "us_places.parquet"
    con = duckdb.connect()
    con.execute(f"CREATE VIEW p AS SELECT * FROM read_parquet('{path}')")

    print("== Region values (distinct formats are a normalization problem) ==")
    rows = con.execute("SELECT region, count(*) n FROM p GROUP BY 1 ORDER BY n DESC").fetchall()
    print(f"  {len(rows)} distinct values")
    for region, n in rows:
        print(f"  {n:>9}  {region}")

    print("\n== Operating status ==")
    for status, n in con.execute(
        "SELECT operating_status, count(*) FROM p GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall():
        print(f"  {n:>9}  {status}")

    print("\n== Chain spot checks (raw name prefix vs. brand field) ==")
    for chain in SPOT_CHECK_CHAINS:
        by_name, by_brand, name_no_brand = con.execute(
            """
            SELECT
                count(*) FILTER (WHERE lower(name) LIKE ? || '%'),
                count(*) FILTER (WHERE lower(brand_name) LIKE ? || '%'),
                count(*) FILTER (WHERE lower(name) LIKE ? || '%' AND brand_name IS NULL)
            FROM p
            """,
            [chain, chain, chain],
        ).fetchone() or (0, 0, 0)
        print(
            f"  {chain:<16} name={by_name:>6}  brand={by_brand:>6}"
            f"  name-but-no-brand={name_no_brand:>6}"
        )


if __name__ == "__main__":
    main()
