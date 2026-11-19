"""Target list and export tests, using the shared fixture in conftest.py."""

from __future__ import annotations

import csv
import io
from typing import Any

import pytest
from fastapi.testclient import TestClient

from turf_app import targets


def post(client: TestClient, **body: object) -> dict[str, Any]:
    resp = client.post("/api/targets", json=body)
    assert resp.status_code == 200, resp.text
    data: dict[str, Any] = resp.json()
    return data


def target_ids(data: dict[str, Any]) -> set[str]:
    return {t["id"] for t in data["targets"]}


def test_defaults_to_open_locations_with_one_mile_radius(client: TestClient) -> None:
    data = post(client, chain_keys=["starbucks"])
    assert target_ids(data) == {"sb1", "sb2"}  # sb3 unverified, sb4 duplicate
    assert data["radius_mi"] == 1.0
    assert (data["candidates"], data["dropped_count"]) == (2, 0)
    assert data["own_filter"] is None


def test_state_filter(client: TestClient) -> None:
    assert target_ids(post(client, chain_keys=["starbucks"], states=["ca"])) == {"sb2"}


def test_excludes_targets_near_own_locations(client: TestClient) -> None:
    data = post(
        client,
        chain_keys=["starbucks"],
        own_chain_key="dunkin",
        own_filter="skip_near",
        own_radius_mi=1,
    )
    assert target_ids(data) == {"sb2"}  # nearest Dunkin' is ~7 mi away
    excluded = data["dropped"]
    assert isinstance(excluded, list) and len(excluded) == 1
    assert excluded[0]["id"] == "sb1"
    assert excluded[0]["reason"] == "near_yours"
    assert excluded[0]["nearest_own_location"] == "dk1 Main St"
    assert 0.3 < excluded[0]["distance_mi"] < 0.4
    assert (excluded[0]["lat"], excluded[0]["lon"]) == (30.27, -97.74)  # for drawing on the map


def test_unverified_own_locations_still_exclude(client: TestClient) -> None:
    # sb3 (unverified competitor) sits ~0.6 mi from dk2 (unverified own location).
    data = post(
        client,
        chain_keys=["starbucks"],
        include_unverified=True,
        own_chain_key="dunkin",
        own_filter="skip_near",
        own_radius_mi=1,
    )
    excluded = data["dropped"]
    assert isinstance(excluded, list)
    assert {e["id"] for e in excluded} == {"sb1", "sb3"}


def test_exclusion_radius_respected(client: TestClient) -> None:
    # At 0.5 mi, sb3 (0.6 mi from dk2) is no longer excluded.
    data = post(
        client,
        chain_keys=["starbucks"],
        include_unverified=True,
        own_chain_key="dunkin",
        own_filter="skip_near",
        own_radius_mi=0.5,
    )
    assert target_ids(data) == {"sb2", "sb3"}


def test_large_exclusion_radius_reaches_far_locations(client: TestClient) -> None:
    data = post(
        client,
        chain_keys=["starbucks"],
        own_chain_key="dunkin",
        own_filter="skip_near",
        own_radius_mi=10,
    )
    assert target_ids(data) == set()


def test_radius_limits_follow_meta_us_range(client: TestClient) -> None:
    for radius in (0.5, 51):
        resp = client.post("/api/targets", json={"chain_keys": ["starbucks"], "radius_mi": radius})
        assert resp.status_code == 422


def test_unknown_chain_404(client: TestClient) -> None:
    assert client.post("/api/targets", json={"chain_keys": ["nope"]}).status_code == 404


def test_unknown_own_chain_404(client: TestClient) -> None:
    resp = client.post("/api/targets", json={"chain_keys": ["starbucks"], "own_chain_key": "nope"})
    assert resp.status_code == 404


def test_invalid_state_rejected(client: TestClient) -> None:
    resp = client.post("/api/targets", json={"chain_keys": ["starbucks"], "states": ["Texas"]})
    assert resp.status_code == 422


