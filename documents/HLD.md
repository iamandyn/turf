# Turf: High-Level Design

> Keep this current. Any change to components, data flow, or deployment updates this file in the same commit. Details live in [LLD.md](LLD.md); a file-by-file map lives in [CODE_GUIDE.md](CODE_GUIDE.md).

## 1. Purpose

Turf helps a consumer brand decide **which US metro areas to spend on next**. The brand gives two inputs:
- **Signals:** a few kinds of local business whose density hints at its audience (pet groomers, dog parks and vets for a pet-care app), or one of six presets.
- **Its results (optional):** an ad report by ZIP or market, from which Turf picks the best markets and checks whether the signals predict this brand's results; or a few best markets typed in.

Turf ranks every US metro area by how closely its profile matches the seeds, or by signal strength when there are no seeds. Each result explains itself ("dog walkers: 3.2× the typical metro's share of businesses"). Results export as ZIP lists for Meta and Google Ads, and each preset's top metros come with a pre-written creative brief. The same search is available to AI agents as MCP tools.

**Users:** media buyers at an agency like Valid, an AI-native agency whose clients are national consumer brands (subscription apps, DTC, health & fitness, home services) buying on Meta, TikTok and Google. Those brands buy by geography but have no stores, so the question isn't "where are our competitors" but "where else looks like the markets that work for us".

**History:** the first version was a geo-conquesting tool, building target lists around competitors' store locations. It was rebuilt after checking who Valid's clients actually are: almost none have physical locations. The data pipeline and the engineering carried over; the product didn't.

**Why Overture + Census:** Overture says where every business is and what kind it is, for the whole US, for free. That's enough to describe what each metro is like. The Census adds what Overture lacks: metro boundaries, population and growth. Neither measures demand, so Turf presents signals as a proxy, and the way to validate them is the brand's own results by metro.

## 2. System overview

```
           OFFLINE (dev machine, once per Overture release)            ONLINE (server)
┌──────────────────────────────────────────────────────────┐   ┌───────────────────────────────┐
│ Overture GeoParquet on S3 (pinned release)               │   │ Cloudflare (proxy, TLS edge)  │
│        │ DuckDB, bbox pushdown                           │   │          │                    │
│        ▼                                                 │   │          ▼                    │
│ extract_us.py ──► us_places.parquet  (18.3M US places)   │   │ Caddy (TLS, client IP)        │
│ extract_states.py ► us_states.parquet (state polygons)   │   │          │ 127.0.0.1:8090     │
│ extract_metros.py ► us_metros.parquet (Census metros)    │   │          │                    │
│        │                                                 │   │          ▼                    │
│        ▼                                                 │   │ Docker: turf container        │
│ build_chains.py ─► places.parquet (duplicate flags)      │   │   FastAPI (uvicorn, 2 workers)│
│   chain matching, duplicate flags, state fill            │   │   /api/* · /mcp · page        │
│        │                                                 │   │   rate limiter (per IP)       │
│        ▼                                                 │   │   DuckDB read-only ───────┐   │
│ build_serving_db.py ─► turf.duckdb (5.5 MB) ─────────────┼───┼──► /opt/turf/data/current ┘   │
│   per-metro aggregates   (scripts/deploy_data.sh)        │   │                               │
└──────────────────────────────────────────────────────────┘   └───────────────────────────────┘

   CODE PATH:  git push main ─► GitHub Actions: check ─► build image ─► push GHCR ─► deploy ─► smoke + browser test
```

## 3. Components

| Component | Responsibility | Tech |
|---|---|---|
| **Data pipeline** (`scripts/`, `src/turf_app/{normalize,chains,states,serving}.py`) | Extract US places from a pinned Overture release and metro boundaries and population from the Census; normalize, flag duplicates, fill missing states; aggregate open places per metro and category; write the serving database | Python 3.12, DuckDB (+ spatial, httpfs) |
| **Market engine** (`src/turf_app/markets.py`) | Presets, signal shares, z-scores, lookalike distance (optionally weighted), signal strength, reasons | Pure Python over DuckDB queries |
| **Brand results** (`src/turf_app/performance.py`, `samples.py`) | Match uploaded rows to metros, KPI per metro, best markets, the permutation-tested signal check, weighted recommendations; fictional sample brands with synthetic results | Pure Python |
| **API** (`src/turf_app/api.py`, `ratelimit.py`, `briefs.py`) | Presets, category and metro search, market ranking, metro/ZIP CSV export, briefs; per-IP rate limiting | FastAPI, Pydantic, DuckDB read-only |
| **MCP server** (`src/turf_app/mcp_server.py`) | The same search as six read-only tools for AI agents, at `/mcp` in the same app | MCP Python SDK 2.x (streamable HTTP, stateless) |
| **Frontend** (`src/turf_app/web/`) | One page: audience presets and editable signals, seed metros, filters, ranked list, map, detail panel with signal bars and brief, exports, shareable URLs, light/dark theme | Plain HTML/CSS/ES modules (no build step), MapLibre GL 4.7.1 (vendored), OpenFreeMap basemap |
| **Container** (`Dockerfile`, `deploy/docker-compose.yml`) | Immutable image per commit; data mounted read-only | Docker, Compose |
| **Edge** (`deploy/Caddyfile.snippet`) | TLS, reverse proxy, real client IP from Cloudflare | Cloudflare proxy, Caddy |
| **CI/CD** (`.github/workflows/ci.yml`, `scripts/server_deploy.sh`) | Lint, type-check, test; build and push image; deploy with health check and automatic rollback; public smoke test and browser test | GitHub Actions, GHCR, SSH forced command |

