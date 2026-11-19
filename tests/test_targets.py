"""Target list and export tests, using the shared fixture in conftest.py."""

from __future__ import annotations

import csv
import io

from fastapi.testclient import TestClient


def post(client: TestClient, **body: object) -> dict[str, object]:
    resp = client.post("/api/targets", json=body)
    assert resp.status_code == 200, resp.text
    data: dict[str, object] = resp.json()
    return data


def target_ids(data: dict[str, object]) -> set[str]:
    targets = data["targets"]
    assert isinstance(targets, list)
    return {t["id"] for t in targets}


def test_defaults_to_open_locations_with_one_mile_radius(client: TestClient) -> None:
    data = post(client, chain_key="starbucks")
    assert target_ids(data) == {"sb1", "sb2"}  # sb3 unverified, sb4 duplicate
    assert data["radius_mi"] == 1.0
    assert (data["candidates"], data["excluded_count"]) == (2, 0)


def test_state_filter(client: TestClient) -> None:
    assert target_ids(post(client, chain_key="starbucks", states=["ca"])) == {"sb2"}


def test_excludes_targets_near_own_locations(client: TestClient) -> None:
    data = post(client, chain_key="starbucks", exclude_chain_key="dunkin", exclude_radius_mi=1)
    assert target_ids(data) == {"sb2"}  # nearest Dunkin' is ~7 mi away
    excluded = data["excluded"]
    assert isinstance(excluded, list) and len(excluded) == 1
    assert excluded[0]["id"] == "sb1"
    assert excluded[0]["nearest_own_location"] == "dk1 Main St"
    assert 0.3 < excluded[0]["distance_mi"] < 0.4
    assert (excluded[0]["lat"], excluded[0]["lon"]) == (30.27, -97.74)  # for drawing on the map


def test_unverified_own_locations_still_exclude(client: TestClient) -> None:
    # sb3 (unverified competitor) sits ~0.6 mi from dk2 (unverified own location).
    data = post(
        client,
        chain_key="starbucks",
        include_unverified=True,
        exclude_chain_key="dunkin",
        exclude_radius_mi=1,
    )
    excluded = data["excluded"]
    assert isinstance(excluded, list)
    assert {e["id"] for e in excluded} == {"sb1", "sb3"}


def test_exclusion_radius_respected(client: TestClient) -> None:
    # At 0.5 mi, sb3 (0.6 mi from dk2) is no longer excluded.
    data = post(
        client,
        chain_key="starbucks",
        include_unverified=True,
        exclude_chain_key="dunkin",
        exclude_radius_mi=0.5,
    )
    assert target_ids(data) == {"sb2", "sb3"}


def test_large_exclusion_radius_reaches_far_locations(client: TestClient) -> None:
    data = post(client, chain_key="starbucks", exclude_chain_key="dunkin", exclude_radius_mi=10)
    assert target_ids(data) == set()


def test_radius_limits_follow_meta_us_range(client: TestClient) -> None:
    for radius in (0.5, 51):
        resp = client.post("/api/targets", json={"chain_key": "starbucks", "radius_mi": radius})
        assert resp.status_code == 422


def test_unknown_chain_404(client: TestClient) -> None:
    assert client.post("/api/targets", json={"chain_key": "nope"}).status_code == 404


def test_unknown_exclude_chain_404(client: TestClient) -> None:
    resp = client.post("/api/targets", json={"chain_key": "starbucks", "exclude_chain_key": "nope"})
    assert resp.status_code == 404


def test_invalid_state_rejected(client: TestClient) -> None:
    resp = client.post("/api/targets", json={"chain_key": "starbucks", "states": ["Texas"]})
    assert resp.status_code == 422


def export(client: TestClient, fmt: str, **body: object) -> tuple[list[list[str]], str]:
    resp = client.post(f"/api/targets/export?format={fmt}", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    return list(csv.reader(io.StringIO(resp.text))), resp.headers["content-disposition"]


def test_google_ads_editor_export_uses_proximity_format(client: TestClient) -> None:
    rows, disposition = export(
        client, "google_ads_editor", chain_key="starbucks", radius_mi=2.5, campaign_name="Q1 Austin"
    )
    assert rows[0] == ["Campaign", "Location"]
    assert ["Q1 Austin", "(2.5mi:30.270000:-97.740000)"] in rows
    assert len(rows) == 3
    assert disposition == 'attachment; filename="turf-starbucks-google_ads_editor.csv"'


def test_google_ads_editor_whole_mile_radius_has_no_decimal(client: TestClient) -> None:
    rows, _ = export(client, "google_ads_editor", chain_key="starbucks", states=["TX"])
    assert rows[1] == ["Turf - starbucks", "(1mi:30.270000:-97.740000)"]


def test_generic_export_columns_and_unverified_status(client: TestClient) -> None:
    rows, _ = export(
        client, "generic", chain_key="starbucks", include_unverified=True, states=["TX"]
    )
    header, *body = rows
    assert header[:7] == ["name", "address", "city", "state", "postcode", "latitude", "longitude"]
    by_id = {r[header.index("overture_id")]: r for r in body}
    assert set(by_id) == {"sb1", "sb3"}
    assert by_id["sb3"][header.index("status")] == "unverified"
    assert by_id["sb1"][header.index("radius")] == "1"
    assert by_id["sb1"][header.index("radius_unit")] == "mi"


def test_export_excludes_same_as_json(client: TestClient) -> None:
    rows, _ = export(client, "generic", chain_key="starbucks", exclude_chain_key="dunkin")
    assert [r[-1] for r in rows[1:]] == ["sb2"]


def test_export_rejects_unknown_format(client: TestClient) -> None:
    resp = client.post("/api/targets/export?format=pdf", json={"chain_key": "starbucks"})
    assert resp.status_code == 422
