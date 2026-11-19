# Turf: High-Level Design

> Keep this current. Any change to components, data flow, or deployment updates this file in the same commit. Details live in [LLD.md](LLD.md); a file-by-file map lives in [CODE_GUIDE.md](CODE_GUIDE.md).

## 1. Purpose

Turf turns open Overture Maps data into **ad-ready location target lists for geo-conquesting**: showing ads to people near a competitor's locations.

A user picks one or more competitor chains (e.g. Orangetheory Fitness + F45) or a whole category (e.g. pet boarding: every such business, chains and independents), optionally a state or metro area and their own locations (their chain in Overture, an uploaded store list, or both), and gets the competitors' US locations as radius targets: by default only those near the user's own locations, where customers could switch, or alternatively all except those near them. Each target is a lat/lng plus a radius, and the list exports as a Google Ads Editor CSV or a generic CSV for Meta and other tools.

**Users:** media buyers and local marketers running location-based campaigns.

**Why Overture:** it's free, covers the whole US, and has the one thing this needs: where businesses are, what category they are, and which brand they belong to. It has no demographics or foot traffic, so Turf only makes claims that location and brand data can support.

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
│ build_chains.py ─► places.parquet + chains.parquet       │   │   FastAPI (uvicorn, 2 workers)│
│   chain matching, duplicate flags, state fill            │   │   rate limiter (per IP)       │
│        │                                                 │   │   DuckDB read-only ───────┐   │
│        ▼                                                 │   │                           │   │
│ build_serving_db.py ─► turf.duckdb (1.3 GB) ─────────────┼───┼──► /opt/turf/data/current ┘   │
│                         (scripts/deploy_data.sh)         │   │                               │
└──────────────────────────────────────────────────────────┘   └───────────────────────────────┘

   CODE PATH:  git push main ─► GitHub Actions: check ─► build image ─► push GHCR ─► deploy ─► smoke test
```

## 3. Components

| Component | Responsibility | Tech |
|---|---|---|
| **Data pipeline** (`scripts/`, `src/turf_app/{normalize,chains,states,serving}.py`) | Extract US places and state boundaries from a pinned Overture release; normalize names, match places to chains, flag duplicates, fill missing states; write the serving database | Python 3.12, DuckDB (+ spatial, httpfs) |
| **API** (`src/turf_app/api.py`, `targets.py`, `ratelimit.py`) | Search (chains, categories, metro areas), chain locations, target lists filtered by area and by distance to your locations, CSV export; per-IP rate limiting | FastAPI, Pydantic, DuckDB read-only |
| **Frontend** (`src/turf_app/web/`) | One page: competitor search (several chains or a category), one "Where" field for states and metro areas (combinable), radius/unverified controls, your locations (chain and/or CSV upload, only-near or skip-near), map with layer toggles, target and skipped lists, sticky results and export bar, shareable URLs, light/dark theme | Plain HTML/CSS/ES modules (no build step), MapLibre GL 4.7.1 (vendored), OpenFreeMap basemap; served by FastAPI |
| **Container** (`Dockerfile`, `deploy/docker-compose.yml`) | Immutable image per commit; data mounted read-only | Docker, Compose |
| **Edge** (`deploy/Caddyfile.snippet`) | TLS, reverse proxy, real client IP from Cloudflare | Cloudflare proxy, Caddy |
| **CI/CD** (`.github/workflows/ci.yml`, `scripts/server_deploy.sh`) | Lint, type-check, test; build and push image; deploy with health check and automatic rollback; public smoke test | GitHub Actions, GHCR, SSH forced command |

## 4. Key data flows

**Page load:** FastAPI serves `index.html`, `app.js`, `styles.css`, and the vendored MapLibre from the package, with a strict CSP; map tiles come straight from OpenFreeMap.

**Request path:** browser → Cloudflare → Caddy (sets `X-Client-IP` from `CF-Connecting-IP`) → container on `127.0.0.1:8090` → rate limiter → FastAPI route → DuckDB cursor on the read-only `turf.duckdb` → JSON or CSV.

**Code release:** a push to `main` runs the checks. If they pass, CI builds `ghcr.io/iamandyn/turf:<sha>`, then SSHes to the server as `turfdeploy`, whose key can only run `deploy <sha>`. The server pulls the image, switches the container, and waits for `/api/meta` to report that SHA, rolling back to the previous image if it doesn't. CI then smoke-tests the public URL.

**Data release:** run the pipeline locally for a new Overture release, then `scripts/deploy_data.sh <release>`. That uploads to `/opt/turf/data/<release>/`, points `current` at it, and recreates the container. Rolling back data means pointing `current` at an older release.

## 5. Key decisions and trade-offs

| Decision | Why | Cost |
|---|---|---|
| **Geo-conquesting, not a "demand score"** | Overture can't support a demand score (no demographics or traffic); brand and location are what it's good at | A narrower product |
| **Precompute offline, serve a read-only DuckDB file** | Fast, predictable queries; no database server to run; data can be swapped with a single rename | A data refresh means a rebuild (~5 minutes) and a 1.3 GB upload |
| **Pinned Overture release** | Results are reproducible; the release is shown in `/api/meta` | Doesn't update itself; someone has to run the pipeline monthly |
| **Chain matching on brand *or* name, with brand tags trusted per chain** | The brand field is missing on many real chain locations and wrong on some (see LLD §2.3) | Heuristic; tuned on real cases and covered by tests |
| **Confirmed-open locations by default** | Places with no status have no recent Overture activity signal; they explain most over-counting | Some real but unverified locations are hidden; a toggle shows them |
| **Duplicates flagged, not merged** | Rules stay conservative: merging two real downtown stores is worse than one leftover duplicate | ~1–2% duplicates remain for some chains |
| **No merging of overlapping circles** | Both ad platforms accept overlapping targets; merged circles get larger and less precise | Longer export lists |
| **Radius limited to 1–50 mi** | Meta Ads Manager's US range, so exports work on both platforms | Google allows more |
| **Docker + GHCR + SSH forced command** | Immutable, rollback-able releases; the CI key can't do anything except deploy a SHA | Server setup is a separate, manual step (`setup_server.sh`) |
| **No-build frontend served by the API** | One image, one deploy, nothing extra to host; no Node toolchain for a backend-focused project | No TypeScript or bundling; fine at this size (one ~500-line module) |
| **In-memory, per-worker rate limiting** | No extra infrastructure; enough to stop one visitor hogging the server | Limits are per worker (up to 2× with 2 workers); Redis would make them exact |
| **Client IP from a Caddy-set header, not `X-Forwarded-For`** | Cloudflare passes a visitor-supplied `X-Forwarded-For` through, so trusting it lets anyone pick a new IP per request | Depends on the Caddy config being in place before this app version |

## 6. Non-goals (for now)

- Countries other than the US.
- Live ad-platform integration through their APIs; Turf produces files for upload.
- Demographic or foot-traffic scoring.
- User accounts or saved lists.

## 7. Known limitations

- **Unverified Meta format:** Meta doesn't publish a bulk radius-target file format that we could verify, so the "generic" CSV is deliberately plain.
- **US territories:** Puerto Rico and other territories aren't covered (LLD §2.1).
- **Categories:** search covers Overture's 2,060 fine-grained categories, but not synonyms. "dog daycare" has no category of its own (it's under pet boarding), so search falls back to entries matching any word.
- **List size:** one list is capped at 25,000 targets; broad categories need a state.
- **Brand data:** a few odd "chains" appear (e.g. a single city's fire department).
