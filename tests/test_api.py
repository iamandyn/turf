"""API tests against a small serving database built with the real builder."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from turf_app.api import create_app
from turf_app.ratelimit import RateLimiter, Rule


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


def test_meta_reports_commit(client: TestClient) -> None:
    assert client.get("/api/meta").json()["commit"] == "dev"


def test_rate_limit_returns_429_with_retry_after(serving_db: Path) -> None:
    limiter = RateLimiter(rules=(Rule("/api/", limit=2, window_s=60),))
    with TestClient(create_app(serving_db, rate_limiter=limiter)) as c:
        assert [c.get("/api/meta").status_code for _ in range(2)] == [200, 200]
        resp = c.get("/api/meta")
        assert resp.status_code == 429
        assert int(resp.headers["retry-after"]) >= 1
        # Health checks are never limited.
        assert c.get("/healthz").status_code == 200


def test_rate_limit_uses_forwarded_client_ip(serving_db: Path) -> None:
    # The TestClient's client host is "testclient"; different hosts get separate budgets.
    limiter = RateLimiter(rules=(Rule("/api/", limit=1, window_s=60),))
    app = create_app(serving_db, rate_limiter=limiter)
    with (
        TestClient(app, client=("203.0.113.1", 1000)) as a,
        TestClient(app, client=("203.0.113.2", 1000)) as b,
    ):
        assert a.get("/api/meta").status_code == 200
        assert a.get("/api/meta").status_code == 429
        assert b.get("/api/meta").status_code == 200


def test_rate_limit_keys_on_configured_client_ip_header(
    serving_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TURF_CLIENT_IP_HEADER", "X-Client-IP")
    limiter = RateLimiter(rules=(Rule("/api/", limit=1, window_s=60),))
    with TestClient(create_app(serving_db, rate_limiter=limiter)) as c:
        assert c.get("/api/meta", headers={"X-Client-IP": "198.51.100.7"}).status_code == 200
        assert c.get("/api/meta", headers={"X-Client-IP": "198.51.100.7"}).status_code == 429
        assert c.get("/api/meta", headers={"X-Client-IP": "198.51.100.8"}).status_code == 200


def test_x_forwarded_for_is_ignored(serving_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A visitor can't dodge the limit by sending their own X-Forwarded-For.
    monkeypatch.setenv("TURF_CLIENT_IP_HEADER", "X-Client-IP")
    limiter = RateLimiter(rules=(Rule("/api/", limit=1, window_s=60),))
    with TestClient(create_app(serving_db, rate_limiter=limiter)) as c:
        headers = {"X-Client-IP": "198.51.100.7"}
        assert c.get("/api/meta", headers=headers).status_code == 200
        spoofed = {**headers, "X-Forwarded-For": "1.2.3.4"}
        assert c.get("/api/meta", headers=spoofed).status_code == 429


def test_frontend_served_with_security_headers(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "<title>Turf" in resp.text
    csp = resp.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_frontend_assets_and_vendored_maplibre(client: TestClient) -> None:
    for path in ("/app.js", "/styles.css", "/vendor/maplibre-gl-4.7.1/maplibre-gl.js"):
        assert client.get(path).status_code == 200, path


def test_api_responses_have_no_csp(client: TestClient) -> None:
    resp = client.get("/api/meta")
    assert "content-security-policy" not in resp.headers
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_unknown_api_path_is_404_not_frontend(client: TestClient) -> None:
    assert client.get("/api/nope").status_code == 404
