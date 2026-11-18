"""Step 0: inspect the Overture places schema and measure a sample before designing the extract.

Reads the cloud-hosted GeoParquet directly with DuckDB (no account or key needed).

Usage:
    uv run python scripts/inspect_places.py --release 2026-10-22.0
    uv run python scripts/inspect_places.py --release 2026-10-22.0 --bbox -97.94,30.10,-97.56,30.52

Find valid release names with:
    aws s3 ls --no-sign-request s3://overturemaps-us-west-2/release/
"""

from __future__ import annotations

import argparse

import duckdb

# Default sample: roughly the city of Austin, TX (xmin, ymin, xmax, ymax).
DEFAULT_BBOX = (-97.94, 30.10, -97.56, 30.52)


def places_path(release: str) -> str:
    return f"s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*"


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
    con.execute("SET s3_region='us-west-2';")
    return con


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, help="Overture release, e.g. 2026-10-22.0")
    parser.add_argument(
        "--bbox",
        default=",".join(map(str, DEFAULT_BBOX)),
        help="xmin,ymin,xmax,ymax in WGS84",
    )
    args = parser.parse_args()
    xmin, ymin, xmax, ymax = (float(v) for v in args.bbox.split(","))
    src = places_path(args.release)
    con = connect()

    print("== Schema ==")
    for name, dtype, *_ in con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{src}', hive_partitioning=1)"
    ).fetchall():
        print(f"  {name}: {dtype}")

    # The bbox predicate lets DuckDB skip row groups outside the area.
    bbox_filter = f"""
        bbox.xmin >= {xmin} AND bbox.xmax <= {xmax}
        AND bbox.ymin >= {ymin} AND bbox.ymax <= {ymax}
    """
    con.execute(f"""
        CREATE TEMP TABLE sample AS
        SELECT * FROM read_parquet('{src}', hive_partitioning=1)
        WHERE {bbox_filter}
    """)

    print("\n== Sample size ==")
    print(con.execute("SELECT count(*) AS places FROM sample").fetchone())

    print("\n== Brand coverage ==")
    print(
        con.execute("""
        SELECT
            count(*) FILTER (WHERE brand IS NOT NULL) AS with_brand,
            count(DISTINCT brand.names.primary) AS distinct_brands
        FROM sample
        """).fetchone()
    )

    print("\n== Top 25 brands (raw, before normalization) ==")
    for row in con.execute("""
        SELECT brand.names.primary AS brand, count(*) AS n
        FROM sample WHERE brand IS NOT NULL
        GROUP BY 1 ORDER BY n DESC LIMIT 25
    """).fetchall():
        print(f"  {row[1]:>5}  {row[0]}")

    print("\n== Confidence distribution ==")
    print(
        con.execute("""
        SELECT
            round(quantile_cont(confidence, 0.1), 2) AS p10,
            round(quantile_cont(confidence, 0.5), 2) AS p50,
            round(quantile_cont(confidence, 0.9), 2) AS p90
        FROM sample
        """).fetchone()
    )


if __name__ == "__main__":
    main()
