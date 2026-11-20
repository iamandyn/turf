"""Turf as MCP tools, so AI agents can find markets the way the web page does.

Mounted inside the API at /mcp (streamable HTTP, stateless, JSON responses),
so it shares the read-only database, rate limiting, and deploys. Marketing
teams increasingly query their ad accounts through AI agents in plain English;
this gives those agents market selection: an agent can ask "where else looks like our
best markets for a pet-care app?" and get ranked metros with reasons, ZIP
codes, and briefs.

Every tool is read-only and works on public data, so there's no auth. The
tools return compact results (top three reasons, not every number) to keep an
agent's context small; the full detail is in the HTTP API.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import duckdb
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from turf_app import briefs
from turf_app.markets import PRESETS, MarketRequest, MarketResult, rank_markets, zip_rows
from turf_app.normalize import normalize_text
from turf_app.performance import PerformanceRequest, ResultRow, analyze

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

# Host headers the endpoint accepts (DNS-rebinding protection). The SDK's
# default only allows localhost, which would reject the public domain.
DEFAULT_ALLOWED_HOSTS = "127.0.0.1:*,localhost:*,[::1]:*,testserver"

INSTRUCTIONS = """\
Turf ranks US metro areas for a consumer brand's ad spend, from Overture Maps
places and Census population. Typical flow: list_presets (or search_signals for
custom ones), search_metros to turn the brand's best markets into metro codes,
then rank_markets with those codes as seeds to get lookalike metros, each with
the signals that make it similar. metro_zip_codes gives ZIPs for Meta or Google
Ads targeting; get_brief returns a pre-written creative brief when there is one.
With the brand's actual results by geography (an ad-platform export), use
recommend_from_results instead: it picks the best markets itself, checks whether
the signals predict this brand's results, and recommends where it isn't spending.
Signals are shares of local businesses (a proxy for the audience), not measured
demand; say so when presenting results."""


class PresetInfo(BaseModel):
    key: str
    label: str
    example: str
    signals: list[str]


class MetroInfo(BaseModel):
    metro_code: str
    name: str
    kind: str
    population: int
    growth_2020_25_pct: float


class SignalInfo(BaseModel):
    key: str
    label: str
    open_places_us: int


class RankedMetro(BaseModel):
    rank: int
    metro_code: str
    name: str
    population: int
    growth_2020_25_pct: float
    match: int | None
    why: list[str]  # top signals, e.g. "dog walker: 3.2x the typical share"
    zip_count: int


class ResultsAdvice(BaseModel):
    matched_rows: int
    rows: int
    unmatched_examples: list[str]
    best_markets: list[str]
    signal_check: str  # verdict in a sentence, with the numbers
    signals_that_matter: list[str]
    recommendations: list[RankedMetro]


class Ranking(BaseModel):
    mode: str
    compared: int
    seeds: list[str]
    metros: list[RankedMetro]


def _ranked(r: MarketResult) -> RankedMetro:
    return RankedMetro(
        rank=r.rank,
        metro_code=r.metro_code,
        name=r.name,
        population=r.population,
        growth_2020_25_pct=r.growth_pct,
        match=r.match,
        why=[f"{x.label}: {x.ratio:g}x the typical metro" for x in r.reasons[:3]],
        zip_count=r.zip_count,
    )


def build_mcp(
    cursor: Callable[[], duckdb.DuckDBPyConnection],
    loaded_briefs: Callable[[], dict[tuple[str, str], briefs.Brief]],
) -> MCPServer:
    """``cursor`` returns a new cursor on the app's database for each call."""
    server: MCPServer = MCPServer(
        name="turf", title="Turf market finder", instructions=INSTRUCTIONS
    )

    @server.tool(annotations=READ_ONLY)
    def list_presets() -> list[PresetInfo]:
        """Audience presets (client types) and the place categories each uses as signals."""
        return [
            PresetInfo(key=p.key, label=p.label, example=p.example, signals=list(p.categories))
            for p in PRESETS.values()
        ]

    @server.tool(annotations=READ_ONLY)
    def search_metros(query: str, limit: int = 5) -> list[MetroInfo]:
        """Find US metro areas by name, e.g. "denver" or "dallas fort worth"."""
        q = normalize_text(query)
        if len(q) < 2:
            return []
        db = cursor()
        try:
            rows = db.execute(
                """
                SELECT metro_code, name, kind, population, growth_pct FROM metros
                WHERE contains(search_key, $q)
                ORDER BY starts_with(search_key, $q) DESC, kind, population DESC
                LIMIT $limit
                """,
                {"q": q, "limit": max(1, min(limit, 25))},
            ).fetchall()
        finally:
            db.close()
        return [
            MetroInfo(
                metro_code=r[0], name=r[1], kind=r[2], population=r[3], growth_2020_25_pct=r[4]
            )
            for r in rows
        ]

    @server.tool(annotations=READ_ONLY)
    def search_signals(query: str, limit: int = 8) -> list[SignalInfo]:
        """Find Overture place categories to use as custom signals, e.g. "yoga"."""
        q = normalize_text(query)
        if len(q) < 2:
            return []
        db = cursor()
        try:
            rows = db.execute(
                """
                SELECT category_key, label, n_open FROM categories
                WHERE contains(search_key, $q)
                ORDER BY starts_with(search_key, $q) DESC, n_open DESC
                LIMIT $limit
                """,
                {"q": q, "limit": max(1, min(limit, 25))},
            ).fetchall()
        finally:
            db.close()
        return [SignalInfo(key=r[0], label=r[1], open_places_us=r[2]) for r in rows]

    # Named for agents; the Python name avoids shadowing the imported function.
    @server.tool(name="rank_markets", annotations=READ_ONLY)
    def rank_markets_tool(
        preset: str | None = None,
        signals: list[str] | None = None,
        seed_metro_codes: list[str] | None = None,
        min_population: int = 250_000,
        include_micro: bool = False,
        use_population_growth: bool = False,
        limit: int = 15,
    ) -> Ranking:
        """Rank US metro areas for a brand. Give a preset key or signal categories.
        With seed_metro_codes (the brand's best markets), returns the closest
        lookalikes; without, the metros where the signals are strongest."""
        req = MarketRequest(
            preset=preset,
            categories=signals,
            seeds=seed_metro_codes or [],
            min_population=min_population,
            include_micro=include_micro,
            use_growth=use_population_growth,
            limit=limit,
        )
        db = cursor()
        try:
            result = rank_markets(db, req)
        finally:
            db.close()
        return Ranking(
            mode=result.mode,
            compared=result.universe,
            seeds=[s.name for s in result.seeds],
            metros=[_ranked(r) for r in result.results],
        )

    @server.tool(annotations=READ_ONLY)
    def recommend_from_results(
        rows: list[ResultRow],
        kpi: str = "roas",
        geo_type: str = "metro",
        preset: str | None = None,
        signals: list[str] | None = None,
        limit: int = 10,
    ) -> ResultsAdvice:
        """Recommend new metros from a brand's results by geography. Each row:
        geo (ZIP with geo_type "zip", or a market like "Austin, TX"), spend, and
        conversions (kpi "cpa") or revenue (kpi "roas"). Nothing is stored."""
        req = PerformanceRequest.model_validate(
            {"rows": rows, "kpi": kpi, "geo_type": geo_type, "preset": preset,
             "categories": signals, "limit": limit}
        )  # fmt: skip
        db = cursor()
        try:
            result = analyze(db, req)
        finally:
            db.close()
        names = {m.metro_code: m.name for m in result.metros}
        return ResultsAdvice(
            matched_rows=result.matched_rows,
            rows=result.rows,
            unmatched_examples=result.unmatched_examples,
            best_markets=[names[m.metro_code] for m in result.metros if m.best],
            signal_check=result.check.summary,
            signals_that_matter=[
                f"{f.label} (rank correlation {f.rho:+.2f})"
                for f in result.check.signals
                if f.weight
            ],
            recommendations=[_ranked(r) for r in result.recommendations.results],
        )

    @server.tool(annotations=READ_ONLY)
    def metro_zip_codes(metro_code: str) -> list[str]:
        """Every ZIP code in a metro area, for Meta or Google Ads location targeting."""
        db = cursor()
        try:
            return [z for _, z, _ in zip_rows(db, [metro_code])]
        finally:
            db.close()

    @server.tool(annotations=READ_ONLY)
    def get_brief(preset: str, metro_code: str) -> briefs.Brief | None:
        """The pre-written creative brief for a preset in a metro, or null if there isn't one."""
        return loaded_briefs().get((preset, metro_code))

    return server


def start(server: MCPServer) -> None:
    """(Re)create the HTTP session manager; call at each app startup, then run
    ``server.session_manager`` for the app's lifetime. A session manager can
    only run once, and an app can start more than once (tests do)."""
    hosts = os.environ.get("TURF_MCP_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS).split(",")
    server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[h.strip() for h in hosts if h.strip()],
            allowed_origins=[],
        ),
    )


class _Endpoint:
    """ASGI app for /mcp that always uses the server's current session manager."""

    def __init__(self, server: MCPServer) -> None:
        self.server = server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.server.session_manager.handle_request(scope, receive, send)


def mcp_route(server: MCPServer) -> Route:
    """The /mcp route, for the API's router (no redirect to /mcp/)."""
    return Route("/mcp", endpoint=_Endpoint(server))
