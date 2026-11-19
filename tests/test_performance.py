"""Recommendations from a brand's results (performance.py), on the 12-metro fixture.

Metro i (code 10000 + i) is "Metro i, ST" (10003 is "Austin-Round Rock-San
Marcos, TX"), with i pet groomers and 12 - i dog parks among 100 restaurants,
and ZIPs 7000i0-7000i2 (70000 + 10i + k).
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastapi.testclient import TestClient

from turf_app.performance import ResultRow, match_rows, spearman


def name(i: int) -> str:
    return "Austin, TX" if i == 3 else f"Metro {i}, ST"


def perf(client: TestClient, **body: Any) -> dict[str, Any]:
    body.setdefault("categories", ["pet_groomer", "dog_park"])
    resp = client.post("/api/performance", json=body)
    assert resp.status_code == 200, resp.text
    data: dict[str, Any] = resp.json()
    return data


def rows_where_roas_rises_with_groomers(metros: range = range(11)) -> list[dict[str, Any]]:
    # ROAS = 1 + i/10: better where pet groomers are a bigger share.
    return [{"geo": name(i), "spend": 1000, "revenue": 1000 * (1 + i / 10)} for i in metros]


# --- Matching ---


@pytest.fixture()
def db(serving_db: Path, tmp_path: Path) -> duckdb.DuckDBPyConnection:
    copy = tmp_path / "turf.duckdb"  # the session client holds the original open
    copy.write_bytes(serving_db.read_bytes())
    return duckdb.connect(str(copy), read_only=True)


def test_match_names(db: duckdb.DuckDBPyConnection) -> None:
    geos = ["Metro 5, ST", "metro 5 st", "Austin, TX", "Round Rock TX", "Austin-Round Rock, TX",
            "10007", "Austin", "Atlantis, ZZ", "Metro 5, TX"]  # fmt: skip
    got = match_rows(db, [ResultRow(geo=g, spend=1) for g in geos], "metro")
    assert got == ["10005", "10005", "10003", "10003", "10003", "10007", "10003", None, None]


def test_match_zips(db: duckdb.DuckDBPyConnection) -> None:
    got = match_rows(
        db, [ResultRow(geo=g, spend=1) for g in ["70051", "70051-1234", "99999", "abc"]], "zip"
    )
    assert got == ["10005", "10005", None, None]


def test_spearman() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 2, 2, 3], [1, 2, 3, 4]) == pytest.approx(0.9487, abs=1e-3)  # ties


# --- Analysis ---


def test_totals_best_markets_and_strong_check(client: TestClient) -> None:
    rows = rows_where_roas_rises_with_groomers()
    rows.append({"geo": "Metro 10, ST", "spend": 1000, "revenue": 2000})  # same metro twice
    rows.append({"geo": "Atlantis, ZZ", "spend": 5, "revenue": 5})
    data = perf(client, rows=rows, kpi="roas", best_count=3)
    assert (data["rows"], data["matched_rows"]) == (13, 12)
    assert data["unmatched_examples"] == ["Atlantis, ZZ"]
    m10 = data["metros"][0]
    assert m10["metro_code"] == "10010" and m10["spend"] == 2000 and m10["kpi"] == 2.0
    assert [m["metro_code"] for m in data["metros"] if m["best"]] == ["10010", "10009", "10008"]
    check = data["check"]
    assert check["verdict"] == "strong" and check["p_value"] < 0.05
    assert check["signals"][0]["signal"] == "pet_groomer" and check["signals"][0]["rho"] > 0.9
    dog_park = next(s for s in check["signals"] if s["signal"] == "dog_park")
    assert dog_park["rho"] < 0 and dog_park["weight"] == 0  # negatively related: ignored


def test_cpa_lower_is_better(client: TestClient) -> None:
    rows = [{"geo": name(i), "spend": 1000, "conversions": 10 + i} for i in range(11)]
    data = perf(client, rows=rows, kpi="cpa", best_count=2)
    best = [m["metro_code"] for m in data["metros"] if m["best"]]
    assert best == ["10010", "10009"]  # most conversions per dollar
    assert data["metros"][0]["kpi"] == pytest.approx(1000 / 20)


def test_small_spend_metros_are_not_trusted(client: TestClient) -> None:
    rows = rows_where_roas_rises_with_groomers(range(10))
    rows.append({"geo": name(10), "spend": 1, "revenue": 1000})  # ROAS 1000, but $1
    data = perf(client, rows=rows, kpi="roas", min_spend_share=0.01)
    m10 = next(m for m in data["metros"] if m["metro_code"] == "10010")
    assert not m10["trusted"] and not m10["best"]
    assert data["metros"][-1]["metro_code"] == "10010"  # untrusted listed last


def test_no_relationship_is_weak(client: TestClient) -> None:
    roas = [1.5, 1.2, 1.9, 1.1, 1.6, 1.3, 1.8, 1.4, 1.7, 1.25, 1.55]  # unrelated to i
    rows = [{"geo": name(i), "spend": 1000, "revenue": 1000 * r} for i, r in enumerate(roas)]
    check = perf(client, rows=rows, kpi="roas")["check"]
    assert check["verdict"] == "weak" and "No evidence" in check["summary"]


def test_too_few_metros(client: TestClient) -> None:
    check = perf(client, rows=rows_where_roas_rises_with_groomers(range(4)), kpi="roas")["check"]
    assert check["verdict"] == "too_few" and check["predictive_rho"] is None


def test_recommendations_skip_active_metros(client: TestClient) -> None:
    rows = rows_where_roas_rises_with_groomers(range(4, 11))
    rows.append({"geo": name(2), "spend": 1, "revenue": 1})  # tiny: "low spend", recommendable
    data = perf(client, rows=rows, kpi="roas", best_count=2)
    recs = [r["metro_code"] for r in data["recommendations"]["results"]]
    assert not set(recs) & {f"1000{i}" if i < 10 else "10010" for i in range(4, 11)}
    status = {d["metro_code"]: d["status"] for d in data["details"]}
    assert status["10002"] == "low_spend" and status["10000"] == "not_running"


def test_performance_export(client: TestClient) -> None:
    body = {"rows": rows_where_roas_rises_with_groomers(range(6, 11)), "kpi": "roas",
            "categories": ["pet_groomer"], "best_count": 2}  # fmt: skip
    resp = client.post("/api/performance/export?format=metros", json=body)
    assert resp.status_code == 200
    header, *rows = list(csv.reader(io.StringIO(resp.text)))
    assert header[-1] == "brand_status" and {r[-1] for r in rows} == {"not running"}
    assert resp.headers["content-disposition"].endswith('turf-custom-recommended-metros.csv"')


@pytest.mark.parametrize(
    "body",
    [
        {"rows": [{"geo": "Metro 1, ST", "spend": 10, "conversions": 1}], "kpi": "roas"},
        {"rows": [], "kpi": "roas"},
        {"rows": [{"geo": "Metro 1, ST", "spend": 10, "revenue": 5}], "kpi": "roas", "x": 1},
        {"rows": [{"geo": "Metro 1, ST", "spend": -1, "revenue": 5}], "kpi": "roas"},
    ],
)
def test_validation(client: TestClient, body: dict[str, Any]) -> None:
    body["categories"] = ["pet_groomer"]
    assert client.post("/api/performance", json=body).status_code == 422


def test_nothing_matched(client: TestClient) -> None:
    body = {"rows": [{"geo": "Atlantis, ZZ", "spend": 10, "revenue": 5}], "kpi": "roas",
            "categories": ["pet_groomer"]}  # fmt: skip
    resp = client.post("/api/performance", json=body)
    assert resp.status_code == 422 and "No rows matched" in resp.json()["detail"]


# --- Sample brands ---


def test_samples_are_fictional_and_synthetic(client: TestClient) -> None:
    listed = client.get("/api/samples").json()
    assert len(listed) == 4 and all(
        s["synthetic"] and s["name"].startswith("Sample") for s in listed
    )
    assert client.get("/api/samples/nope").status_code == 404


@pytest.mark.parametrize("key", ["pets", "fitness"])  # metro names and ZIP rows
def test_sample_runs_end_to_end(client: TestClient, key: str) -> None:
    sample = client.get(f"/api/samples/{key}").json()
    assert sample["rows"] and sample == client.get(f"/api/samples/{key}").json()  # stable
    data = perf(
        client, rows=sample["rows"], geo_type=sample["geo_type"], kpi=sample["kpi"],
        preset=sample["preset"], categories=None, min_population=0, include_micro=True,
    )  # fmt: skip
    assert data["matched_rows"] == data["rows"]


def test_recommendation_details(client: TestClient) -> None:
    rows = rows_where_roas_rises_with_groomers(range(4, 11))
    data = perf(client, rows=rows, kpi="roas", best_count=2)
    first = data["details"][0]
    assert first["metro_code"] == data["recommendations"]["results"][0]["metro_code"]
    kpis = [m["kpi"] for m in first["similar"]]
    assert len(kpis) == 3
    assert first["expected_range"] == [min(kpis), max(kpis)]
    assert first["expected_kpi"] == sorted(kpis)[1]
    assert "people" in first["snapshot"] and "since 2020" in first["snapshot"]


def test_best_ratio_in_signal_check(client: TestClient) -> None:
    data = perf(client, rows=rows_where_roas_rises_with_groomers(), kpi="roas", best_count=3)
    check = data["check"]
    groomer = next(s for s in check["signals"] if s["signal"] == "pet_groomer")
    dog_park = next(s for s in check["signals"] if s["signal"] == "dog_park")
    assert groomer["best_ratio"] > 1.4 and dog_park["best_ratio"] < 0.6  # best markets: 8-10
