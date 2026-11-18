"""API tests against a small serving database built with the real builder."""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from turf_app.api import create_app
from turf_app.serving import write_serving_db

PLACES = [
    # id, name, chain_key, category, confidence, status, state, lat, lon, is_duplicate
    ("sb1", "Starbucks", "starbucks", "coffee_shop", 0.99, "open", "TX", 30.27, -97.74, False),
    ("sb2", "Starbucks Coffee Company", "starbucks", "coffee_shop", 0.9, "open", "CA", 37.7, -122.4, False),
    ("sb3", "Starbucks", "starbucks", "coffee_shop", 0.8, None, "TX", 30.3, -97.7, False),
    ("sb4", "Starbucks", "starbucks", "coffee_shop", 0.7, "open", "TX", 30.31, -97.71, True),
    ("otf1", "Orangetheory Fitness", "orangetheory fitness", "gym", 0.99, "open", "TX", 30.2, -97.8, False),
    ("otf2", "Orangetheory Fitness", "orangetheory fitness", "gym", 0.99, "open", "CA", 34.0, -118.2, False),
    ("ot1", "Orangetheory", "orangetheory", "gym", 0.9, "open", "TX", 30.5, -97.9, False),
    ("pf1", "Planet Fitness", "planet fitness", "gym", 0.99, "open", "TX", 30.4, -97.7, False),
    ("ss1", "Starship Bagel", "starship bagel", "bakery", 0.9, "open", "TX", 30.1, -97.6, False),
    ("x1", "Old Shop", "old shop", "retail", 0.9, None, "TX", 30.0, -97.0, False),
    ("ind", "Joe's Coffee", None, "coffee_shop", 0.9, "open", "TX", 30.0, -97.5, False),
]  # fmt: skip

CHAINS = [
    ("starbucks", "Starbucks", ["coffee_shop"]),
    ("orangetheory fitness", "Orangetheory Fitness", ["gym"]),
    ("orangetheory", "Orangetheory", ["gym"]),
    ("planet fitness", "Planet Fitness", ["gym"]),
    ("starship bagel", "Starship Bagel", ["bakery"]),
    ("old shop", "Old Shop", ["retail"]),  # no open locations -> not served
]


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    d = tmp_path_factory.mktemp("serving")
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE p (id VARCHAR, name VARCHAR, chain_key VARCHAR, basic_category VARCHAR, "
        "confidence DOUBLE, operating_status VARCHAR, state VARCHAR, lat DOUBLE, lon DOUBLE, "
        "is_duplicate BOOLEAN)"
    )
    con.executemany("INSERT INTO p VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", PLACES)
    con.execute(f"""
        COPY (SELECT *, '1 Main St' AS address, 'Austin' AS locality, '78701' AS postcode FROM p)
        TO '{d / "places.parquet"}' (FORMAT parquet)
    """)
    con.execute("CREATE TABLE c (chain_key VARCHAR, display_name VARCHAR, categories VARCHAR[])")
    con.executemany("INSERT INTO c VALUES (?, ?, ?)", CHAINS)
    con.execute(f"COPY c TO '{d / 'chains.parquet'}' (FORMAT parquet)")
    db = d / "turf.duckdb"
    write_serving_db(d / "places.parquet", d / "chains.parquet", db, "test-release")
    with TestClient(create_app(db)) as c:
        yield c


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_meta_counts_exclude_duplicates_and_closed_chains(client: TestClient) -> None:
    body = client.get("/api/meta").json()
    assert body["release"] == "test-release"
    assert body["places"] == 10  # sb4 is a duplicate
    assert body["chains"] == 5  # "old shop" has no open locations


def test_search_prefix_ranks_bigger_chain_first(client: TestClient) -> None:
    keys = [c["chain_key"] for c in client.get("/api/chains/search?q=star").json()]
    assert keys == ["starbucks", "starship bagel"]


def test_search_normalizes_query(client: TestClient) -> None:
    keys = [c["chain_key"] for c in client.get("/api/chains/search?q=STARBUCKS!").json()]
    assert keys[0] == "starbucks"


def test_search_word_start_match(client: TestClient) -> None:
    keys = [c["chain_key"] for c in client.get("/api/chains/search?q=fitness").json()]
    assert set(keys) == {"orangetheory fitness", "planet fitness"}


def test_search_exact_small_chain_ranks_below_bigger_prefix_match(client: TestClient) -> None:
    # Real case: "orangetheory" (3 locations) vs "orangetheory fitness" (1,275).
    keys = [c["chain_key"] for c in client.get("/api/chains/search?q=orangetheory").json()]
    assert keys == ["orangetheory fitness", "orangetheory"]


def test_search_tolerates_typo(client: TestClient) -> None:
    keys = [c["chain_key"] for c in client.get("/api/chains/search?q=starbuks").json()]
    assert keys[0] == "starbucks"


def test_search_counts(client: TestClient) -> None:
    sb = client.get("/api/chains/search?q=starbucks").json()[0]
    assert (sb["n_open"], sb["n_total"]) == (2, 3)


def test_search_rejects_short_query(client: TestClient) -> None:
    assert client.get("/api/chains/search?q=s").status_code == 422


def test_locations_open_only_by_default(client: TestClient) -> None:
    body = client.get("/api/chains/starbucks/locations").json()
    assert body["include_unverified"] is False
    assert {loc["id"] for loc in body["locations"]} == {"sb1", "sb2"}


def test_locations_include_unverified(client: TestClient) -> None:
    body = client.get("/api/chains/starbucks/locations?include_unverified=true").json()
    assert {loc["id"] for loc in body["locations"]} == {"sb1", "sb2", "sb3"}


def test_locations_state_filter_case_insensitive(client: TestClient) -> None:
    body = client.get("/api/chains/starbucks/locations?state=tx").json()
    assert {loc["id"] for loc in body["locations"]} == {"sb1"}


def test_locations_unknown_chain_404(client: TestClient) -> None:
    assert client.get("/api/chains/nope/locations").status_code == 404


def test_locations_url_encoded_chain_key(client: TestClient) -> None:
    resp = client.get("/api/chains/orangetheory%20fitness/locations")
    assert resp.status_code == 200
    assert {loc["id"] for loc in resp.json()["locations"]} == {"otf1", "otf2"}


def test_invalid_state_rejected(client: TestClient) -> None:
    assert client.get("/api/chains/starbucks/locations?state=Texas").status_code == 422
