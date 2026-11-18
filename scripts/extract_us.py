"""Step 1: extract US places from a pinned Overture release into a local Parquet file.

Only the columns Turf needs are kept, and permanently closed places are dropped.
Everything after this step works on the local file, never on S3.

Usage:
    uv run python scripts/extract_us.py --release 2026-11-18.0
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import duckdb

# Rough boxes (xmin, ymin, xmax, ymax). These only let DuckDB skip row groups;
# the address country filter below decides what is actually US.
# Not covered yet: Puerto Rico and other territories, and Aleutian islands west of 180°.
US_BOXES = {
    "lower48": (-125.0, 24.3, -66.8, 49.5),
    "alaska": (-180.0, 51.0, -129.0, 71.6),
    "hawaii": (-160.6, 18.8, -154.6, 22.4),
}


def places_path(release: str) -> str:
    return f"s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*"


def bbox_predicate() -> str:
    parts = [
        f"(bbox.xmin >= {x0} AND bbox.xmax <= {x1} AND bbox.ymin >= {y0} AND bbox.ymax <= {y1})"
        for x0, y0, x1, y1 in US_BOXES.values()
    ]
    return "(" + " OR ".join(parts) + ")"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--out-dir", default="data")
    args = parser.parse_args()

    out_dir = Path(args.out_dir) / args.release
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "us_places.parquet"

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
    con.execute("SET s3_region='us-west-2'; SET enable_progress_bar=false;")

    started = time.monotonic()
    # Places are points, so the bbox corner is the point itself; reading it
    # avoids decoding every geometry.
    con.execute(f"""
        COPY (
            SELECT
                id,
                names.primary                       AS name,
                nullif(brand.names.primary, '')     AS brand_name,
                brand.wikidata                      AS brand_wikidata,
                basic_category,
                taxonomy.primary                    AS taxonomy_primary,
                taxonomy.hierarchy                  AS taxonomy_hierarchy,
                confidence,
                operating_status,
                addresses[1].freeform               AS address,
                addresses[1].locality               AS locality,
                addresses[1].region                 AS region,
                addresses[1].postcode               AS postcode,
                bbox.xmin                           AS lon,
                bbox.ymin                           AS lat,
                [s.dataset for s in sources]        AS source_datasets
            FROM read_parquet('{places_path(args.release)}')
            WHERE {bbox_predicate()}
              AND addresses[1].country = 'US'
              AND coalesce(operating_status, '') <> 'permanently_closed'
              AND names.primary IS NOT NULL
        ) TO '{out_file}' (FORMAT parquet, COMPRESSION zstd)
    """)
    elapsed = time.monotonic() - started

    stats = con.execute(f"""
        SELECT
            count(*)                                        AS rows,
            count(brand_name)                               AS with_brand_name,
            count(DISTINCT brand_name)                      AS distinct_brands,
            count(*) FILTER (WHERE operating_status IS NULL) AS unknown_status,
            count(DISTINCT region)                          AS regions
        FROM read_parquet('{out_file}')
    """).fetchone()
    assert stats is not None
    rows, with_brand, distinct_brands, unknown_status, regions = stats

    manifest = {
        "release": args.release,
        "rows": rows,
        "with_brand_name": with_brand,
        "distinct_brands": distinct_brands,
        "unknown_status": unknown_status,
        "regions": regions,
        "file_bytes": out_file.stat().st_size,
        "extract_seconds": round(elapsed, 1),
    }
    (out_dir / "extract_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
