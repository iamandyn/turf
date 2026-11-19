"""What changed between two Overture releases, per chain: new and gone locations.

Two kinds of noise had to be removed (measured on 2026-10-21.0 → 2026-11-18.0):

* **Same store, new record.** A place's ID is usually stable across releases,
  but sources come and go, so a store can reappear under a new ID, often with
  shifted coordinates (of 255 "new" Starbucks by ID alone, 141 had a Starbucks
  within 500 m the month before, 50 at the same address). So a location is only
  **new** if its ID wasn't there before, no location of the same chain was
  within ``match_m``, and none had the same address within ``ADDRESS_MATCH_M``.
  **Gone** is the same the other way round.
* **Unstable chains.** Whole "chains" appear or vanish when upstream brand tags
  change and chain matching (chains.py) groups places differently: "atm"
  (24,669 gone), "hair" (18,489 new), every Jiffy Lube (2,535 gone). A chain
  whose changes exceed ``MAX_CHURN`` of its size is left out entirely and listed
  in the report for review.

What remains still can't tell a real opening from a store Overture has only
just mapped, and coverage growth dominates for some chains (Subway: 2,142
"new" in a month, ~9% of the chain, while it's shrinking). So the product
treats it as list maintenance: "added since the <date> data" is what to add to
a campaign exported last month, "removed" is what to take out. It never claims
"opened" or "closed".

Only chain locations are compared: independents churn too much between
releases for a per-business diff to mean much.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

# Same chain within this distance in the other release = the same location.
# Duplicate detection uses 1 km for "same street address"; this is tighter,
# because two real locations of a chain a few hundred meters apart are common
# downtown and must not hide an opening.
DEFAULT_MATCH_M = 150.0
# Same chain and same normalized address within this distance = the same location.
ADDRESS_MATCH_M = 2_000.0
# A chain whose new + gone exceeds this share of its size (and at least
# MIN_CHURN_LOCATIONS) is treated as a chain-matching artifact, not real change.
MAX_CHURN = 0.25
MIN_CHURN_LOCATIONS = 5

_COLUMNS = "id, name, chain_key, status, address, locality, state, postcode, lat, lon"


@dataclass(frozen=True)
class ChangeReport:
    previous_chain_locations: int
    current_chain_locations: int
    new: int
    gone: int
    same_id: int  # matched by ID
    matched_nearby: int  # different ID, same chain nearby or at the same address
    unstable_chains: int  # left out entirely (see MAX_CHURN)
    unstable_examples: list[tuple[str, int, int]]  # (chain_key, new, gone), biggest first


def _side(name: str, path: Path) -> str:
    return f"""
        CREATE TEMP TABLE {name} AS
        SELECT id, name, chain_key, operating_status AS status, address, locality, state,
               postcode, lat, lon,
               nullif(regexp_replace(lower(address), '[^a-z0-9]', '', 'g'), '') AS addr_key
        FROM read_parquet('{path}')
        WHERE chain_key IS NOT NULL AND NOT is_duplicate
    """


def _unmatched(a: str, b: str, match_m: float) -> str:
    """Rows of ``a`` with no same-ID row in ``b``, and no same-chain row of ``b``
    nearby or at the same address."""
    # Prefilter window: degrees of latitude for the address distance, with margin;
    # longitude gets twice that (narrower than latitude across the US below ~60°N).
    addr_deg = ADDRESS_MATCH_M / 111_000 * 1.5
    dist = """2 * 6371008.8 * asin(sqrt(
        pow(sin(radians(y.lat - x.lat) / 2), 2)
        + cos(radians(x.lat)) * cos(radians(y.lat)) * pow(sin(radians(y.lon - x.lon) / 2), 2)))"""
    return f"""
        SELECT x.* FROM {a} x
        WHERE NOT EXISTS (SELECT 1 FROM {b} y WHERE y.id = x.id)
          AND NOT EXISTS (
              SELECT 1 FROM {b} y
              WHERE y.chain_key = x.chain_key
                AND abs(y.lat - x.lat) < {addr_deg} AND abs(y.lon - x.lon) < {addr_deg * 2}
                AND (
                    {dist} <= {match_m}
                    OR (y.addr_key = x.addr_key AND {dist} <= {ADDRESS_MATCH_M})
                )
          )
    """


def build_changes(
    con: duckdb.DuckDBPyConnection,
    current: Path,
    previous: Path,
    out: Path,
    match_m: float = DEFAULT_MATCH_M,
) -> ChangeReport:
    """Write ``out`` (parquet: change ('new' | 'gone') + place columns) and report counts.

    ``current`` and ``previous`` are places.parquet files from build_chains.py.
    """
    con.execute(_side("cur", current))
    con.execute(_side("prev", previous))
    con.execute(f"""
        CREATE TEMP TABLE raw_changes AS
        SELECT 'new' AS change, {_COLUMNS} FROM ({_unmatched("cur", "prev", match_m)})
        UNION ALL
        SELECT 'gone' AS change, {_COLUMNS} FROM ({_unmatched("prev", "cur", match_m)})
    """)
    # Per-chain churn against the chain's size in either release.
    con.execute(f"""
        CREATE TEMP TABLE unstable AS
        WITH sizes AS (
            SELECT chain_key, count(*) AS n FROM prev GROUP BY 1
            UNION ALL
            SELECT chain_key, count(*) FROM cur GROUP BY 1
        ),
        size AS (SELECT chain_key, max(n) AS n FROM sizes GROUP BY 1),
        churn AS (
            SELECT chain_key,
                   count(*) FILTER (WHERE change = 'new') AS new,
                   count(*) FILTER (WHERE change = 'gone') AS gone
            FROM raw_changes GROUP BY 1
        )
        SELECT c.chain_key, c.new, c.gone
        FROM churn c JOIN size s USING (chain_key)
        WHERE c.new + c.gone > greatest({MIN_CHURN_LOCATIONS}, {MAX_CHURN} * s.n)
    """)
    con.execute(f"""
        COPY (
            SELECT * FROM raw_changes
            WHERE chain_key NOT IN (SELECT chain_key FROM unstable)
            ORDER BY chain_key, change, state, id
        ) TO '{out}' (FORMAT parquet, COMPRESSION zstd)
    """)
    row = con.execute(f"""
        SELECT (SELECT count(*) FROM prev), (SELECT count(*) FROM cur),
               count(*) FILTER (WHERE change = 'new'), count(*) FILTER (WHERE change = 'gone'),
               (SELECT count(*) FROM cur JOIN prev USING (id)),
               (SELECT count(*) FROM unstable),
               (SELECT count(*) FROM raw_changes WHERE change = 'new')
        FROM read_parquet('{out}')
    """).fetchone()
    assert row is not None
    prev_n, cur_n, new, gone, same_id, n_unstable, raw_new = (int(v) for v in row)
    examples = con.execute(
        "SELECT chain_key, new, gone FROM unstable ORDER BY new + gone DESC LIMIT 10"
    ).fetchall()
    return ChangeReport(
        previous_chain_locations=prev_n,
        current_chain_locations=cur_n,
        new=new,
        gone=gone,
        same_id=same_id,
        matched_nearby=cur_n - same_id - raw_new,
        unstable_chains=n_unstable,
        unstable_examples=[(str(k), int(n), int(g)) for k, n, g in examples],
    )