## 4. Key data flows

**Page load:** FastAPI serves `index.html` with content-hashed asset URLs, the scripts, styles and vendored MapLibre, with a strict CSP; map tiles come straight from OpenFreeMap.

**Ranking:** browser or agent → Cloudflare → Caddy (sets `X-Client-IP`) → container → rate limiter → `POST /api/markets` (or the `rank_markets` MCP tool) → `rank_markets()` reads the metros and the signal categories' counts from DuckDB, standardizes, ranks, and returns reasons → JSON.

**Code release:** a push to `main` runs the checks. If they pass, CI builds `ghcr.io/iamandyn/turf:<sha>`, then SSHes to the server as `turfdeploy`, whose key can only run `deploy <sha>`. The server pulls the image, switches the container, and waits for `/api/meta` to report that SHA, rolling back if it doesn't. CI then smoke-tests the public URL and drives it in a browser.

**Data release:** run the pipeline locally for a new Overture release, then `scripts/deploy_data.sh <release>`, which uploads to `/opt/turf/data/<release>/`, points `current` at it, and recreates the container.

## 5. Key decisions and trade-offs

| Decision | Why | Cost |
|---|---|---|
| **Market selection, not geo-conquesting** (the pivot) | Valid's clients are national consumer brands without stores; they buy by geography, so "which metros next" is their question | the conquesting features were removed (they remain in git history) |
| **Signals are shares of local businesses, not per-capita rates** | Overture's coverage varies 3× between metros (2,100 to 6,200 open places per 100k people), so per-capita rates would partly measure mapping quality | Shares are diluted in metros with many of some other kind of business |
| **Check the signals against the brand's results, with a permutation test** | The signals are a proxy; a brand's results are the only ground truth. Leave-one-out avoids overfitting the weights, but its score is biased upward, so 999 shufflings set the chance level | 0.15 s per analysis; needs at least 6 metros with enough spend |
| **Uploaded results are never stored** | Client-confidential data; no accounts to build or data to secure | Nothing is remembered between visits |
| **Sample brands are fictional, with synthetic data** | Real brands' results by metro aren't public; invented numbers under a real name would misrepresent it | The samples demonstrate the flow, not the method |
| **Lookalikes to the brand's own best markets** | The brand's results are the only ground truth available; matching them is more defensible than a universal "opportunity score" | Needs seeds for the best results; without them it's only signal strength |
| **Every result explains itself** | A media buyer has to defend a plan to a client; "2.1× the typical share of pet groomers" is checkable | More data per result |
| **Census for metros and population** | Overture has neither; CBSAs are the standard public metro definition | A second data source, pinned by SHA-256 |
| **Export ZIPs, not metro names** | Neither Meta nor Google targets Census metros; both take US ZIP lists | ZIPs that straddle metros go to the metro holding most of their places |
| **Pre-written briefs, no live LLM calls** | It's a take-home demo: the live site must cost nothing to run however much it's used | Briefs exist only for each preset's top metros (65); see README |
| **MCP server in the same app** | Valid already gives clients an MCP server for their account; agents are how an AI-native agency would use this | A runtime dependency (MCP SDK) |
| **Precompute offline, serve aggregates** | The served file is 5.5 MB; queries take milliseconds; no database server | A data refresh means rerunning the pipeline (~5 minutes) |
| **Pinned Overture release** | Reproducible results; the release is shown in `/api/meta` | Someone has to run the pipeline monthly |
| **Docker + GHCR + SSH forced command** | Immutable, rollback-able releases; the CI key can only deploy a SHA | Server setup is a separate, manual step |
| **No-build frontend served by the API** | One image, one deploy; no Node toolchain for a backend-focused project | No TypeScript or bundling |
| **In-memory, per-worker rate limiting** | No extra infrastructure | Limits are per worker (up to 2× with 2 workers) |

## 6. Non-goals (for now)

- Countries other than the US.
- Measured demand (demographics, income, foot traffic, search volume).
- Pushing targets into ad accounts through the platforms' APIs; Turf produces files and tool results.
- User accounts or saved plans.

## 7. Known limitations

- **Signals are a proxy.** Business mix suggests where an audience lives but doesn't measure it. Validate against the brand's results by metro before moving budget.
- **Coverage and category quirks.** Overture's categories are crowdsourced; some metros have oddities (a category at 100× typical), and small categories are noisy in small metros. The population floor (250k by default) keeps comparisons sensible.
- **Unverified upload formats.** The ZIP list is a plain CSV for the platforms' bulk location boxes; neither platform publishes a file format for this that I could verify.
- **US territories:** Puerto Rico and other territories aren't covered.
