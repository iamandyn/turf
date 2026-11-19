"""Build the read-only DuckDB file the API serves.

Only unique locations (duplicates removed) are kept, and the file is written
to a temporary path and then renamed, so the API never opens a half-written
database.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

SCHEMA_VERSION = 2  # 2: places.category + categories table


def write_serving_db(places: Path, chains: Path, out: Path, release: str) -> None:
    tmp = out.with_name(out.name + ".building")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    try:
        # Sorted by chain so a chain's locations sit together on disk and
        # DuckDB can skip everything else when looking one up.
        con.execute(
            """
            CREATE TABLE places AS
            SELECT id, name, chain_key, basic_category,
                   taxonomy_primary AS category, confidence,
                   operating_status AS status,
                   address, locality, state, postcode, lat, lon
            FROM read_parquet(?)
            WHERE NOT is_duplicate
            ORDER BY chain_key, state
            """,
            [str(places)],
        )
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
