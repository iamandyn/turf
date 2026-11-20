# Turf: High-Level Design

> Keep this current. Any change to components, data flow, or deployment updates this file in the same commit. Details live in [LLD.md](LLD.md); a file-by-file map lives in [CODE_GUIDE.md](CODE_GUIDE.md).

## 1. Purpose

Turf turns **a brand's ad results by location into a market report**: the metro areas its ads should run in next, what to expect there, and the data behind each recommendation. The user uploads an ad-platform report (or opens a sample brand) and says what kind of brand it is. Turf:
- matches the results to US metro areas and finds the best markets by ROAS or CPA;
- describes every metro by its mix of local businesses (Overture) and population (Census);
- checks whether those patterns predict the brand's results, with a permutation test;
- recommends the metros that look most like the best markets where the brand spends little or nothing, each with an expected result from the brand's most similar markets.

The report exports as ZIP lists for Meta and Google Ads, a CSV, or a PDF. The same analysis is available to AI agents as MCP tools. Instead of uploading, a visitor can connect a **simulated ad account**: Turf syncs it through an API like a production connector would and updates the report as new days arrive.

**Users:** media buyers and strategists, at performance-marketing agencies or on in-house growth teams, running ads for national consumer brands (subscription apps, DTC, health & fitness, home services) on Meta, TikTok and Google. Those brands buy by geography but have no stores, so the question isn't "where are our competitors" but "where else looks like the markets that work for us".

