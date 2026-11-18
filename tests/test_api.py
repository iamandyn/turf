"""API tests against a small serving database built with the real builder."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_meta_counts_exclude_duplicates_and_closed_chains(client: TestClient) -> None:
    body = client.get("/api/meta").json()
    assert body["release"] == "test-release"
    assert body["places"] == 13  # sb4 is a duplicate
    assert body["chains"] == 6  # "old shop" has no open locations


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
