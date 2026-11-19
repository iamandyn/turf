"""Step 1c: metro areas (Census CBSAs): boundaries and population.

Overture has no metro areas or population, so this uses two Census Bureau
files, both on the July 2023 CBSA delineations:

* the cartographic boundary file of Core-Based Statistical Areas (2024 vintage);
* the Vintage 2025 population estimates (July 1, 2020 and 2025), which turn
  place counts into per-capita rates and give five-year growth.

Puerto Rico is dropped, since the places extract doesn't cover territories,
leaving 387 metropolitan and 538 micropolitan areas, all with a population.

Downloads are pinned by SHA-256, so a rebuild uses exactly the same files.
Writes data/<release>/us_metros.parquet: code, name, kind, search_key,
population, population_2020, geometry.

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

BOUNDARIES_URL = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_cbsa_500k.zip"
BOUNDARIES_SHA256 = "6b49c0d8caaab77aed9c7c786c4b2af19d1acd9333c76cd702fc117ff2165117"
POPULATION_URL = (
    "https://www2.census.gov/programs-surveys/popest/datasets/2020-2025/metro/totals/"
    "cbsa-est2025-alldata.csv"
)
POPULATION_SHA256 = "bf6fad83753456413d047d1acebeb434bd067eba9d6d0be62b3f737ad1301852"


def download(url: str, sha256: str, dest: Path) -> Path:
    """Fetch ``url`` into its own directory (once) and check the pinned hash."""
    path = dest / Path(url).name
    if not path.exists():
        dest.mkdir(parents=True, exist_ok=True)
        print(f"downloading {url}")
        urllib.request.urlretrieve(url, path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != sha256:
        raise SystemExit(f"{path}: sha256 {digest} doesn't match the pinned {sha256}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--out-dir", default="data")
    args = parser.parse_args()

    out_dir = Path(args.out_dir) / args.release
    zip_path = download(BOUNDARIES_URL, BOUNDARIES_SHA256, out_dir / "census_cbsa_2024")
    pop_path = download(POPULATION_URL, POPULATION_SHA256, out_dir / "census_pop_2025")
    out_file = out_dir / "us_metros.parquet"

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial; SET enable_progress_bar=false;")
    shp = f"/vsizip/{zip_path}/{zip_path.stem}.shp"
    rows = con.execute(
        "SELECT CBSAFP, NAME, LSAD FROM ST_Read(?) WHERE NAME NOT LIKE '%, PR'", [shp]
    ).fetchall()
    con.execute("CREATE TABLE keys (code VARCHAR, search_key VARCHAR)")
    con.executemany("INSERT INTO keys VALUES (?, ?)", [(r[0], normalize_text(r[1])) for r in rows])
    # Latin-1 (accented names like Cañon City). The file also has county and
    # metropolitan-division rows; only whole metro/micro areas are kept.
    con.execute(
        """
        CREATE TABLE population AS
        SELECT CBSA AS code, POPESTIMATE2025::BIGINT AS population,
               POPESTIMATE2020::BIGINT AS population_2020
        FROM read_csv(?, all_varchar = true, header = true, encoding = 'latin-1')
        WHERE LSAD IN ('Metropolitan Statistical Area', 'Micropolitan Statistical Area')
        """,
        [str(pop_path)],
    )
    con.execute(
        f"""
        COPY (
            SELECT s.CBSAFP AS code, s.NAME AS name,
                   CASE s.LSAD WHEN 'M1' THEN 'metro' WHEN 'M2' THEN 'micro' END AS kind,
                   k.search_key, p.population, p.population_2020,
                   -- NAD83; within a meter or so of WGS84, plenty for metro boundaries.
                   s.geom::GEOMETRY AS geometry
            FROM ST_Read(?) s
            JOIN keys k ON k.code = s.CBSAFP
            JOIN population p ON p.code = s.CBSAFP
            ORDER BY code
        ) TO '{out_file}' (FORMAT parquet, COMPRESSION zstd)
        """,
        [shp],
    )
    n_boundaries = len(rows)
    n_written = con.execute(f"SELECT count(*) FROM read_parquet('{out_file}')").fetchone()
    if not n_written or n_written[0] != n_boundaries:
        raise SystemExit(f"population missing for some metros: {n_written} of {n_boundaries}")
    for kind, n in con.execute(
        f"SELECT kind, count(*) FROM read_parquet('{out_file}') GROUP BY 1 ORDER BY 1"
    ).fetchall():
        print(f"{kind}: {n}")
    print(f"wrote {out_file} ({out_file.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
