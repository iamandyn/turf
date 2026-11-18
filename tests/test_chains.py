"""Chain assignment and duplicate rules, on small tables shaped like real cases."""

from __future__ import annotations

import duckdb
import pytest

from turf_app.chains import ChainRules, build

COLUMNS = (
    "id, name, brand_name, basic_category, taxonomy_primary, confidence, "
    "operating_status, address, locality, region, postcode, lat, lon, source_datasets"
)


def place(
    id: str,
    name: str,
    brand: str | None,
    category: str | None,
    lat: float,
    lon: float,
    address: str | None = None,
    status: str | None = "open",
    confidence: float = 0.9,
    region: str | None = "US-TX",
) -> tuple[object, ...]:
    return (
        id, name, brand, category, category, confidence, status,
        address, None, region, None, lat, lon, ["test"],
    )  # fmt: skip


def run(
    rows: list[tuple[object, ...]], rules: ChainRules | None = None
) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE places (id VARCHAR, name VARCHAR, brand_name VARCHAR, "
        "basic_category VARCHAR, taxonomy_primary VARCHAR, confidence DOUBLE, "
        "operating_status VARCHAR, address VARCHAR, locality VARCHAR, region VARCHAR, "
        "postcode VARCHAR, lat DOUBLE, lon DOUBLE, source_datasets VARCHAR[])"
    )
    con.executemany(f"INSERT INTO places ({COLUMNS}) VALUES ({', '.join('?' * 14)})", rows)
    build(con, rules or ChainRules(min_tagged=2))
    return con


def chain_of(con: duckdb.DuckDBPyConnection, id: str) -> str | None:
    row = con.execute("SELECT chain_key FROM places_out WHERE id = ?", [id]).fetchone()
    assert row is not None
    key: str | None = row[0]
    return key


def is_dup(con: duckdb.DuckDBPyConnection, id: str) -> bool:
    row = con.execute("SELECT is_duplicate FROM places_out WHERE id = ?", [id]).fetchone()
    assert row is not None
    return bool(row[0])


# Spread test locations ~10 km apart so only intended pairs are near each other.
def far(i: int) -> tuple[float, float]:
    return 30.0 + i * 0.1, -97.0


ORANGETHEORY = [
    place(f"otf{i}", "Orangetheory Fitness", "Orangetheory Fitness", "gym", *far(i))
    for i in range(4)
]


def test_location_named_place_with_trusted_brand_joins_chain() -> None:
    # Real case: brand "Orangetheory Fitness", name "Mansfield, TX", category gym.
    con = run([*ORANGETHEORY, place("x", "Mansfield, TX", "Orangetheory Fitness", "gym", *far(9))])
    assert chain_of(con, "x") == "orangetheory fitness"


def test_brand_tag_outside_chain_categories_and_name_rejected() -> None:
    con = run(
        [*ORANGETHEORY, place("x", "Joe's Tacos", "Orangetheory Fitness", "restaurant", *far(9))]
    )
    assert chain_of(con, "x") is None


def test_unbranded_place_joins_chain_by_name() -> None:
    con = run([*ORANGETHEORY, place("x", "Orangetheory Fitness", None, "gym", *far(9))])
    assert chain_of(con, "x") == "orangetheory fitness"


def test_name_match_requires_chain_category() -> None:
    subway = [place(f"s{i}", "Subway", "Subway", "restaurant", *far(i)) for i in range(4)]
    con = run([*subway, place("x", "Subway Station Parking", None, "parking", *far(9))])
    assert chain_of(con, "x") is None


def test_untrusted_brand_is_ignored() -> None:
    # Real case: one brand tagged on many individual professionals.
    people = [
        place(f"p{i}", n, "Anh Phương", "personal_or_beauty_service", *far(i))
        for i, n in enumerate(
            ["Ashley Lewis", "James M Marshall", "James H Grant", "Sophia Robinson"]
        )
    ]
    con = run(people)
    assert con.execute("SELECT count(*) FROM chains WHERE chain_key = 'anh phuong'").fetchone() == (
        0,
    )
    assert all(chain_of(con, f"p{i}") is None for i in range(4))


def test_suffix_variants_merge_into_one_chain() -> None:
    rows = [
        place("a", "Starbucks", "Starbucks", "coffee_shop", *far(0)),
        place("b", "Starbucks Coffee Company", "Starbucks Coffee Company", "coffee_shop", *far(1)),
        place("c", "Starbucks Coffee Company", None, "coffee_shop", *far(2)),
    ]
    con = run(rows)
    assert {chain_of(con, i) for i in "abc"} == {"starbucks"}


def test_excluded_kiosk_chain_is_not_a_chain() -> None:
    lockers = [
        place(f"l{i}", "Amazon Locker", "Amazon Locker", "shipping_center", *far(i))
        for i in range(4)
    ]
    con = run(lockers)
    assert chain_of(con, "l0") is None


def test_same_street_number_within_500m_is_duplicate() -> None:
    # Real case: "9500 S Ih 35 Ste L800" vs "9500 S Interstate 35 Frontage Rd Ste L800", ~450 m.
    rows = [
        *ORANGETHEORY,
        place("keep", "Orangetheory Fitness", "Orangetheory Fitness", "gym", 30.1658, -97.7926,
              address="9500 S Interstate 35 Frontage Rd Ste L800", status="open", confidence=0.92),
        place("drop", "Orangetheory Fitness", None, "gym", 30.1621, -97.7895,
              address="9500 S Ih 35 Ste L800", status=None, confidence=0.77),
    ]  # fmt: skip
    con = run(rows)
    assert not is_dup(con, "keep")
    assert is_dup(con, "drop")


def test_different_street_numbers_nearby_are_not_duplicates() -> None:
    rows = [
        *ORANGETHEORY,
        place("a", "Orangetheory Fitness", None, "gym", 30.2000, -97.7000, address="100 Main St"),
        place("b", "Orangetheory Fitness", None, "gym", 30.2010, -97.7000, address="250 Main St"),
    ]
    con = run(rows)
    assert not is_dup(con, "a")
    assert not is_dup(con, "b")


def test_same_spot_without_address_is_duplicate() -> None:
    rows = [
        *ORANGETHEORY,
        place("a", "Orangetheory Fitness", None, "gym", 30.2, -97.7, confidence=0.95),
        place("b", "Orangetheory Fitness", None, "gym", 30.2001, -97.7, confidence=0.6),
    ]
    con = run(rows)
    assert not is_dup(con, "a")
    assert is_dup(con, "b")


@pytest.mark.parametrize(("region", "state"), [("US-TX", "TX"), ("Texas", "TX"), ("US-PR", None)])
def test_state_normalized(region: str, state: str | None) -> None:
    con = run([place("a", "Somewhere", None, None, 30.0, -97.0, region=region)])
    assert con.execute("SELECT state FROM places_out").fetchone() == (state,)
