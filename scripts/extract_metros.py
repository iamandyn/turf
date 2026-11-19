"""Step 1c: metro area boundaries (Census CBSAs), for the metro filter.

Overture has no metro areas, so this uses the Census Bureau's cartographic
boundary file of Core-Based Statistical Areas (2024 vintage, July 2023
delineations). Puerto Rico is dropped, since the places extract doesn't cover
territories, leaving 387 metropolitan and 538 micropolitan areas.

The download is pinned by SHA-256, so a rebuild uses exactly the same file.
Writes data/<release>/us_metros.parquet: code, name, kind, search_key, geometry.

Usage:
    uv run python scripts/extract_metros.py --release 2026-11-18.0
"""

from __future__ import annotations

import argparse
import hashlib
import urllib.request
from pathlib import Path

import duckdb

from turf_app.normalize import normalize_text

URL = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_cbsa_500k.zip"
SHA256 = "6b49c0d8caaab77aed9c7c786c4b2af19d1acd9333c76cd702fc117ff2165117"


def download(dest: Path) -> Path:
    zip_path = dest / Path(URL).name
    if not zip_path.exists():
        dest.mkdir(parents=True, exist_ok=True)
        print(f"downloading {URL}")
        urllib.request.urlretrieve(URL, zip_path)
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    if digest != SHA256:
        raise SystemExit(f"{zip_path}: sha256 {digest} doesn't match the pinned {SHA256}")
    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--out-dir", default="data")
    args = parser.parse_args()

    out_dir = Path(args.out_dir) / args.release
    zip_path = download(out_dir / "census_cbsa_2024")
    out_file = out_dir / "us_metros.parquet"

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial; SET enable_progress_bar=false;")
    shp = f"/vsizip/{zip_path}/{zip_path.stem}.shp"
    rows = con.execute(
        "SELECT CBSAFP, NAME, LSAD FROM ST_Read(?) WHERE NAME NOT LIKE '%, PR'", [shp]
    ).fetchall()
    con.execute("CREATE TABLE keys (code VARCHAR, search_key VARCHAR)")
    con.executemany("INSERT INTO keys VALUES (?, ?)", [(r[0], normalize_text(r[1])) for r in rows])
    con.execute(
        f"""
        COPY (
            SELECT s.CBSAFP AS code, s.NAME AS name,
                   CASE s.LSAD WHEN 'M1' THEN 'metro' WHEN 'M2' THEN 'micro' END AS kind,
                   k.search_key,
                   -- NAD83; within a meter or so of WGS84, plenty for metro boundaries.
                   s.geom::GEOMETRY AS geometry
            FROM ST_Read(?) s JOIN keys k ON k.code = s.CBSAFP
            ORDER BY code
        ) TO '{out_file}' (FORMAT parquet, COMPRESSION zstd)
        """,
        [shp],
    )
    for kind, n in con.execute(
        f"SELECT kind, count(*) FROM read_parquet('{out_file}') GROUP BY 1 ORDER BY 1"
    ).fetchall():
        print(f"{kind}: {n}")
    print(f"wrote {out_file} ({out_file.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
