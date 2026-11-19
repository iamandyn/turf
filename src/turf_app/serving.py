"""Build the read-only DuckDB file the API serves.

Market selection works on per-metro aggregates, so the served file holds no
individual places: it's a few megabytes, built from the 18M-place pipeline
output. Only unique places (duplicates removed) are counted, and the file is
written to a temporary path and then renamed, so the API never opens a
half-written database.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

SCHEMA_VERSION = 5
# 2: places.category + categories; 3: places.metro + metros; 4: change view;
# 5: market selection: per-metro aggregates (metro_category, metro_zips,
#    population), no places table.

# A ZIP is exported for the metro that holds most of its places, and only if
# it has at least this many there (stray or mistyped postcodes otherwise add
# far-away ZIPs to a metro's list).
MIN_PLACES_PER_ZIP = 5


def write_serving_db(
    places: Path, metros: Path, out: Path, release: str, memory_limit: str = "4GB"
) -> None:
    tmp = out.with_name(out.name + ".building")
    tmp.unlink(missing_ok=True)
    # Without a limit DuckDB takes up to 80% of RAM; above the limit it spills to disk.
    con = duckdb.connect(str(tmp), config={"memory_limit": memory_limit})
    try:
        # Spatial is only needed here, to tag places with their metro; the
        # served file has no geometry, so the API doesn't load it.
        con.execute("INSTALL spatial; LOAD spatial;")
        con.execute("CREATE TEMP TABLE metro_areas AS SELECT * FROM read_parquet(?)", [str(metros)])
        # Only the columns used here: the parquet has wide list columns that
        # would otherwise all be held in memory.
        con.execute(
            """
            CREATE TEMP TABLE unique_places AS
            SELECT id, basic_category, taxonomy_primary AS category,
                   operating_status AS status, postcode, lat, lon
            FROM read_parquet(?) WHERE NOT is_duplicate
            """,
            [str(places)],
        )
        # An inner join so DuckDB uses its spatial join operator (seconds for
        # 18M points); a LEFT JOIN falls back to a nested loop. CBSAs don't
        # overlap, so each place gets at most one metro.
        con.execute("""
            CREATE TEMP TABLE metro_places AS
            SELECT p.*, m.code AS metro
            FROM unique_places p
            JOIN metro_areas m ON ST_Contains(m.geometry, ST_Point(p.lon, p.lat))
        """)
        # Metro areas: population from the Census, place totals, and the
        # places' center and extent to draw and frame each one on the map.
        con.execute("""
            CREATE TABLE metros AS
            SELECT m.code AS metro_code, m.name, m.kind, m.search_key,
                   m.population, m.population_2020,
                   round(100.0 * (m.population - m.population_2020) / m.population_2020, 2)
                       AS growth_pct,
                   count(p.id) AS n_places,
                   count(p.id) FILTER (WHERE p.status = 'open') AS n_open,
                   avg(p.lat) AS center_lat, avg(p.lon) AS center_lon,
                   min(p.lat) AS min_lat, min(p.lon) AS min_lon,
                   max(p.lat) AS max_lat, max(p.lon) AS max_lon
            FROM metro_areas m JOIN metro_places p ON p.metro = m.code
            GROUP BY ALL
            ORDER BY metro_code
        """)
        # Open places per metro and Overture category: the raw material for
        # every signal (rates per 100k residents are computed at query time).
        con.execute("""
            CREATE TABLE metro_category AS
            SELECT metro AS metro_code, category, count(*) AS n_open
            FROM metro_places
            WHERE status = 'open' AND category IS NOT NULL
            GROUP BY ALL
            ORDER BY category, metro_code
        """)
        # Fine-grained Overture categories (e.g. pet_groomer) that can be used
        # as signals, for search. Counted nationwide.
        con.execute("""
            CREATE TABLE categories AS
            SELECT category AS category_key,
                   upper(left(replace(category, '_', ' '), 1))
                       || substr(replace(category, '_', ' '), 2) AS label,
                   replace(category, '_', ' ') AS search_key,
                   mode(basic_category) AS parent,
                   count(*) AS n_total,
                   count(*) FILTER (WHERE status = 'open') AS n_open
            FROM unique_places
            WHERE category IS NOT NULL
            GROUP BY category
            HAVING n_open >= 1
        """)
        # ZIP codes per metro, for ad platforms (Meta and Google both take US
        # ZIPs; neither targets Census metro areas directly). Each ZIP goes to
        # the metro holding most of its places.
        con.execute(f"""
            CREATE TABLE metro_zips AS
            WITH counts AS (
                SELECT left(postcode, 5) AS zip, metro, count(*) AS n_places
                FROM metro_places
                WHERE regexp_full_match(left(postcode, 5), '[0-9]{{5}}')
                GROUP BY ALL
            )
            SELECT metro AS metro_code, zip, n_places
            FROM counts
            QUALIFY row_number() OVER (PARTITION BY zip ORDER BY n_places DESC, metro) = 1
                AND n_places >= {MIN_PLACES_PER_ZIP}
            ORDER BY metro_code, zip
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
