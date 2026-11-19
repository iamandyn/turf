"""API tests against a small serving database built with the real builder."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from turf_app import api
from turf_app.api import create_app
from turf_app.ratelimit import RateLimiter, Rule


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


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
    assert "script-src 'self' https://static.cloudflareinsights.com" in csp
    assert "frame-ancestors 'none'" in csp
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_page_references_versioned_assets(client: TestClient) -> None:
    html = client.get("/").text
    for name in ("theme.js", "styles.css", "app.js"):
        match = re.search(rf'"/{re.escape(name)}\?v=([0-9a-f]{{12}})"', html)
        assert match, name
        resp = client.get(f"/{name}?v={match.group(1)}")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get("/index.html").text == html


def test_asset_version_changes_with_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("index.html", "theme.js", "styles.css", "app.js"):
        (tmp_path / name).write_bytes((api.WEB_DIR / name).read_bytes())
    monkeypatch.setattr(api, "WEB_DIR", tmp_path)
    before = api.render_index()
    (tmp_path / "app.js").write_text("// changed")
    after = api.render_index()
    assert before != after and "styles.css?v=" in after


def test_unversioned_assets_revalidate(client: TestClient) -> None:
    assert client.get("/app.js").headers["cache-control"] == "no-cache"


def test_frontend_assets_and_vendored_maplibre(client: TestClient) -> None:
    for path in ("/app.js", "/styles.css", "/vendor/maplibre-gl-4.7.1/maplibre-gl.js"):
        assert client.get(path).status_code == 200, path


def test_api_responses_have_no_csp(client: TestClient) -> None:
    resp = client.get("/api/meta")
    assert "content-security-policy" not in resp.headers
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_unknown_api_path_is_404_not_frontend(client: TestClient) -> None:
    assert client.get("/api/nope").status_code == 404
