# Turf: Code Guide

> Where everything lives and what to touch for a given change. Keep this current: adding, moving, or repurposing a file updates this guide in the same commit. Design rationale: [HLD.md](HLD.md). Rules, schemas, and protocols: [LLD.md](LLD.md).

## Repo map

```
turf/
├── src/turf_app/            Library code shared by the pipeline and the API (importable, typed)
│   ├── normalize.py         Text, chain-key, state, and street-key normalization
│   ├── chains.py            Chain matching + duplicate flagging (DuckDB SQL + Python functions)
│   ├── states.py            Fill missing states from state boundaries
│   ├── serving.py           Build the read-only turf.duckdb the API serves
│   ├── targets.py           Target-list logic, request/response models, CSV export
│   ├── ratelimit.py         Per-IP sliding-window rate limiter
│   ├── api.py               FastAPI app: routes, DB cursor dependency, middleware, static files
│   └── web/                 Frontend, served at / (no build step)
│       ├── index.html       Page structure (steps 1–3, sticky action bar, map)
│       ├── app.js           All behavior: search, state/URL, map layers, export
│       ├── styles.css       Layout and theme (CSS variables at the top)
│       └── vendor/maplibre-gl-4.7.1/  Pinned MapLibre build + license
├── scripts/                 Everything you run by hand (thin wrappers around src/)
│   ├── inspect_places.py        Step 0: look at the schema and a sample area
│   ├── extract_us.py            Step 1: US places from S3 → us_places.parquet
│   ├── extract_states.py        Step 1b: state boundaries → us_states.parquet
│   ├── build_chains.py          Step 2: chains, duplicates, states → places/chains.parquet
│   ├── build_serving_db.py      Step 3: → turf.duckdb
│   ├── check_extract.py         Checks: state formats, status mix, chain counts
│   ├── investigate_chain.py     Break down one chain (status, sources, near pairs)
│   ├── deploy_data.sh           Upload a release's turf.duckdb and switch to it
│   ├── setup_server.sh          Create the deploy user and /opt/turf (admin, re-runnable)
│   ├── server_deploy.sh         Runs on the server: the CI key's only command
│   ├── update_caddy_cloudflare_ips.sh  Refresh Caddy's trusted Cloudflare ranges
│   ├── browser_smoke.py         End-to-end browser test (Playwright) against any URL
│   └── watch_ci.sh              Wait for a commit's CI/CD run and print the job results
├── tests/                   pytest; conftest.py builds a small serving DB with the real builder
├── deploy/
│   ├── docker-compose.yml   Production container definition (installed to /opt/turf)
│   └── Caddyfile.snippet    Turf's parts of the server Caddyfile
├── .github/workflows/ci.yml CI/CD: check → image → deploy → smoke test (actions pinned to SHAs)
├── .github/dependabot.yml   Weekly PRs to update the pinned actions
├── Dockerfile               API image (data is mounted, not baked in)
├── documents/               HLD, LLD, this guide
└── data/                    Pipeline output, per release (gitignored)
```

**Convention:** logic lives in `src/turf_app/`; scripts in `scripts/` only parse arguments, call that logic, and print or write results. Shared code (e.g. `normalize_text`, used by both the pipeline and search) must stay in `src/`.

## Features → files

| Feature | Start here | Also touches | Tests |
|---|---|---|---|
| **Overture extract** (which places, which columns) | `scripts/extract_us.py` | `scripts/inspect_places.py` to explore the schema first | none (needs S3); check `extract_manifest.json` |
| **Name / brand normalization** | `src/turf_app/normalize.py` | search behavior in `api.py` (same function) | `tests/test_normalize.py` |
| **Chain aliases** (e.g. merge "Exxon" into "ExxonMobil") | `CHAIN_ALIASES` in `normalize.py` | | `test_normalize.py::test_chain_key` |
| **Which brands count as chains** (trust thresholds, kiosk exclusions) | `ChainRules`, `EXCLUDED_CHAINS` in `chains.py` | | `tests/test_chains.py` |
| **Duplicate detection** | `chains.py` step 5, `street_key` in `normalize.py` | `ChainRules.dup_*` | `test_chains.py`, `test_normalize.py::test_street_key_*` |
| **State cleanup / fill** | `normalize_state` (`normalize.py`), `states.py` | `scripts/extract_states.py` | `test_states.py`, `test_normalize.py` |
| **Serving DB schema** | `serving.py` | every SQL query in `api.py` and `targets.py`; bump `SCHEMA_VERSION` | `tests/conftest.py` builds it |
| **Search** (chains + categories, ranking, typos, any-word fallback) | `search` in `api.py` (`_SEARCH_CANDIDATES`); `search_chains` for chains only | `FUZZY_MIN_SIMILARITY` | `test_api.py::test_search_*`, `test_unified_search_*` |
| **Categories** | `categories` table in `serving.py` | `category` filter in `targets.py`; category label in `web/app.js` | `test_targets.py::test_category_*` |
| **Chain locations** | `chain_locations` in `api.py` | | `test_api.py::test_locations_*` |
| **Target lists, your locations, only-near / skip-near** | `build_targets`, `_own_locations_sql` in `targets.py` | `TargetRequest` limits (`MAX_TARGETS`, `MAX_CHAINS`, `MAX_UPLOADED_LOCATIONS`), `DEFAULT_OWN_RADIUS_MI`; keep the JS copies of these constants in `web/app.js` in step; timing with `scripts/bench_targets.py` | `tests/test_targets.py` |
| **CSV export formats** | `to_csv`, `ExportFormat` in `targets.py` | `export_targets` route in `api.py` | `test_targets.py::test_*export*` |
| **Rate limits** | `DEFAULT_RULES` in `ratelimit.py` | middleware in `api.py`; `TURF_CLIENT_IP_HEADER` in the `Dockerfile` | `tests/test_ratelimit.py`, `test_api.py::test_rate_limit_*` |
| **Frontend: search box, several competitors** | `setupCombo`, `addTarget`, `renderTargets` in `web/app.js` | `/api/search` | `scripts/browser_smoke.py` |
| **Frontend: store list upload** | `parseStoreList`, `parseCsv`, `onUpload` in `web/app.js` | step 3 in `web/index.html` | browser smoke (upload) |
| **Frontend: map layers, colors, layer toggles, zoom hint** | `setupLayers`, `metersToPixels`, `setLayerVisible`, `updateZoomHint` in `web/app.js` | colors in `web/styles.css` (`--accent`, `--own`, `--skip`) | browser smoke (screenshots, axe) |
| **Frontend: controls, URL sharing** | `state`, `syncUrl`, `restoreFromUrl`, `init` in `web/app.js` | `web/index.html` | browser smoke (URL restore) |
| **Security headers / CSP** | `CONTENT_SECURITY_POLICY` and middleware in `api.py` | add new third-party origins here *and* test in a browser | `test_api.py::test_frontend_*` |
| **Real client IP** | `deploy/Caddyfile.snippet` (server) | `scripts/update_caddy_cloudflare_ips.sh`, `ratelimit.py` docstring | `test_api.py::test_x_forwarded_for_is_ignored` |
| **Container** | `Dockerfile` | `deploy/docker-compose.yml`, `.dockerignore`; dependency groups in `pyproject.toml` (`pipeline` and `dev` stay out of the image) | CI `image` job |
| **CI/CD** | `.github/workflows/ci.yml` | `scripts/server_deploy.sh` | CI itself |
| **Server setup** | `scripts/setup_server.sh` | `deploy/docker-compose.yml`, `scripts/server_deploy.sh` | manual |
| **Data release / rollback** | `scripts/deploy_data.sh` | | manual |