**History:** the first version was a geo-conquesting tool (target lists around competitors' stores), rebuilt after recognizing that the brands buying ads this way mostly have no physical locations. A second version was a configurable market explorer (presets, signals, seed metros, filters); it was simplified into this single flow because it was confusing. The data pipeline and the engineering carried over each time.

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
| **Ad-account connector** (`src/turf_app/connector/`) | Connect a (simulated) ad account and sync it: OAuth token with refresh, cursor paging, retries with backoff and `Retry-After`, validated rows upserted by day and market, backfill then incremental syncs with a lookback for late conversions, the report recomputed and diffed; background jobs with progress as server-sent events | httpx, SQLite (WAL) in the container's tmpfs, asyncio tasks |
| **Simulated ad platform** (`src/turf_app/connector/platform.py`) | A stand-in for a real ad platform at `/mock/ads`: an insights API shaped like Meta's, serving the sample brands day by day, with injected rate limits, outages and late-arriving conversions, and a demo control to move its date forward | FastAPI sub-app, reached by the connector in-process over httpx's ASGI transport |
| **MCP server** (`src/turf_app/mcp_server.py`) | The same search as six read-only tools for AI agents, at `/mcp` in the same app | MCP Python SDK 2.x (streamable HTTP, stateless) |
| **Frontend** (`src/turf_app/web/`) | Two states: a start screen (sample brands, or upload + one category question) and the report (recommendations with expandable data and briefs, confidence, drivers, markets today, method, adjust signals), map, exports, print layout, demo disclaimer dialog, light/dark theme | Plain HTML/CSS/ES modules (no build step), MapLibre GL 4.7.1 (vendored), OpenFreeMap basemap |
| **Container** (`Dockerfile`, `deploy/docker-compose.yml`) | Immutable image per commit; data mounted read-only | Docker, Compose |
| **Edge** (`deploy/Caddyfile.snippet`) | TLS, reverse proxy, real client IP from Cloudflare | Cloudflare proxy, Caddy |
| **CI/CD** (`.github/workflows/ci.yml`, `scripts/server_deploy.sh`) | Lint, type-check, test; build and push image; deploy with health check and automatic rollback; public smoke test and browser test | GitHub Actions, GHCR, SSH forced command |

## 4. Key data flows

**Page load:** FastAPI serves `index.html` with content-hashed asset URLs, the scripts, styles and vendored MapLibre, with a strict CSP; map tiles come straight from OpenFreeMap.

**Report:** browser or agent → Cloudflare → Caddy (sets `X-Client-IP`) → container → rate limiter → `POST /api/performance` (or the `recommend_from_results` MCP tool) → `analyze()` matches rows to metros, builds profiles from DuckDB, runs the signal check, ranks lookalikes and attaches similar markets and snapshots → JSON. The rows exist only for that request.

**Ad-account sync:** `POST /api/connections` creates a simulated account and a connection, and queues a backfill job (an asyncio task in that worker, at most 2 running per worker). The job gets a token, pages through the platform's insights (retrying refusals), upserts each page into SQLite, then runs `analyze()` on the last 28 days and stores what changed. Every step is written as an event; `GET …/events` streams them to the page (server-sent events) from whichever worker serves the request. The page then fetches the window's rows and builds the report the same way as for an upload.

**Code release:** a push to `main` runs the checks. If they pass, CI builds `ghcr.io/iamandyn/turf:<sha>`, then SSHes to the server as `turfdeploy`, whose key can only run `deploy <sha>`. The server pulls the image, switches the container, and waits for `/api/meta` to report that SHA, rolling back if it doesn't. CI then smoke-tests the public URL and drives it in a browser.

**Data release:** run the pipeline locally for a new Overture release, then `scripts/deploy_data.sh <release>`, which uploads to `/opt/turf/data/<release>/`, points `current` at it, and recreates the container.

## 5. Key decisions and trade-offs

| Decision | Why | Cost |
|---|---|---|
| **Market selection, not geo-conquesting** (the pivot) | The target brands are national consumer brands without stores; they buy by geography, so "which metros next" is their question | the conquesting features were removed (they remain in git history) |
| **Signals are shares of local businesses, not per-capita rates** | Overture's coverage varies 3× between metros (2,100 to 6,200 open places per 100k people), so per-capita rates would partly measure mapping quality | Shares are diluted in metros with many of some other kind of business |
| **Check the signals against the brand's results, with a permutation test** | The signals are a proxy; a brand's results are the only ground truth. Leave-one-out avoids overfitting the weights, but its score is biased upward, so 999 shufflings set the chance level | 0.15 s per analysis; needs at least 6 metros with enough spend |
| **Uploaded results are never stored** | Client-confidential data; no accounts to build or data to secure | Nothing is remembered between visits |
| **Sample brands are fictional, with synthetic data** | Real brands' results by metro aren't public; invented numbers under a real name would misrepresent it | The samples demonstrate the flow, not the method |
| **Lookalikes to the brand's own best markets** | The brand's results are the only ground truth available; matching them is more defensible than a universal "opportunity score" | Needs seeds for the best results; without them it's only signal strength |
| **Every result explains itself** | A media buyer has to defend a plan to a client; "2.1× the typical share of pet groomers" is checkable | More data per result |
| **Census for metros and population** | Overture has neither; CBSAs are the standard public metro definition | A second data source, pinned by SHA-256 |
| **Export ZIPs, not metro names** | Neither Meta nor Google targets Census metros; both take US ZIP lists | ZIPs that straddle metros go to the metro holding most of their places |
| **One flow: data in, report out** | The configurable explorer (presets, signals, seeds, filters) was confusing; a strategist wants an answer and the evidence for it | Exploration without data is API/MCP-only (`/api/markets`, `rank_markets`) |
| **Ask the brand's category, don't detect it** | Trying every category and keeping the best fit would inflate the confidence numbers; the user already knows | One question before the report |
| **Expected results from similar markets** | Easy to check and honest about spread: the median and range of the brand's three most similar existing markets | A rough estimate, labelled as such |
| **Pre-written briefs, no live LLM calls; a disclaimer dialog** | It's a public demo: the live site must cost nothing to run, and visitors should know the samples are fictional | Briefs exist only for the sample brands' top recommendations (20) |
| **A connector against a simulated platform** | Shows the integration work a real connector needs (auth, paging, retries, idempotency, incremental sync, background jobs, live progress) with no ad account and no API cost | The platform's behavior is ours, so the connector is proven against a model of Meta's API, not Meta's |
| **Connector state in SQLite under `/tmp`** | Two workers in a read-only container: job state and progress must be shared, and SQLite in WAL mode needs no extra service | State is lost on restart (fine for synthetic demo data); a multi-host deployment would move it to Postgres |
| **Synced data is kept, uploads are not** | Synced data is synthetic and incremental sync needs history; uploads are client data | Connections expire after a day of inactivity, and can be deleted at once ("Disconnect") |
| **MCP server in the same app** | Marketing teams increasingly query their ad accounts through AI agents; an MCP server lets those agents use the same analysis | A runtime dependency (MCP SDK) |
| **Precompute offline, serve aggregates** | The served file is 5.5 MB; queries take milliseconds; no database server | A data refresh means rerunning the pipeline (~5 minutes) |
| **Pinned Overture release** | Reproducible results; the release is shown in `/api/meta` | Someone has to run the pipeline monthly |
| **Docker + GHCR + SSH forced command** | Immutable, rollback-able releases; the CI key can only deploy a SHA | Server setup is a separate, manual step |
| **No-build frontend served by the API** | One image, one deploy; no Node toolchain for a backend-focused project | No TypeScript or bundling |
| **In-memory, per-worker rate limiting** | No extra infrastructure | Limits are per worker (up to 2× with 2 workers) |

## 6. Non-goals (for now)

- Countries other than the US.
- Measured demand (demographics, income, foot traffic, search volume).
- Pushing targets into ad accounts through the platforms' APIs; Turf produces files and tool results.
- User accounts or saved plans (a connection is reachable only through its unguessable id, and expires after a day).

## 7. Known limitations

- **Signals are a proxy.** Business mix suggests where an audience lives but doesn't measure it. Validate against the brand's results by metro before moving budget.
- **Coverage and category quirks.** Overture's categories are crowdsourced; some metros have oddities (a category at 100× typical), and small categories are noisy in small metros. The population floor (250k by default) keeps comparisons sensible.
- **Unverified upload formats.** The ZIP list is a plain CSV for the platforms' bulk location boxes; neither platform publishes a file format for this that I could verify.
- **US territories:** Puerto Rico and other territories aren't covered.