def export(client: TestClient, fmt: str, **body: object) -> tuple[list[list[str]], str]:
    resp = client.post(f"/api/targets/export?format={fmt}", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    return list(csv.reader(io.StringIO(resp.text))), resp.headers["content-disposition"]


def test_google_ads_editor_export_uses_proximity_format(client: TestClient) -> None:
    rows, disposition = export(
        client,
        "google_ads_editor",
        chain_keys=["starbucks"],
        radius_mi=2.5,
        campaign_name="Q1 Austin",
    )
    assert rows[0] == ["Campaign", "Location"]
    assert ["Q1 Austin", "(2.5mi:30.270000:-97.740000)"] in rows
    assert len(rows) == 3
    assert disposition == 'attachment; filename="turf-starbucks-google_ads_editor.csv"'


def test_google_ads_editor_whole_mile_radius_has_no_decimal(client: TestClient) -> None:
    rows, _ = export(client, "google_ads_editor", chain_keys=["starbucks"], states=["TX"])
    assert rows[1] == ["Turf - starbucks", "(1mi:30.270000:-97.740000)"]


def test_generic_export_columns_and_unverified_status(client: TestClient) -> None:
    rows, _ = export(
        client, "generic", chain_keys=["starbucks"], include_unverified=True, states=["TX"]
    )
    header, *body = rows
    assert header[:7] == ["name", "address", "city", "state", "postcode", "latitude", "longitude"]
    by_id = {r[header.index("overture_id")]: r for r in body}
    assert set(by_id) == {"sb1", "sb3"}
    assert by_id["sb3"][header.index("status")] == "unverified"
    assert by_id["sb1"][header.index("radius")] == "1"
    assert by_id["sb1"][header.index("radius_unit")] == "mi"


def test_export_excludes_same_as_json(client: TestClient) -> None:
    rows, _ = export(
        client, "generic", chain_keys=["starbucks"], own_chain_key="dunkin", own_filter="skip_near"
    )
    header, *body = rows
    assert [r[header.index("overture_id")] for r in body] == ["sb2"]


def test_export_rejects_unknown_format(client: TestClient) -> None:
    resp = client.post("/api/targets/export?format=pdf", json={"chain_keys": ["starbucks"]})
    assert resp.status_code == 422


# --- Category targets (every business of a kind, chains and independents) ---


def test_category_targets_include_independents(client: TestClient) -> None:
    # coffee_shop open in TX: sb1 (Starbucks), dk1 (Dunkin'), ind (Joe's Coffee, no chain).
    data = post(client, category="coffee_shop", states=["TX"])
    assert target_ids(data) == {"sb1", "dk1", "ind"}
    assert data["category"] == "coffee_shop" and data["chain_keys"] is None


def test_category_with_own_chain_skips_yours_and_nearby(client: TestClient) -> None:
    data = post(
        client,
        category="coffee_shop",
        states=["TX"],
        own_chain_key="dunkin",
        own_filter="skip_near",
        own_radius_mi=1,
    )
    assert target_ids(data) == {"ind"}  # dk1 is yours (0 mi); sb1 is 0.35 mi from it


def test_exactly_one_of_chain_or_category(client: TestClient) -> None:
    assert client.post("/api/targets", json={}).status_code == 422
    both = {"chain_keys": ["starbucks"], "category": "coffee_shop"}
    assert client.post("/api/targets", json=both).status_code == 422


def test_unknown_category_404(client: TestClient) -> None:
    assert client.post("/api/targets", json={"category": "nope"}).status_code == 404


def test_too_many_targets_asks_for_a_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(targets, "MAX_TARGETS", 2)
    resp = client.post("/api/targets", json={"category": "coffee_shop"})
    assert resp.status_code == 422
    assert "Pick a state" in resp.json()["detail"]
    # Narrowed to one state it fits again (sb2 + dk3 in CA).
    assert (
        client.post("/api/targets", json={"category": "coffee_shop", "states": ["CA"]}).status_code
        == 200
    )


def test_category_export_filename_and_campaign(client: TestClient) -> None:
    rows, disposition = export(client, "google_ads_editor", category="coffee_shop", states=["TX"])
    assert disposition == 'attachment; filename="turf-coffee-shop-google_ads_editor.csv"'
    assert {r[0] for r in rows[1:]} == {"Turf - coffee_shop"}


# --- Only near yours (the default when you give your own locations) ---


def test_only_near_is_the_default_and_keeps_targets_near_yours(client: TestClient) -> None:
    # Default 3 mi: sb1 is 0.35 mi from dk1; sb2 is ~7 mi from dk3.
    data = post(client, chain_keys=["starbucks"], own_chain_key="dunkin")
    assert data["own_filter"] == "only_near" and data["own_radius_mi"] == 3.0
    assert data["own_location_count"] == 3  # unverified dk2 counts too
    assert target_ids(data) == {"sb1"}
    (t,) = data["targets"]
    assert t["nearest_own_location"] == "dk1 Main St" and 0.3 < t["distance_mi"] < 0.4
    (d,) = data["dropped"]
    assert (d["id"], d["reason"], d["distance_mi"]) == ("sb2", "far_from_yours", None)


def test_only_near_radius_widens(client: TestClient) -> None:
    data = post(client, chain_keys=["starbucks"], own_chain_key="dunkin", own_radius_mi=10)
    assert target_ids(data) == {"sb1", "sb2"}


def test_skip_near_default_radius_is_one_mile(client: TestClient) -> None:
    data = post(client, chain_keys=["starbucks"], own_chain_key="dunkin", own_filter="skip_near")
    assert data["own_radius_mi"] == 1.0
    assert target_ids(data) == {"sb2"}


def test_own_chain_never_targeted_in_a_category(client: TestClient) -> None:
    # Coffee shops in TX near your Dunkin's: sb1 is near dk1; dk1 itself is yours.
    data = post(client, category="coffee_shop", states=["TX"], own_chain_key="dunkin")
    assert target_ids(data) == {"sb1"}
    assert "dk1" not in {d["id"] for d in data["dropped"]}


def test_own_chain_cannot_be_a_competitor(client: TestClient) -> None:
    body = {"chain_keys": ["starbucks", "dunkin"], "own_chain_key": "dunkin"}
    assert client.post("/api/targets", json=body).status_code == 422


# --- Several competitors in one list ---


def test_several_chains_in_one_list(client: TestClient) -> None:
    data = post(client, chain_keys=["starbucks", "orangetheory fitness", "starbucks"])
    assert data["chain_keys"] == ["starbucks", "orangetheory fitness"]  # deduplicated
    assert target_ids(data) == {"sb1", "sb2", "otf1", "otf2"}
    chains = {t["id"]: t["chain_key"] for t in data["targets"]}
    assert chains["otf1"] == "orangetheory fitness"


def test_chain_list_limits(client: TestClient) -> None:
    assert client.post("/api/targets", json={"chain_keys": []}).status_code == 422
    eleven = {"chain_keys": [f"c{i}" for i in range(targets.MAX_CHAINS + 1)]}
    assert client.post("/api/targets", json=eleven).status_code == 422
    assert (
        client.post("/api/targets", json={"chain_keys": ["starbucks", "nope"]}).status_code == 404
    )


def test_several_chains_filename_and_campaign(client: TestClient) -> None:
    rows, disposition = export(
        client, "google_ads_editor", chain_keys=["starbucks", "orangetheory fitness"]
    )
    assert disposition == 'attachment; filename="turf-starbucks-and-1-more-google_ads_editor.csv"'
    assert {r[0] for r in rows[1:]} == {"Turf - starbucks and 1 more"}


# --- Uploaded store lists ---

MY_GYM = {"name": "My Gym", "lat": 30.21, "lon": -97.8}  # ~0.7 mi from otf1


def test_uploaded_locations_work_like_an_own_chain(client: TestClient) -> None:
    data = post(client, chain_keys=["orangetheory fitness"], own_locations=[MY_GYM])
    assert data["own_location_count"] == 1
    assert target_ids(data) == {"otf1"}
    assert data["targets"][0]["nearest_own_location"] == "My Gym"


def test_uploaded_and_chain_locations_combine(client: TestClient) -> None:
    near_sb2 = {"lat": 37.701, "lon": -122.401}  # no name
    data = post(client, chain_keys=["starbucks"], own_chain_key="dunkin", own_locations=[near_sb2])
    assert data["own_location_count"] == 4
    assert target_ids(data) == {"sb1", "sb2"}
    by_id = {t["id"]: t for t in data["targets"]}
    assert by_id["sb2"]["nearest_own_location"] == "uploaded location"


def test_empty_upload_means_no_own_locations(client: TestClient) -> None:
    data = post(client, chain_keys=["starbucks"], own_locations=[])
    assert data["own_filter"] is None and target_ids(data) == {"sb1", "sb2"}


def test_uploaded_locations_validated(client: TestClient) -> None:
    bad = {"chain_keys": ["starbucks"], "own_locations": [{"lat": 91, "lon": 0}]}
    assert client.post("/api/targets", json=bad).status_code == 422
    too_many = [MY_GYM] * (targets.MAX_UPLOADED_LOCATIONS + 1)
    body = {"chain_keys": ["starbucks"], "own_locations": too_many}
    assert client.post("/api/targets", json=body).status_code == 422


def test_generic_export_has_distance_to_yours(client: TestClient) -> None:
    rows, _ = export(client, "generic", chain_keys=["orangetheory fitness"], own_locations=[MY_GYM])
    header, *body = rows
    assert len(body) == 1
    assert body[0][header.index("nearest_yours")] == "My Gym"
    assert 0.6 < float(body[0][header.index("miles_to_yours")]) < 0.8


def test_unknown_request_fields_rejected(client: TestClient) -> None:
    old = {"chain_keys": ["starbucks"], "exclude_chain_key": "dunkin"}
    assert client.post("/api/targets", json=old).status_code == 422