## How to make common changes

**Add a chain alias.** Add `"variant": "canonical"` (both already normalized) to `CHAIN_ALIASES`, add a test case, then re-run `build_chains.py` and `build_serving_db.py` and deploy the data.

**Tune chain matching or duplicates.**
1. Change `ChainRules` or the SQL in `chains.py`.
2. Add a test reproducing the real case.
3. Re-run `build_chains.py`, then compare `chains_manifest.json` spot checks with the previous run.
4. Use `investigate_chain.py --chain "<key>"` to see the effect on one chain.

**Add an API endpoint.**
1. Add a Pydantic response model and a route inside `create_app()` in `api.py`, taking `db: Db` for a per-request cursor.
2. If it can be expensive, add a `Rule` in `ratelimit.py`.
3. Test it against the shared `client` fixture.
4. Document it in LLD §3.2.

**Add an export format.**
1. Add a value to `ExportFormat` and a branch in `to_csv`.
2. Write tests for the header and one row.
3. Record the platform's documented format, with its source, in LLD §3.3.

**Change the frontend.** Edit `src/turf_app/web/`, run the API locally (`TURF_DB=… uv run uvicorn turf_app.api:app --reload`), then `uv run python scripts/browser_smoke.py --base-url http://127.0.0.1:8000` and look at the screenshots in `data/screenshots/`. Loading anything from a new origin (fonts, tiles, scripts) needs a CSP change in `api.py`. To upgrade MapLibre, download the new `dist/` files into a new `vendor/maplibre-gl-X.Y.Z/`, check their hashes against a second CDN, and update the two paths in `index.html`.

**Change the serving DB schema.**
1. Edit `serving.py` and bump `SCHEMA_VERSION`.
2. Update every query that reads the changed table, and `tests/conftest.py` if new columns are needed.
3. The data and the code have to be deployed together: deploy the data first if the new code needs the new columns.

**Add a pipeline step.** Put the logic in `src/turf_app/<step>.py` with a test on small tables, add a thin `scripts/<step>.py` CLI that takes `--release`, and add it to the table in LLD §1.

**Deploy.**
- **Code:** merge to `main`; CI does the rest.
- **Data:** `TURF_HOST=ovh scripts/deploy_data.sh <release>`.
- **Server setup** (compose file, deploy script): `TURF_HOST=ovh scripts/setup_server.sh <ci-key.pub>`.
- **Caddy:** edit on the server following `deploy/Caddyfile.snippet`; back up, `caddy validate`, then reload.

## Checks before committing

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy scripts src tests && uv run pytest -q
```

Chain these with `&&` so a failure stops the commit. CI runs the same checks.

## Conventions

- Dependencies: runtime in `[project].dependencies`; offline-only packages in the `pipeline` group; tools in `dev`. Local `uv sync` installs all three.
- Python 3.12, `uv`, `mypy --strict`, ruff (line length 100; long lines are allowed in `tests/` for data tables).
- DuckDB SQL is written inline; Python functions are registered as DuckDB functions only for logic that must also run in the API (normalization).
- Every runnable script lives in `scripts/`; useful one-off queries get saved there too.
- Tests built from real data quote the real row in a comment.
- **Docs:** any change to architecture, rules, endpoints, pipeline steps, deploy, or file layout updates `documents/` in the same commit.
