"""Build the read-only DuckDB file the API serves.

Only unique locations (duplicates removed) are kept, and the file is written
to a temporary path and then renamed, so the API never opens a half-written
database.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

SCHEMA_VERSION = 3  # 2: places.category + categories table; 3: places.metro + metros table


def write_serving_db(
    places: Path, chains: Path, metros: Path, out: Path, release: str, memory_limit: str = "4GB"
) -> None:
    tmp = out.with_name(out.name + ".building")
    tmp.unlink(missing_ok=True)
    # Without a limit DuckDB takes up to 80% of RAM; above the limit it spills to disk.
    con = duckdb.connect(str(tmp), config={"memory_limit": memory_limit})
    try:
        # Spatial is only needed here, to tag places with their metro; the
        # served file stores plain codes, so the API doesn't load it.
        con.execute("INSTALL spatial; LOAD spatial;")
        con.execute("CREATE TEMP TABLE metro_areas AS SELECT * FROM read_parquet(?)", [str(metros)])
        # Only the served columns: the parquet has wide list columns that
        # would otherwise all be held in memory.
        con.execute(
            """
            CREATE TEMP TABLE unique_places AS
            SELECT id, name, chain_key, basic_category, taxonomy_primary, confidence,
                   operating_status, address, locality, state, postcode, lat, lon
            FROM read_parquet(?) WHERE NOT is_duplicate
            """,
            [str(places)],
        )
        # An inner join so DuckDB uses its spatial join operator (seconds for
        # 18M points); a LEFT JOIN falls back to a nested loop. CBSAs don't
        # overlap, so each place gets at most one metro.
        con.execute("""
            CREATE TEMP TABLE place_metro AS
            SELECT p.id, m.code AS metro
            FROM unique_places p
            JOIN metro_areas m ON ST_Contains(m.geometry, ST_Point(p.lon, p.lat))
        """)
        # Sorted by chain so a chain's locations sit together on disk and
        # DuckDB can skip everything else when looking one up.
        con.execute("""
            CREATE TABLE places AS
            SELECT p.id, p.name, p.chain_key, p.basic_category,
                   p.taxonomy_primary AS category, p.confidence,
                   p.operating_status AS status,
                   p.address, p.locality, p.state, p.postcode, p.lat, p.lon,
                   pm.metro
            FROM unique_places p LEFT JOIN place_metro pm USING (id)
            ORDER BY p.chain_key, p.state
        """)
        # Metro areas with at least one place, for search and the filter.
        # Bounds are the places' extent, so the map can frame a metro.
        con.execute("""
            CREATE TABLE metros AS
            SELECT m.code AS metro_code, m.name, m.kind, m.search_key,
                   count(*) AS n_total,
                   count(*) FILTER (WHERE p.status = 'open') AS n_open,
                   min(p.lat) AS min_lat, min(p.lon) AS min_lon,
                   max(p.lat) AS max_lat, max(p.lon) AS max_lon
            FROM metro_areas m JOIN places p ON p.metro = m.code
            GROUP BY ALL
        """)
        con.execute(
            """
            CREATE TABLE chains AS
            SELECT c.chain_key, c.display_name, c.categories,
                   count(p.id) AS n_total,
                   count(p.id) FILTER (WHERE p.status = 'open') AS n_open
            FROM read_parquet(?) c
            JOIN places p USING (chain_key)
            GROUP BY ALL
            HAVING n_open >= 1
            """,
            [str(chains)],
        )
        # Fine-grained Overture categories (e.g. pet_groomer), for targeting
        # every business of a kind, chains and independents alike.
        con.execute("""
            CREATE TABLE categories AS
            SELECT category AS category_key,
                   upper(left(replace(category, '_', ' '), 1))
                       || substr(replace(category, '_', ' '), 2) AS label,
                   replace(category, '_', ' ') AS search_key,
                   mode(basic_category) AS parent,
                   count(*) AS n_total,
                   count(*) FILTER (WHERE status = 'open') AS n_open
            FROM places
            WHERE category IS NOT NULL
            GROUP BY category
            HAVING n_open >= 1
        """)
        con.execute(
            "CREATE TABLE meta AS SELECT ?::VARCHAR AS release, ?::INT AS schema_version, "
            "(now() AT TIME ZONE 'UTC') AS built_at_utc",
            [release, SCHEMA_VERSION],
        )
        con.execute("CHECKPOINT")
    finally:
        con.close()
    tmp.replace(out)
