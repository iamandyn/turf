"""The MCP endpoint (/mcp), called over HTTP the way an agent would."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def rpc(client: TestClient, method: str, params: dict[str, Any], **headers: str) -> Any:
    resp = client.post(
        "/mcp",
        headers={**HEADERS, **headers},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def call(client: TestClient, tool: str, **arguments: Any) -> Any:
    result = rpc(client, "tools/call", {"name": tool, "arguments": arguments})["result"]
    assert not result.get("isError"), result
    return result.get("structuredContent", json.loads(result["content"][0]["text"]))


def test_initialize_and_list_tools(client: TestClient) -> None:
    init = rpc(
        client,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    )
    assert "Turf ranks US metro areas" in init["result"]["instructions"]
    tools = {t["name"]: t for t in rpc(client, "tools/list", {})["result"]["tools"]}
    assert set(tools) == {
        "list_presets", "search_metros", "search_signals", "rank_markets",
        "recommend_from_results", "metro_zip_codes", "get_brief",
    }  # fmt: skip
    assert all(t["annotations"]["readOnlyHint"] for t in tools.values())


def test_rank_markets_lookalikes(client: TestClient) -> None:
    out = call(
        client,
        "rank_markets",
        signals=["pet_groomer", "dog_park"],
        seed_metro_codes=["10005"],
        limit=2,
    )
    assert out["mode"] == "lookalike" and out["seeds"] == ["Metro 5, ST"]
    assert {m["metro_code"] for m in out["metros"]} == {"10004", "10006"}
    assert "the typical metro" in out["metros"][0]["why"][0]


def test_search_and_zips(client: TestClient) -> None:
    metros = call(client, "search_metros", query="round rock")
    metros = metros.get("result", metros)
    assert metros[0]["metro_code"] == "10003"
    signals = call(client, "search_signals", query="groom")
    assert signals.get("result", signals)[0]["key"] == "pet_groomer"
    zips = call(client, "metro_zip_codes", metro_code="10000")
    assert zips.get("result", zips) == ["70000", "70001", "70002"]


def test_bad_input_is_a_tool_error(client: TestClient) -> None:
    result = rpc(client, "tools/call", {"name": "rank_markets", "arguments": {"preset": "nope"}})
    assert result["result"]["isError"]


def test_unknown_host_rejected(client: TestClient) -> None:
    # DNS-rebinding protection: only configured Host headers are served.
    resp = client.post("/mcp", headers={**HEADERS, "host": "evil.example"}, json={})
    assert resp.status_code == 421


def test_recommend_from_results(client: TestClient) -> None:
    rows = [{"geo": f"Metro {i}, ST", "spend": 1000, "revenue": 1000 + 100 * i} for i in range(11)]
    rows[3]["geo"] = "Austin, TX"
    out = call(
        client, "recommend_from_results", rows=rows, kpi="roas",
        signals=["pet_groomer", "dog_park"], limit=3,
    )  # fmt: skip
    assert out["matched_rows"] == 11 and out["best_markets"][0] == "Metro 10, ST"
    assert "better than chance" in out["signal_check"]
    assert out["signals_that_matter"][0].startswith("pet groomer")
