"""Assign places to chains and flag duplicate locations.

Works on a DuckDB table named ``places`` with the columns written by
scripts/extract_us.py. The rules come from what the real data showed:

* The brand field is missing on many real chain locations, so a place can join
  a chain by its name as well as its brand.
* Some chains have brand tags on places with location-only names
  ("Mansfield, TX" for an Orangetheory studio), so a correct brand tag can't
  require the name to agree.
* Some brand tags are simply wrong (one brand attached to dozens of unrelated
  individual professionals), so each chain earns trust from how often its
  tagged places also carry its name.
* Name matches are limited to the chain's usual categories, so "Subway" the
  restaurant doesn't claim a subway station.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import duckdb
from duckdb.func import FunctionNullHandling

from turf_app.normalize import (
    CHAIN_ALIASES,
    chain_key,
    normalize_state,
    normalize_text,
    street_key,
)

# Brand-like entries that are kiosks or counters inside other businesses, not
# storefronts anyone would target. Normalized chain keys.
EXCLUDED_CHAINS = frozenset(
    {
        "amazon locker",
        "amazon hub locker",
        "western union",
        "moneygram",
        "redbox",
        "coinstar",
        "bitcoin depot",
    }
)


@dataclass(frozen=True)
class ChainRules:
    # A chain needs this many brand-tagged places to exist at all.
    min_tagged: int = 3
    # Share of a chain's tagged places whose name also matches the chain.
    min_name_agreement: float = 0.3
    # A category counts as one of the chain's "usual" categories at this share.
    min_category_share: float = 0.05
    # Duplicates: same chain, same street number and street name, within this
    # distance. A real duplicate pair in the data was 518 m apart.
    dup_same_street_m: float = 1000.0
    # Duplicates: same chain within this distance, whatever the address says.
    dup_any_m: float = 30.0


def _register_functions(con: duckdb.DuckDBPyConnection) -> None:
    text_functions: dict[str, Callable[[str | None], str | None]] = {
        "normalize_text": normalize_text,
        "chain_key": chain_key,
        "normalize_state": normalize_state,
        "street_key": street_key,
    }
    for fn_name, fn in text_functions.items():
        con.create_function(
            fn_name, fn, ["VARCHAR"], "VARCHAR", null_handling=FunctionNullHandling.SPECIAL
        )
    # Great-circle distance in meters; avoids the spatial extension's axis-order rules.
    con.execute("""
        CREATE OR REPLACE MACRO haversine_m(lat1, lon1, lat2, lon2) AS
            2 * 6371008.8 * asin(sqrt(
                pow(sin(radians(lat2 - lat1) / 2), 2)
                + cos(radians(lat1)) * cos(radians(lat2))
                  * pow(sin(radians(lon2 - lon1) / 2), 2)
            ))
    """)


def build(con: duckdb.DuckDBPyConnection, rules: ChainRules = ChainRules()) -> None:
    """Create tables ``chains`` and ``places_out`` from ``places``."""
    _register_functions(con)

    # 1. Normalize once. Python UDFs run on distinct values only, then join back.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE brand_keys AS
        SELECT brand_name, chain_key(brand_name) AS bkey
        FROM (SELECT DISTINCT brand_name FROM places WHERE brand_name IS NOT NULL)
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE name_norm AS
        SELECT name, normalize_text(name) AS nname
        FROM (SELECT DISTINCT name FROM places)
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE state_norm AS
        SELECT region, normalize_state(region) AS state
        FROM (SELECT DISTINCT region FROM places WHERE region IS NOT NULL)
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE p AS
        SELECT pl.*, bk.bkey, nn.nname, sn.state
        FROM places pl
        LEFT JOIN brand_keys bk USING (brand_name)
        LEFT JOIN name_norm nn USING (name)
        LEFT JOIN state_norm sn USING (region)
    """)

    # 2. Name prefixes (whole tokens, up to 5) for name-based matching.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE prefixes AS
        SELECT id, k AS ntokens, array_to_string(tokens[1:k], ' ') AS prefix
        FROM (
            SELECT id, string_split(nname, ' ') AS tokens FROM p WHERE nname <> ''
        ), LATERAL (SELECT unnest(range(1, least(len(tokens), 5) + 1)) AS k)
    """)

    # Lookup from any key a name can produce to its canonical chain key.
    alias_rows = [(alias, canonical) for alias, canonical in CHAIN_ALIASES.items()]
    con.execute("CREATE OR REPLACE TEMP TABLE aliases (alias VARCHAR, canonical VARCHAR)")
    if alias_rows:
        con.executemany("INSERT INTO aliases VALUES (?, ?)", alias_rows)

    # 3. Chain stats: how often a tagged place's name agrees with its brand.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE tagged AS
        SELECT p.id, p.bkey, p.basic_category, p.brand_name,
               EXISTS (
                   SELECT 1 FROM prefixes x
                   LEFT JOIN aliases a ON a.alias = x.prefix
                   WHERE x.id = p.id AND coalesce(a.canonical, x.prefix) = p.bkey
               ) AS name_agrees
        FROM p WHERE p.bkey IS NOT NULL AND p.bkey <> ''
    """)
    con.execute(
        """
        CREATE OR REPLACE TABLE chains AS
        WITH stats AS (
            SELECT bkey AS chain_key,
                   count(*) AS n_tagged,
                   avg(name_agrees::INT) AS name_agreement,
                   mode(brand_name) AS display_name
            FROM tagged GROUP BY 1
        ),
        cats AS (
            SELECT bkey AS chain_key, basic_category,
                   count(*) / sum(count(*)) OVER (PARTITION BY bkey) AS share
            -- Only places whose name agrees, so a wrong brand tag on a taco
            -- shop can't add "restaurant" to a gym chain's categories.
            FROM tagged WHERE basic_category IS NOT NULL AND name_agrees
            GROUP BY 1, 2
        )
        SELECT s.*,
               list(c.basic_category ORDER BY c.share DESC)
                   FILTER (WHERE c.share >= $min_cat) AS categories
        FROM stats s LEFT JOIN cats c USING (chain_key)
        WHERE s.n_tagged >= $min_tagged
          AND s.name_agreement >= $min_agree
          AND s.chain_key NOT IN (SELECT unnest($excluded::VARCHAR[]))
        GROUP BY ALL
        """,
        {
            "min_cat": rules.min_category_share,
            "min_tagged": rules.min_tagged,
            "min_agree": rules.min_name_agreement,
            "excluded": sorted(EXCLUDED_CHAINS),
        },
    )

    # 4. Assign: trusted brand tag first, then longest name prefix in-category.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE by_brand AS
        SELECT t.id, t.bkey AS chain_key
        FROM tagged t JOIN chains c ON c.chain_key = t.bkey
        WHERE t.name_agrees OR list_contains(c.categories, t.basic_category)
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE by_name AS
        SELECT id, chain_key FROM (
            SELECT x.id, c.chain_key,
                   row_number() OVER (PARTITION BY x.id ORDER BY x.ntokens DESC) AS rn
            FROM prefixes x
            LEFT JOIN aliases a ON a.alias = x.prefix
            JOIN chains c ON c.chain_key = coalesce(a.canonical, x.prefix)
            JOIN p ON p.id = x.id
            WHERE list_contains(c.categories, p.basic_category)
              AND x.id NOT IN (SELECT id FROM by_brand)
        ) WHERE rn = 1
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE assigned AS
        SELECT p.*,
               coalesce(b.chain_key, n.chain_key) AS chain_key,
               CASE WHEN b.id IS NOT NULL THEN 'brand'
                    WHEN n.id IS NOT NULL THEN 'name' END AS chain_match
        FROM p
        LEFT JOIN by_brand b USING (id)
        LEFT JOIN by_name n USING (id)
    """)

    # 5. Duplicates within a chain. The better row of each pair survives:
    #    known-open beats unknown status, then higher confidence, then id.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE street_keys AS
        SELECT address, street_key(address) AS skey
        FROM (SELECT DISTINCT address FROM assigned
              WHERE chain_key IS NOT NULL AND address IS NOT NULL)
    """)
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE dups AS
        WITH c AS (
            SELECT a.id, a.chain_key, sk.skey, a.lat, a.lon,
                   (a.operating_status = 'open')::INT AS is_open, a.confidence
            FROM assigned a LEFT JOIN street_keys sk USING (address)
            WHERE a.chain_key IS NOT NULL
        )
        SELECT DISTINCT b.id
        FROM c a JOIN c b
          ON a.chain_key = b.chain_key AND a.id <> b.id
         AND abs(a.lat - b.lat) < 0.01   -- ~1.1 km prefilter before exact distance
        WHERE (
                (a.skey = b.skey
                 AND haversine_m(a.lat, a.lon, b.lat, b.lon) <= $same_street_m)
                OR haversine_m(a.lat, a.lon, b.lat, b.lon) <= $any_m
              )
          AND (coalesce(a.is_open, 0), a.confidence, a.id)
              > (coalesce(b.is_open, 0), b.confidence, b.id)
        """,
        {"same_street_m": rules.dup_same_street_m, "any_m": rules.dup_any_m},
    )

    con.execute("""
        CREATE OR REPLACE TABLE places_out AS
        SELECT id, name, brand_name, chain_key, chain_match,
               basic_category, taxonomy_primary, confidence, operating_status,
               address, locality, state, postcode, lat, lon, source_datasets,
               id IN (SELECT id FROM dups) AS is_duplicate
        FROM assigned
    """)
