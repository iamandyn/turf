"""Market selection: ranking, lookalikes, reasons, exports, presets, briefs.

Fixture (conftest.py): metro i (10000 + i) has 100 restaurants, i pet groomers
and 12 - i dog parks, population 300k + 100k * i, growth rising with i; 10011
is a micro area. The default universe (metros, >= 250k) is 10000-10010.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastapi.testclient import TestClient

from turf_app import briefs
from turf_app.api import create_app
from turf_app.markets import PRESETS, zip_rows
from turf_app.ratelimit import RateLimiter


def markets(client: TestClient, **body: object) -> dict[str, Any]:
    resp = client.post("/api/markets", json=body)
    assert resp.status_code == 200, resp.text
    data: dict[str, Any] = resp.json()
    return data


def codes(data: dict[str, Any]) -> list[str]:
    return [r["metro_code"] for r in data["results"]]


# --- Signal strength (no seeds) ---


def test_signal_strength_ranks_by_share(client: TestClient) -> None:
    data = markets(client, categories=["pet_groomer"])
    assert data["mode"] == "signal_strength" and data["universe"] == 11
    assert codes(data)[:3] == ["10010", "10009", "10008"]
    assert all(r["match"] is None for r in data["results"])
    assert codes(markets(client, categories=["dog_park"]))[0] == "10000"


def test_shares_count_open_unique_places_only(client: TestClient) -> None:
    # Metro 10 has 100 restaurants + 10 groomers + 2 dog parks open; its 50
    # duplicate and 50 unverified groomers don't count.
    top = markets(client, categories=["pet_groomer"])["results"][0]
    reason = top["reasons"][0]
    assert reason["signal"] == "pet_groomer"
    assert reason["value"] == pytest.approx(100 * 10 / 112, abs=0.001)
    assert reason["ratio"] > 1.5  # vs the median metro (metro 5)
    assert reason["label"] == "pet groomer"


def test_growth_signal(client: TestClient) -> None:
    data = markets(client, use_growth=True)
    assert codes(data)[0] == "10010"  # fastest growing metro (10011 is a micro area)
    reason = data["results"][0]["reasons"][0]
    assert reason["signal"] == "population_growth" and reason["value"] == pytest.approx(10.0, 0.01)


def test_population_and_kind_filters(client: TestClient) -> None:
    assert (
        markets(client, categories=["pet_groomer"], include_micro=True, min_population=0)[
            "universe"
        ]
        == 12
    )
    resp = client.post(
        "/api/markets", json={"categories": ["pet_groomer"], "min_population": 1_000_000}
    )
    assert resp.status_code == 422 and "lower the minimum population" in resp.json()["detail"]


# --- Lookalikes ---


def test_lookalikes_are_nearest_profiles(client: TestClient) -> None:
    data = markets(client, categories=["pet_groomer", "dog_park"], seeds=["10005"])
    assert data["mode"] == "lookalike"
    assert set(codes(data)[:2]) == {"10004", "10006"}
    assert "10005" not in codes(data)
    assert [s["metro_code"] for s in data["seeds"]] == ["10005"]
    matches = [r["match"] for r in data["results"]]
    assert matches == sorted(matches, reverse=True) and 0 <= matches[-1] < matches[0] <= 100


def test_several_seeds_use_their_average(client: TestClient) -> None:
    data = markets(client, categories=["pet_groomer", "dog_park"], seeds=["10002", "10008"])
    assert codes(data)[0] == "10005"  # halfway between them


def test_seed_outside_the_filters_still_counts(client: TestClient) -> None:
    data = markets(client, categories=["pet_groomer"], seeds=["10000"], min_population=500_000)
    assert data["universe"] == 10  # metros 2-10 plus the seed
    assert data["seeds"][0]["metro_code"] == "10000"


def test_preset(client: TestClient) -> None:
    data = markets(client, preset="pets", limit=3)
    assert data["signals"] == list(PRESETS["pets"].categories)
    assert len(data["results"]) == 3
    assert data["results"][0]["zip_count"] == 3


# --- Validation ---


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"categories": ["nope"]}, 404),
        ({"categories": ["pet_groomer"], "seeds": ["99999"]}, 404),
        ({"categories": ["pet_groomer"], "seeds": ["Austin"]}, 422),
        ({}, 422),  # no signals
        ({"preset": "nope"}, 422),
        ({"categories": ["pet_groomer"], "radius_mi": 3}, 422),  # unknown field
        ({"categories": ["pet_groomer"], "limit": 0}, 422),
    ],
)
def test_validation(client: TestClient, body: dict[str, object], status: int) -> None:
    assert client.post("/api/markets", json=body).status_code == status


# --- Exports ---


def test_export_metros(client: TestClient) -> None:
    resp = client.post(
        "/api/markets/export?format=metros",
        json={"preset": "pets", "seeds": ["10005"], "limit": 4},
    )
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    assert (
        resp.headers["content-disposition"]
        == 'attachment; filename="turf-pets-lookalikes-metros.csv"'
    )
    header, *rows = list(csv.reader(io.StringIO(resp.text)))
    assert header[:3] == ["rank", "metro", "cbsa_code"] and len(rows) == 4
    assert rows[0][header.index("match")] != "" and "x typical" in rows[0][-1]


def test_export_zips(client: TestClient) -> None:
    resp = client.post(
        "/api/markets/export?format=zips", json={"categories": ["dog_park"], "limit": 2}
    )
    header, *rows = list(csv.reader(io.StringIO(resp.text)))
    assert header == ["zip", "country", "metro", "metro_rank"]
    # Metros 0 and 1 rank first on dog parks; three ZIPs each, in rank order.
    assert [r[0] for r in rows] == ["70000", "70001", "70002", "70010", "70011", "70012"]
    assert {r[1] for r in rows} == {"US"} and rows[0][3] == "1"
    assert resp.headers["content-disposition"].endswith('turf-custom-top-zips.csv"')


def test_zip_assignment(serving_db: Path, tmp_path: Path) -> None:
    # A copy: the session's API client already holds the file open.
    copy = tmp_path / "turf.duckdb"
    copy.write_bytes(serving_db.read_bytes())
    db = duckdb.connect(str(copy), read_only=True)
    zips = {z for _, z, _ in zip_rows(db, ["10000", "10001"])}
    assert "70000" in zips  # the stray metro-1 place doesn't move it
    assert "79999" not in zips  # one place: below the minimum
    assert ("10001", "70000") not in {(m, z) for m, z, _ in zip_rows(db, ["10001"])}


# --- Presets, search, metros, meta ---


def test_presets(client: TestClient) -> None:
    presets = client.get("/api/presets").json()
    assert [p["key"] for p in presets] == list(PRESETS)
    assert presets[0]["categories"] == list(PRESETS["pets"].categories)
    assert not any(p["has_briefs"] for p in presets)


def test_category_search(client: TestClient) -> None:
    assert client.get("/api/categories/search?q=groom").json()[0]["key"] == "pet_groomer"
    assert client.get("/api/categories/search?q=pet%20grommer").json()[0]["key"] == "pet_groomer"
    assert client.get("/api/categories/search?q=p").status_code == 422


def test_metro_search_and_get(client: TestClient) -> None:
    (austin,) = client.get("/api/metros/search?q=round%20rock").json()
    assert austin["metro_code"] == "10003" and austin["population"] == 600_000
    assert austin["growth_pct"] == pytest.approx(3.0, 0.01)
    min_lon, min_lat, max_lon, max_lat = austin["bounds"]
    assert min_lon < max_lon and min_lat < max_lat
    assert client.get("/api/metros/10003").json()["name"].startswith("Austin")
    assert client.get("/api/metros/99999").status_code == 404


def test_meta(client: TestClient) -> None:
    meta = client.get("/api/meta").json()
    assert (meta["metros"], meta["briefs"], meta["release"]) == (12, 0, "test-release")


# --- Briefs ---

BRIEF = {
    "preset": "pets",
    "metro_code": "10003",
    "metro_name": "Austin-Round Rock-San Marcos, TX",
    "headline": "Austin's dogs have a busy calendar",
    "audience": "Dog owners in fast-growing suburbs",
    "angles": ["Same-day walks"],
    "local_context": ["Dog parks"],
    "grounded_in": ["dog walkers 2.4x typical share"],
}


def test_no_brief_is_404(client: TestClient) -> None:
    assert client.get("/api/briefs/pets/10003").status_code == 404


def test_briefs_loaded_and_served(
    serving_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "briefs.json"
    path.write_text(json.dumps([BRIEF]))
    monkeypatch.setattr(briefs, "BRIEFS_FILE", path)
    with TestClient(create_app(serving_db, rate_limiter=RateLimiter(rules=()))) as c:
        assert c.get("/api/briefs/pets/10003").json()["headline"] == BRIEF["headline"]
        assert c.get("/api/meta").json()["briefs"] == 1
        assert next(p for p in c.get("/api/presets").json() if p["key"] == "pets")["has_briefs"]


def test_shipped_briefs_are_valid() -> None:
    loaded = briefs.load()
    for (preset, code), brief in loaded.items():
        assert preset in PRESETS and len(code) == 5 and brief.grounded_in
