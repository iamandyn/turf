"""Break down one chain's locations to find over-counting (duplicates, closures, offices).

Reads data/<release>/places.parquet from build_chains.py.

Usage:
    uv run python scripts/investigate_chain.py --release 2026-11-18.0 --chain "planet fitness"
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb


def show(con: duckdb.DuckDBPyConnection, title: str, sql: str, params: list[object]) -> None:
    print(f"\n== {title} ==")
    for row in con.execute(sql, params).fetchall():
        print("  ", row)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--chain", required=True, help="normalized chain key")
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    path = Path(args.data_dir) / args.release / "places.parquet"
    con = duckdb.connect()
    con.execute("SET enable_progress_bar=false")
    con.execute(
        """
        CREATE TABLE c AS SELECT * FROM read_parquet(?)
        WHERE chain_key = ? AND NOT is_duplicate
        """,
        [str(path), args.chain],
    )
    con.execute("""
        CREATE MACRO haversine_m(lat1, lon1, lat2, lon2) AS
            2 * 6371008.8 * asin(sqrt(
                pow(sin(radians(lat2 - lat1) / 2), 2)
                + cos(radians(lat1)) * cos(radians(lat2))
                  * pow(sin(radians(lon2 - lon1) / 2), 2)))
    """)
    total = con.execute("SELECT count(*) FROM c").fetchone()
    print(f"{args.chain}: {total[0] if total else 0} unique locations")

    show(con, "Status", "SELECT operating_status, count(*) FROM c GROUP BY 1 ORDER BY 2 DESC", [])
    show(
        con,
        "Match type",
        "SELECT chain_match, count(*) FROM c GROUP BY 1 ORDER BY 2 DESC",
        [],
    )
    show(
        con,
        "Name variants",
        "SELECT name, count(*) FROM c GROUP BY 1 ORDER BY 2 DESC LIMIT 12",
        [],
    )
    show(
        con,
        "Categories",
        "SELECT basic_category, count(*) FROM c GROUP BY 1 ORDER BY 2 DESC LIMIT 8",
        [],
    )
    show(
        con,
        "Source combinations",
        "SELECT source_datasets, count(*) FROM c GROUP BY 1 ORDER BY 2 DESC LIMIT 10",
        [],
    )
    show(
        con,
        "Confidence",
        """SELECT round(quantile_cont(confidence, 0.1), 2), round(median(confidence), 2),
                  count(*) FILTER (WHERE confidence < 0.5) AS below_half FROM c""",
        [],
    )

    # Nearest other location of the same chain: a spike at short distances
    # means duplicates the current rules miss.
    con.execute("""
        CREATE TABLE nn AS
        SELECT a.id, min(haversine_m(a.lat, a.lon, b.lat, b.lon)) AS d
        FROM c a JOIN c b ON a.id <> b.id AND abs(a.lat - b.lat) < 0.02
        GROUP BY a.id
    """)
    show(
        con,
        "Nearest same-chain location",
        """SELECT count(*) FILTER (WHERE d < 50)   AS lt50m,
                  count(*) FILTER (WHERE d < 150)  AS lt150m,
                  count(*) FILTER (WHERE d < 300)  AS lt300m,
                  count(*) FILTER (WHERE d < 1000) AS lt1km
           FROM nn""",
        [],
    )
    show(
        con,
        "Sample close pairs (< 300 m)",
        """SELECT a.name, a.address, b.name, b.address,
                  round(haversine_m(a.lat, a.lon, b.lat, b.lon)) AS m,
                  a.operating_status, b.operating_status
           FROM c a JOIN c b ON a.id < b.id AND abs(a.lat - b.lat) < 0.01
           WHERE haversine_m(a.lat, a.lon, b.lat, b.lon) < 300
           ORDER BY hash(a.id) LIMIT 12""",
        [],
    )


if __name__ == "__main__":
    main()
