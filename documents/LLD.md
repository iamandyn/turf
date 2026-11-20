# Turf: Low-Level Design

> Keep this current. Update the matching section whenever you change a rule, threshold, schema, endpoint, or deploy step. Overview: [HLD.md](HLD.md). Where the code lives: [CODE_GUIDE.md](CODE_GUIDE.md).

Numbers quoted here come from the `2026-11-18.0` release unless stated otherwise.

---

## 1. Data pipeline

The steps run in order on a dev machine. Each one reads the previous step's files from `data/<release>/` (gitignored) and writes its own.

| Step | Script | Reads | Writes | Run time / peak memory |
|---|---|---|---|---|
| 0 (optional) | `scripts/inspect_places.py` | S3 | stdout (schema, sample stats) | ~6 s |
| 1 | `scripts/extract_us.py` | S3 places | `us_places.parquet`, `extract_manifest.json` | 72 s / 5.3 GB |
| 1b | `scripts/extract_states.py` | S3 divisions | `us_states.parquet` (7 MB) | seconds |
| 1c | `scripts/extract_metros.py` | Census CBSA boundaries and population estimates (pinned downloads) | `us_metros.parquet` (5 MB) | seconds |
| 2 | `scripts/build_chains.py` | steps 1, 1b | `places.parquet`, `chains.parquet`, `chains_manifest.json` | ~3 min / 8.6 GB |
| 3 | `scripts/build_serving_db.py` | steps 2, 1c | `turf.duckdb` (5.5 MB) | 17 s / 4.2 GB (`--memory-limit`, default 4 GB) |
| briefs | `scripts/brief_inputs.py`, `scripts/assemble_briefs.py` | step 3 | `src/turf_app/briefs.json` (in git) | seconds |
| checks | `scripts/check_extract.py`, `scripts/investigate_chain.py` | steps 1, 2 | stdout | seconds |

All steps take `--release` (e.g. `2026-11-18.0`). List the available releases with a DuckDB `glob('s3://overturemaps-us-west-2/release/*/theme=places/type=place/*')`; S3 keeps only the last few.

### 1.1 Step 1: US extract (`extract_us.py`)

- **Source:** `s3://overturemaps-us-west-2/release/<release>/theme=places/type=place/*`, read anonymously.
- **Row-group pruning:** filters on the `bbox` struct columns for three boxes (lower 48, Alaska, Hawaii), so DuckDB skips non-US row groups.
- **Keep:** `addresses[1].country = 'US'`, `names.primary IS NOT NULL`, and `operating_status <> 'permanently_closed'`.
- **Coordinates:** read from `bbox.xmin` / `bbox.ymin`. Places are points, so this avoids decoding each geometry.
- **Output columns:** `id, name, brand_name, brand_wikidata, basic_category, taxonomy_primary, taxonomy_hierarchy, confidence, operating_status, address, locality, region, postcode, lon, lat, source_datasets`.
- **Not covered:** US territories, the Aleutians west of 180°, and places whose first address has no country.
- **Result:** 18,339,272 rows. 9.1% have a real brand name; 25% have no operating status.

### 1.2 Step 1b: state boundaries (`extract_states.py`)

- **Source:** `theme=divisions/type=division_area`, filtered to `country='US' AND subtype='region'`.
- **Output:** `state` (two-letter code), `class` (`land` or `maritime`), `geometry`. That's 51 land polygons (50 states plus DC) and 23 maritime polygons.

### 1.2b Step 1c: metro areas and population (`extract_metros.py`)

- **Why not Overture:** Overture has no metro areas (its divisions go country → region → county → locality) and no population. Brands plan and buy by metro, and the standard public definition is the Census Bureau's Core-Based Statistical Areas.
- **Sources** (each downloaded to its own folder under `data/<release>/` and **checked against a pinned SHA-256**):
  - boundaries: `cb_2024_us_cbsa_500k.zip` (2024 cartographic boundaries, July 2023 delineations), read with DuckDB spatial (`ST_Read` over `/vsizip/`);
  - population: `cbsa-est2025-alldata.csv` (Vintage 2025 estimates, Latin-1), whole metro/micro rows only (the file also has counties and metropolitan divisions).
- **Output:** `code` (e.g. `12420`), `name` ("Austin-Round Rock-San Marcos, TX"), `kind` (`metro` | `micro`), `search_key` (`normalize_text(name)`), `population` (2025), `population_2020`, `geometry`. Puerto Rico is dropped: 387 metros and 538 micro areas, every one with a population (the script fails otherwise).
- **Updating:** change the URL and SHA-256 constants when the Census publishes a new vintage.

### 1.3 Step 2: chains, duplicates, states (`build_chains.py` → `chains.build()`, `states.fill_states()`)

See §2 for the rules. Output `places.parquet` columns:
`id, name, brand_name, chain_key, chain_match ('brand'|'name'|NULL), basic_category, taxonomy_primary, confidence, operating_status, address, locality, state, postcode, lat, lon, source_datasets, is_duplicate`.

`chains.parquet`: `chain_key, n_tagged, name_agreement, display_name, categories (list, by share)`.

`chains_manifest.json` records totals, the state-fill report, and spot-check counts for four known chains. **Compare it between releases**: large swings in the spot checks mean a rule broke or the upstream data changed.

**Why chain matching survives the pivot:** duplicate detection (§2.4) needs it. The same store listed twice would otherwise count twice in a metro's category shares. `chains.parquet` itself isn't served any more.

### 1.4 Step 3: serving database (`serving.write_serving_db()`)

The served file holds **per-metro aggregates only**, 5.5 MB (it was 1.37 GB when it held every place). It's written to `turf.duckdb.building` and then renamed, so a reader never sees a half-written file.

| Table | Contents |
|---|---|
| `metros` | `metro_code, name, kind, search_key, population, population_2020, growth_pct, n_places, n_open, center_lat, center_lon` (mean of its places, for the map marker), `min/max_lat/lon` (to frame it): 925 rows |
| `metro_category` | `metro_code, category, n_open`: open, unique places per metro and Overture category; 642,859 rows (only non-zero counts) |
| `categories` | `category_key, label, search_key, parent, n_total, n_open` nationwide; 2,060 categories, for signal search |
| `metro_zips` | `metro_code, zip, n_places`: each 5-digit ZIP goes to the metro holding most of its places, and only with at least `MIN_PLACES_PER_ZIP` (5) there, so stray or mistyped postcodes don't add far-away ZIPs; 26,440 ZIPs |
| `meta` | `release, schema_version (5), built_at_utc` |

**Metro tagging** is a point-in-polygon join of the unique places against `us_metros.parquet`, written as an **inner** join so DuckDB plans it with its spatial join operator (about 14 s for 18M points); a LEFT spatial join falls back to a nested loop. CBSAs don't overlap, so each place gets at most one metro, from its coordinates rather than its address. 17.2M of the 18.2M unique places fall in a metro or micro area.

Schema history: v1 had no categories; v2 added categories; v3 metros; v4 a month-over-month change view (v1 product); **v5** is the market-selection schema above. v5 code needs v5 data and the v1-era code can't read v5 data, so ship them together (§4.6).

---

## 2. Normalization and matching rules

### 2.1 Text (`normalize.py`)

| Function | Rule | Example |
|---|---|---|
| `normalize_text` | NFKD, strip accents, lowercase, `&`→` and `, drop apostrophes, other non-alphanumerics → space, collapse spaces | `McDonald’s` → `mcdonalds`; `Anh Phương` → `anh phuong` |
| `chain_key` | `normalize_text`, then repeatedly strip trailing generic suffixes (`coffee company, corporation, company, corp, inc, llc, ltd, co`), then apply `CHAIN_ALIASES` | `Starbucks Coffee Company` → `starbucks`; `Exxon` → `exxonmobil` |
| `name_prefixes` | Whole-token prefixes of the normalized name, longest first, max 5 | `Subwayside Deli` never yields `subway` |
| `normalize_state` | `US-TX`, `TX`, `Tx`, `Texas` → `TX`; territory codes and anything else → `None` | `US-PR` → `None` |
| `street_key` | Street number + first street word, skipping one direction word, with aliases (`ih/i`→`interstate`, `hwy`→`highway`, `st`→`saint`, `2nd`→`second`, …) | `9500 S Ih 35` and `9500 S Interstate 35` → `9500 interstate` |

The API normalizes search queries with `normalize_text`. **Changing a rule here changes search behavior too.**

### 2.2 State values

The raw data uses 118 different `region` values (`US-TX`, `TX`, full names, odd casing). After `normalize_state`, 141,618 places had no state. `fill_states` then:

1. Checks, on a 100k sample *before* filling, how many address states disagree with the location. Result: 113 out of 98,911 (0.11%). Address states are kept, not overwritten.
2. Fills missing states by point-in-polygon on **land** boundaries (140,370 filled), then **maritime** boundaries (1,218, for piers and shorelines).
3. 30 places remain without a state, outside every boundary.

### 2.3 Chain matching (`chains.build()`, thresholds in `ChainRules`)

What the real data showed, and the rule each finding led to:

| Finding | Rule |
|---|---|
| 41,592 of 50,809 brand structs in a sample had no name | Brand comes from `brand.names.primary`; empty means none |
| Many real chain locations have no brand, only the name | Match by **brand or name** |
| Correct brand tags sit on places named only by location ("Mansfield, TX" for an Orangetheory studio) | A brand tag doesn't need the name to agree |
| Some brand tags are wrong ("Anh Phương" on dozens of individual beauty professionals) | Each chain must **earn trust** (below) |
| "Subway" could match a subway station | Name matches must fall in the chain's **usual categories** |

**Algorithm:**

1. Compute `bkey = chain_key(brand_name)` and the prefixes of each name. Python functions run only on *distinct* values, then the results are joined back.
2. For each `bkey`, compute `name_agreement`: the share of its tagged places whose name prefix (after aliases) equals `bkey`.
3. A chain exists if `n_tagged >= min_tagged (3)`, `name_agreement >= min_name_agreement (0.3)`, and it isn't in `EXCLUDED_CHAINS` (kiosks and counters inside other stores: Amazon Locker, Western Union, Redbox, …).
4. A chain's categories are the `basic_category` values making up at least `min_category_share (5%)` of its **name-agreeing** tagged places. Only agreeing places count, so one bad tag can't add a category.
5. Assignment, first match wins:
   - **brand:** trusted `bkey`, and the name agrees *or* the place's category is one of the chain's.
   - **name:** the longest name prefix (after aliases) that equals a trusted chain key, and the category is one of the chain's.

Result: 2,213,078 places in 40,424 chains (1.08M matched by brand, 1.13M by name).

### 2.4 Duplicates (`chains.build()` step 5)

Two places of the **same chain** are duplicates if:
- they have the same `street_key` and are within **1,000 m** (`dup_same_street_m`), or
- they are within **30 m** (`dup_any_m`), whatever the address says.

The better row survives: known-open beats unknown status, then higher confidence, then `id`. Rows are **flagged** (`is_duplicate`), not deleted. 112,494 duplicates were flagged.

- **Why 1 km:** a real duplicate pair ("9500 S IH 35" vs. "9500 S Interstate 35") was 518 m apart.
- **Why the same street word:** "1 Plattekill Ave" vs. "1 Hawk Dr" share a number but are different places.
- **Known misses:** different formats for the same address ("957 US Highway 9" vs. "957 Route 9 N"), mall vs. street addresses, and Planet Fitness pairs like "3630 vs. 3636 W Southern Ave" (68 m). About 1–2% for Planet Fitness. Tightening the rules would merge real stores in dense downtowns.

### 2.5 Open vs. unverified

`operating_status` follows Overture's `Overture-signals` source exactly. Places with that signal are `open`; every place with no status lacks it. So **no status means no recent evidence the place is operating**. That's 17.7% of unique chain locations, and it explains most over-counting (Starbucks: 19,330 unique, 17,522 open).

**Product rule:** metro signals count **open places only** (`metro_category.n_open`), so a metro's mix isn't inflated by listings that may be closed.

---

## 3. API

Built by `create_app(db_path=None, rate_limiter=None, state_dir=None, platform_fault_rate=None, connector_sleep=None)` in `api.py` (the last three are for the connector; tests pass a temp dir and a no-op sleep). `uvicorn turf_app.api:app` builds it lazily through a module `__getattr__`, so importing the module (e.g. in tests) needs no database.

### 3.1 Configuration

| Env var | Default | Meaning |
|---|---|---|
| `TURF_DB` | `data/turf.duckdb` | Serving database (opened read-only) |
| `TURF_DB_MEMORY` | `512MB` | DuckDB `memory_limit` per worker |
| `TURF_DB_THREADS` | `2` | DuckDB threads per worker |
| `TURF_COMMIT` | `dev` | Baked into the image by CI (`--build-arg TURF_COMMIT=<sha>`); returned by `/api/meta` |
| `TURF_CLIENT_IP_HEADER` | unset (image: `X-Client-IP`) | Header holding the real client IP, for rate limiting. Unset means the TCP peer address is used |
| `TURF_STATE_DIR` | system temp dir (image: `/tmp`, a tmpfs) | Writable directory for the connector's and the simulated platform's SQLite files |
| `TURF_PLATFORM_FAULT_RATE` | `0.2` | Share of first requests for a page the simulated platform refuses (429 or 503) |
| `TURF_PLATFORM_SECRET` | `turf-simulated-platform` | HMAC key for the simulated platform's tokens (shared by workers; it only guards synthetic data) |
| `TURF_ADS_API_BASE` | unset | Point the connector at a platform over the network instead of the in-process simulated one |
| `TURF_MCP_ALLOWED_HOSTS` | localhost forms + `testserver` (image: adds `turf.dunkasaur.gg`) | Host headers `/mcp` accepts (DNS-rebinding protection) |

**Concurrency:** one DuckDB connection per worker, opened in `lifespan`, and one **cursor per request** (the `Db` dependency); MCP tools take their own cursor per call. Connections aren't thread-safe; cursors are. At startup the app checks that every preset category exists in the data and fails to start otherwise.

### 3.2 Endpoints

| Method and path | Params / body | Returns | Errors |
|---|---|---|---|
| `GET /healthz` | | `{status: "ok"}` | |
| `GET /api/meta` | | `{commit, release, built_at_utc, metros, categories, briefs}` | |
| `GET /api/presets` | | `[{key, label, example, categories, has_briefs}]` | |
| `GET /api/categories/search` | `q` (2–80 chars), `limit` (1–25) | `[{key, label, parent, n_open}]`, bigger categories first within a tier | 422 |
| `GET /api/metros/search` | `q` (2–80 chars), `limit` (1–25) | `[Metro]`: `{metro_code, name, kind, population, growth_pct, bounds}`; metros before micros, then bigger first | 422 |
| `GET /api/metros/{metro_code}` | | `Metro` (restores `?seed=` links) | 404 |
| `POST /api/markets` | `MarketRequest` | `MarketResponse` | 404 unknown category or seed; 422 |
| `POST /api/markets/export?format=metros\|zips` | `MarketRequest` | CSV attachment | 404, 422 |
| `POST /api/performance` | `PerformanceRequest` | `PerformanceResponse` (§3.8) | 404 unknown category; 422 nothing matched, bad rows |
| `POST /api/performance/export?format=metros\|zips` | `PerformanceRequest` | CSV of the recommendations (+ `brand_status`) | 404, 422 |
| `GET /api/samples` | | `[{key, name, description, preset, kpi, geo_type, synthetic}]` | |
| `GET /api/samples/{key}` | | the sample with its synthetic `rows` (generated once per process) | 404 |
| `GET /api/briefs/{preset}/{metro_code}` | | `Brief` | 404 |
| `POST /api/connections` | `{sample}` | `{connection, sync}`: a simulated account connected, backfill queued (§3.9) | 404 unknown sample |
| `GET /api/connections/{id}` | | the connection, stored row count, latest sync | 404 (expired) |
| `DELETE /api/connections/{id}` | | 204; the connection, its jobs and rows are deleted | 404 |
| `POST /api/connections/{id}/syncs` | | 202, the queued sync | 404; 409 with the active sync |
| `GET /api/connections/{id}/syncs/{sync}` | | status, mode, range, stats, result (changes), error | 404 |
| `GET /api/connections/{id}/syncs/{sync}/events` | `Last-Event-ID` header | `text/event-stream` (§3.9) | 404 |
| `GET /api/connections/{id}/results` | | the report window's per-market rows, shaped for `/api/performance` | 404; 409 nothing synced |
| `/mock/ads/*` | | the simulated ad platform (its own OpenAPI at `/mock/ads/docs`) | Meta-style `{"error": {...}}` |
| `POST /mcp` | MCP JSON-RPC (streamable HTTP) | see §3.6 | 421 unknown Host |
| `/`, `/index.html` | | the page, with versioned asset URLs | |

**Search tiers** (categories and metros): prefix, then word-start, then substring, then Jaro-Winkler ≥ 0.88 (typos like "pet grommer").

### 3.3 Markets (`markets.py`)

`MarketRequest` (unknown fields are a 422):

| Field | Type / limits | Default |
|---|---|---|
| `preset` | a key in `PRESETS` | |
| `categories` | up to 20 Overture categories; overrides the preset's | the preset's |
| `use_growth` | add population growth 2020–25 as a signal | false |
| `seeds` | up to 10 five-digit CBSA codes; empty ranks by signal strength | `[]` |
| `min_population` | 0–5,000,000 | 250,000 |
| `include_micro` | include micropolitan areas | false |
| `limit` | 1–100 results | 25 |

At least one signal is required (categories or growth).

**Presets** (`PRESETS`, matching Valid's verticals): pets, fitness, home services, beauty & wellness, young professionals, families. Each is 5–7 Overture categories.

**Algorithm:**
1. **Universe:** metros of the chosen kinds with at least `min_population` people, **plus the seeds** whatever their size. Fewer than 10 is a 422 ("lower the minimum population").
2. **Signal value:** for each category, its **share of the metro's open places** in percent. Not a per-capita rate: Overture's coverage varies 3× between metros (2,100 to 6,200 open places per 100k residents), so per-capita rates would partly measure mapping quality. Growth is the Census 2020–25 change in percent.
3. **Standardize:** shares are log-scaled (`log1p(share per 10k places)`, because a few metros have several times the typical share), then z-scored across the universe, so every signal counts equally. Growth is z-scored directly.
4. **Rank:**
   - with seeds: Euclidean distance (root mean square over signals) from the seeds' mean z-profile, closest first; `match = round(100 · e^(−distance))`;
   - without seeds: mean z, highest first.
5. **Reasons:** for every signal, the metro's value / the universe's median ("ratio"), sorted by the metro's z-score. Growth ratios compare (100 + growth) multipliers.

**Real example** (2026-11-18.0, pets preset, seeds Denver and Austin): Boulder (73% match; dog walkers 3.2×), Raleigh, Philadelphia, Des Moines, Portland OR, San Diego, Seattle.

`MarketResponse = {mode: "lookalike" | "signal_strength", signals, universe, seeds: [MarketResult], results: [MarketResult]}`, with `MarketResult = {rank, metro_code, name, kind, population, growth_pct, score, match, reasons: [{signal, label, ratio, value, typical}], center, bounds, zip_count}`.

**Exports:**
- `metros`: `rank, metro, cbsa_code, kind, population, growth_2020_25_pct, match, zip_count, top_signals` (top three, as "label N× typical").
- `zips`: `zip, country, metro, metro_rank`, one row per ZIP in rank order. Meta and Google Ads both accept US ZIP lists in their bulk location boxes; neither targets CBSAs.

The filename is `turf-<preset or custom>-<lookalikes | top>-<format>.csv`.

### 3.4 Frontend and static serving

`create_app()` mounts `StaticFiles(src/turf_app/web)` at `/` **after** all API and MCP routes, so they always win. `/` and `/index.html` are a route (`render_index`) that rewrites `"/theme.js"`, `"/styles.css"` and `"/app.js"` to `?v=<first 12 hex of the file's SHA-256>`. **Why:** Cloudflare gives `.js`/`.css` a 4-hour browser cache (`max-age=14400`) regardless of the origin's `no-cache`, so a deploy could pair the new page with the old script; that is how dark mode first shipped broken for a returning browser. Versioned URLs are served `Cache-Control: public, max-age=31536000, immutable`; the page and unversioned paths stay `no-cache`. New frontend files must be added to `VERSIONED_ASSETS`. The files ship inside the Python package (hatch includes them in the wheel), so the image needs no separate frontend step.

**Response headers** (same middleware as rate limiting):
- Every response gets `X-Content-Type-Options: nosniff` and `Referrer-Policy: strict-origin-when-cross-origin`.
- Frontend responses also get a CSP, and `Cache-Control: no-cache` except versioned assets (`immutable`, see above). `/api/*`, `/docs` and `/openapi.json` don't get either.
- **CSP:** `default-src 'self'`; `script-src 'self' https://static.cloudflareinsights.com` (Cloudflare Web Analytics is on for the zone and injects its beacon; `connect-src` also allows `https://cloudflareinsights.com`). Found by the browser test, since the injection only happens behind Cloudflare; `style-src 'self' 'unsafe-inline'` (MapLibre sets inline styles); `img-src`/`connect-src` add `https://tiles.openfreemap.org`; `worker-src blob:` (MapLibre's worker); `frame-ancestors 'none'`.

**`web/app.js`** (one ES module, no framework):

| Part | What it does |
|---|---|
| start screen | `renderStart`: four sample-brand cards (`/api/samples`), a CSV upload (`parseResults`, below), and once a file is read, the one question: brand category (a select over `/api/presets`), then "Build the report" |
| `parseResults` | client-side CSV parsing: comma/semicolon/tab, quoted fields, BOM. Column names from `COLUMNS` (ZIP, market/DMA/city, state, spend/cost/amount spent, conversions/purchases/results/leads/installs, revenue/conversion value); joins separate city and state columns; ROAS when revenue is present, else CPA; 20,000 rows / 10 MB max |
| `buildReport` | `POST /api/performance` with the rows, category (preset) or adjusted signals, `limit` 15; a request counter drops stale responses |
| `renderReport` | the sections: where to focus next (top 5 cards, "show more" for 15), how much to trust this (plain-language verdict + the statistics), what drives results (signals with weight and rho ≥ 0.15, with their best-market ratio), markets today (best 5, worst 5, full table), how the report was made, adjust signals |
| recommendation cards | `recCard`: rank, name, status, expected KPI with range and the similar markets, snapshot. `selectRec` expands one (similar markets with KPI, signal bars vs. typical, facts), loads its brief (`/api/briefs/{preset}/{metro}`) if there is one, frames it on the map, and puts `&metro=` in the URL |
| map | GeoJSON sources `current` (grey: markets the brand runs in), `seeds` (blue: best markets), `results` (orange: recommendations, sized by population), `selected` (ring); hover shows the name; clicking a recommendation selects it. Legend buttons toggle layers |
| about dialog | `<dialog id="about">`: the demo disclaimer (fictional samples, no live ingestion, no real-time AI, data sources, recommendations as a starting point). Opens on the first visit (remembered in `localStorage`, `turf-about-seen`) and from the header |
| print | a `beforeprint` handler expands every recommendation and loads its brief; print CSS hides the map, controls and export bar |
| theme | as before: CSS tokens, `theme.js` before first paint, basemap swap with `transformStyle` |
| URL | `?sample=<key>&metro=<code>` for sample reports; uploaded data is never put in the URL |

The phone layout (≤ 800 px) puts the map on top and the report below.

### 3.5 Rate limiting (`ratelimit.py`)

An in-memory sliding window per `(client IP, rule)`, per worker. **The first matching rule wins:**

| Rule | Limit |
|---|---|
| `POST /api/performance*` | 30 / 60 s |
| `POST /api/markets*` | 60 / 60 s |
| `/mcp` | 120 / 60 s |
| `/api/*` | 300 / 60 s |
| anything else (incl. `/healthz`) | unlimited |

Over the limit returns `429` with `Retry-After` in seconds. Tracked keys are capped at 50,000; past that, all counts are cleared. **Client IP chain:** Cloudflare's `CF-Connecting-IP`, which Caddy trusts only from Cloudflare's ranges, becomes Caddy's `{client_ip}`, which goes to the app as `X-Client-IP`, overwriting any visitor value. **Never** key on `X-Forwarded-For`: Cloudflare keeps a visitor-supplied value in it.

### 3.6 MCP server (`mcp_server.py`)

Seven read-only tools (`readOnlyHint`), served at `/mcp` by the MCP Python SDK 2.x (`MCPServer`, streamable HTTP, `stateless_http=True`, JSON responses), inside the same app so they share the database, rate limiting and deploys:

| Tool | Does |
|---|---|
| `list_presets` | presets and their signal categories |
| `search_metros(query)` | metros by name → codes |
| `search_signals(query)` | categories for custom signals |
| `rank_markets(preset?, signals?, seed_metro_codes?, min_population, include_micro, use_population_growth, limit)` | same as `POST /api/markets`, compact: top three reasons as text |
| `recommend_from_results(rows, kpi, geo_type, preset?, signals?, limit)` | same as `POST /api/performance`, compact: best markets, the verdict sentence, signals that matter, recommendations |
| `metro_zip_codes(metro_code)` | ZIPs for targeting |
| `get_brief(preset, metro_code)` | the pre-written brief or null |

Implementation notes:
- **Host check:** the SDK's default transport security only allows `localhost` Host headers, which would reject the public domain, so allowed hosts come from `TURF_MCP_ALLOWED_HOSTS`; anything else gets 421.
- **Startup:** a session manager can only run once, and an app can start more than once (tests do), so `mcp_server.start()` recreates it at each startup and the `/mcp` route delegates to whichever is current.
- **No auth:** the data is public and the tools are read-only.

### 3.7 Briefs (`briefs.py`, `briefs.json`)

65 pre-written creative briefs: each preset's top 10 metros by signal strength, plus the top 5 lookalikes in the demo scenario (pets, Denver + Austin).
- **How they were made:** `scripts/brief_inputs.py` dumps each pair's numbers (signals vs. typical, Census figures, the categories the metro is most over-represented in). The prose (headline, audience, angles, local context) was written once from those numbers, by Claude in a Claude Code session, and lives in `scripts/assemble_briefs.py`. The "Based on" lines are generated from the data by that script, never typed.
- **Why no live LLM calls:** the live demo never calls a model, so it costs nothing however much it's used.
- **Production version:** generate on demand with an API key, cache per (preset, metro), and cap daily spend.

### 3.8 Brand results (`performance.py`, `samples.py`)

`PerformanceRequest`: `rows` (1–20,000 of `{geo, spend, conversions?, revenue?}`), `geo_type` (`zip` | `metro`), `kpi` (`roas` needs revenue, `cpa` needs conversions), the market fields (`preset`/`categories`, `use_growth`, `min_population`, `include_micro`, `limit`), `best_count` (1–10, default 5), `min_spend_share` (default 1%).

1. **Matching** (`match_rows`):
   - ZIPs: the first five digits, through `metro_zips`.
   - Names: an exact full metro name, a 5-digit CBSA code, or a principal city with an optional state. Each metro name is split into principal cities and states ("Austin-Round Rock-San Marcos, TX"), so "Austin, TX", "Austin TX", "Round Rock TX" and "Austin" all match, but "Austin, MN" goes to Austin, MN.
   - Unmatched rows are counted, and up to 10 examples are returned.
   - The frontend joins separate city and state columns.
2. **KPI per metro:** sums per metro; ROAS = revenue / spend, CPA = spend / conversions. A metro is **trusted** with at least `min_spend_share` of total spend (a $1 test with ROAS 1,000 isn't a best market). The best markets are the top `best_count` trusted metros.
3. **Signal check:**
   - Profiles cover every metro the brand runs in (`build_profiles(extra_metros=...)`), whatever its size.
   - Per signal: Spearman of its z-score with the KPI (sign flipped for CPA) across trusted metros; weight = max(0, rho).
   - Out-of-sample: leave-one-out. For each metro, weights come from the others and score that metro, then Spearman(scores, KPI). Each fold uses the full-sample ranks with the held-out metro's terms subtracted, O(1) per signal.
   - That statistic is biased upward (only helpful signals keep weight; shuffled data scores around +0.3), so the same procedure runs on **999 permutations** of the KPI (seeded, deterministic) for the chance level (95th percentile) and a p-value.
   - Verdict: `strong` p < 0.05, `moderate` p < 0.10, `weak` otherwise, `too_few` under 6 trusted metros. With no real relationship p is uniform, so each cut-off is its false-alarm rate. The weights are used for recommendations only when `strong` or `moderate`.
   - Real samples: pets p = 0.006, fitness 0.015, home services 0.01, beauty (no relationship) 0.10, so `weak`.
4. **Recommendations:** `rank_markets` with the best markets as seeds, the fitted weights, and metros with at least 0.5% of spend excluded (already in the plan). Each result gets a status: `low_spend` (in the results, below that) or `not_running`.

The rows are used for the request only and never stored.

**Samples** (`samples.py`):
- Four fictional brands ("Sample pet-care app", …), flagged `synthetic`.
- Generated deterministically: the largest N metros; spend ∝ population^0.9 × lognormal noise; KPI = base × exp(±effect × signal strength + Gaussian noise). Effects: 0.45 / 0.35 / 0.30 / 0.0 (beauty).
- One brand reports by ZIP (8,813 rows), the others by market name.
- They demonstrate the flow; their verdicts are built in, and the page and README say so.

### 3.9 Ad-account connector (`connector/`)

**Simulated platform** (`platform.py`, mounted at `/mock/ads`; a separate FastAPI app):
- Accounts: `POST /_demo/accounts {template}` creates one from a sample brand (`act_<hex>`), with a simulated "today" (initially the real UTC date). `POST /_demo/accounts/{id}/advance {days: 1–7}` moves it forward, at most 60 days in total. Accounts live in their own SQLite file; they expire after a day (at most 1,000).
- Auth: `POST /oauth/access_token` (form: `client_id`, `client_secret`, `account_id`) returns a bearer token `"<account>.<expiry>.<hmac>"`, valid 10 minutes, verifiable by any worker. Missing/invalid → 401 code 190; expired → 190 subcode 463; another account → 403 code 200.
- `GET /v1/{account}`: name, currency, `current_date`, `data_through` (yesterday on the platform).
- `GET /v1/{account}/insights?since&until&limit(1–500)&after`: rows `{date_start, date_stop, region, spend: "12.34", actions: [{action_type: "purchase", value: "9"}], action_values?}` ordered by day then market, through `data_through` only. Paging: `paging.cursors.after` (base64 of offset + query; a cursor from another query is a 400) and `paging.next`.
- Numbers: each metro's monthly totals (`samples.metro_totals`) / 30, × weekday factor (0.85 weekends, 1.06 weekdays), × lognormal(0, 0.12) spend noise; KPI quality = exp(trend × days since the account started / 30 + N(0, 0.18)), trend ~ N(0, 0.12) per metro; purchases stochastically rounded. Plus an `Unknown` market (≈0.4% of spend) each day. Seeded by (sample, metro, day): deterministic.
- Late conversions: a day's reported purchases and value are × 0.72 / 0.90 / 0.97 when it is 1 / 2 / 3 days old, then final.
- Faults: the first request for each (query, offset) fails with probability `fault_rate` (¾ as 429 code 17 with `Retry-After: 1`, ¼ as 503 code 2, both `is_transient`); its retry succeeds. Tracked per process: the connector's requests for a sync all come from one process.

**Client** (`client.py`): tokens on first use and on 401/190 (once per request); retries 429, 5xx, timeouts and network errors up to 5 attempts, waiting `Retry-After` if given, else full-jitter exponential backoff from 0.5 s, each wait capped at 8 s; other 4xx fail at once. Rows are parsed into `InsightRow` (date, market, spend, conversions = sum of `purchase` actions, revenue = sum of `purchase` action values; Decimal parsing, non-negative, one day per row); rows that fail are skipped and counted with a reason.

**Store** (`store.py`, SQLite in WAL mode, `$TURF_STATE_DIR/turf-connector.sqlite3`, a connection per operation):
- `connections(id, sample_key, account_id, created_at, last_seen, synced_through, last_report)`: the id is a 128-bit random token. Idle connections expire after 24 h; at most 300.
- `syncs(id, connection_id, status, mode, …, heartbeat, stats, result, error)`, with a partial unique index allowing one `queued`/`running` job per connection (a second request gets 409 and the active job). Running jobs heartbeat every page; one silent for 30 s, or queued for 2 minutes, is marked failed when anyone next reads (worker died or restarted).
- `sync_events(sync_id, seq, at, kind, data)`: the progress log.
- `insights(connection_id, date, geo, spend, conversions, revenue)` keyed by (connection, date, geo), written with `INSERT … ON CONFLICT DO UPDATE` only when values differ; each upsert counts inserted / updated / unchanged.
- Deleting a connection cascades to its jobs, events and rows.

**Sync job** (`sync.py`): first sync = backfill of the 28 days through `data_through`; later syncs = from 3 days before the day after `synced_through` through `data_through` (so a sync with nothing new still re-fetches 3 days, picking up late conversions). Pages of 100. Then the report: per-market totals over the 28 days through the newest synced day → `analyze()` with the sample's preset and KPI (limit 15) → summary (window, overall KPI, verdict, recommendations) → diff against the previous summary for the top 10: entered, left, moved (largest moves first, at most 5), KPI and verdict before and after.

**Jobs** (`routes.py`): an asyncio task in the worker that got the request, behind a per-worker semaphore of 2 (others stay `queued`). Shutdown cancels running jobs, which are marked failed. Events, in order: `queued`, `auth`, `started {mode, since, until}`, `retry {status, reason, attempt, wait_s, retry_after}`, `page {page, rows, inserted, updated, unchanged, rejected, reasons, first_day, last_day}`, `fetched {totals}`, `report {stage: started|done, changes}`, then `done` or `failed {error}`.

**Progress stream**: `GET …/events` polls the store every 250 ms and writes `id:`/`event:`/`data:` frames; it resumes after `Last-Event-ID`, sends a keepalive comment after 15 s of quiet, ends after the `done`/`failed` event (or 5 minutes), and sets `Cache-Control: no-cache` and `X-Accel-Buffering: no`. GZip middleware skips `text/event-stream`. Any worker can serve it.

**Frontend**: "Connect and sync" on the start screen → the report view opens with the sync panel only (`.report.awaiting`) and follows the job with `EventSource`. On `done` it loads `/results` into the brand's rows and builds the report through `/api/performance` (so exports and signal adjustments work as for an upload). The panel's buttons start a sync (a 409 means one is running: it follows that one) and call the platform's `advance` control. `?connection=<id>` restores the view.

**Rate limits**: `POST /api/connections*` 20/min, `/mock/*` 120/min per IP.

---

## 4. Deployment

### 4.1 Server layout (`ssh ovh`, Ubuntu 26.04)

```
/opt/turf/                      root:root 0755
├── docker-compose.yml          root 0644   (from deploy/docker-compose.yml)
├── bin/server-deploy           root 0755   (from scripts/server_deploy.sh)
├── state/.env                  turfdeploy  TURF_IMAGE=ghcr.io/iamandyn/turf:<sha>
└── data/                       ubuntu:turfdeploy 2775
    ├── 2026-11-18.0/turf.duckdb
    └── current -> 2026-11-18.0
```

- **Container:** `turf`, image from `state/.env`, port `127.0.0.1:8090→8000`. Limits: 4 GB (far more than the aggregates need now), 2 CPUs, read-only root filesystem, `/tmp` tmpfs, `no-new-privileges`. Data mounted read-only at `/data`. Health check on `/healthz`.
- **Docker:** daemon logs rotate at 10 MB × 3 (`/etc/docker/daemon.json`).
- **Image** (`Dockerfile`, two stages, ~330 MB): the builder runs `uv sync --frozen --no-default-groups --no-editable`; the runtime stage copies only `/app/.venv` into `python:3.12-slim`, so there's no uv, no download cache, and no `pipeline` dependency group (numpy).
- **Firewall (ufw):** allows 59595 (SSH), 80 and 443 only. Docker's firewall rules bypass ufw for *published* ports, which is why the port is published on 127.0.0.1 only.

### 4.2 Users and keys

| User | Can do | How |
|---|---|---|
| `ubuntu` (admin) | everything, via sudo | your personal key |
| `turfdeploy` | **only** `deploy <40-hex sha>` | `authorized_keys`: `restrict,command="/opt/turf/bin/server-deploy" <CI public key>`; member of `docker`; password locked |

Created and updated by `scripts/setup_server.sh`, which is re-runnable. CI is deliberately unable to change the compose file, the deploy script, or the Caddy config.

### 4.3 Deploy protocol (`scripts/server_deploy.sh`)

1. Validate `SSH_ORIGINAL_COMMAND` against `^deploy ([0-9a-f]{40})$`, else exit 2.
2. Read the registry user and token from stdin (two lines), and `docker login` into a **temporary** `DOCKER_CONFIG`, deleted on exit.
3. `docker pull ghcr.io/iamandyn/turf:<sha>`.
4. Record the previous `TURF_IMAGE`, write the new one atomically (temp file plus `mv`), and run `docker compose up -d`.
5. Poll `127.0.0.1:8090/api/meta` for `"commit":"<sha>"` for up to 60 s.
6. On success, prune all but the 5 newest image tags and exit 0. On failure, print logs, **switch back to the previous image**, and exit 1, which fails the CI job.

### 4.4 CI/CD (`.github/workflows/ci.yml`)

| Job | Runs on | Steps |
|---|---|---|
| `check` | every push and PR | `uv sync --frozen`, ruff check, ruff format --check, mypy (strict), pytest, `bash -n scripts/*.sh` |
| `image` | push to `main` | buildx, then push `ghcr.io/iamandyn/turf:<sha>` and `:main` with `TURF_COMMIT=<sha>` and a GitHub Actions cache |
| `deploy` | after `image` | SSH as `turfdeploy` with `deploy $GITHUB_SHA` (job token on stdin), then a public smoke test (`/healthz`, `/api/meta` commit equals SHA, metro search, a lookalike ranking), then `scripts/browser_smoke.py` against the live site in headless Chromium, with screenshots uploaded as a workflow artifact |

Third-party actions are **pinned to commit SHAs** (release tag in a comment); `.github/dependabot.yml` opens weekly update PRs.

**Secrets** (repo → Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `DEPLOY_SSH_KEY` | CI private key |
| `DEPLOY_KNOWN_HOSTS` | server host key line(s) |
| `DEPLOY_HOST` | server IP |
| `DEPLOY_PORT` | `59595` |

The `production` environment is optional; add required reviewers there to gate deploys.

### 4.5 Edge (`deploy/Caddyfile.snippet`)

- **Global options:** `servers { trusted_proxies static <Cloudflare ranges>; client_ip_headers CF-Connecting-IP }`. This is server-wide, so it also corrects client IPs for the other sites on the box.
- **Site:** `turf.dunkasaur.gg { encode zstd gzip; reverse_proxy 127.0.0.1:8090 { header_up X-Client-IP {client_ip} } }`.
- **DNS:** a proxied Cloudflare A record. Caddy gets its own Let's Encrypt certificate (HTTP-01 works through the proxy).
- **Refreshing ranges:** `scripts/update_caddy_cloudflare_ips.sh` backs up, rewrites, validates, and reloads, restoring the backup on failure.

### 4.6 Runbook

| Task | Command |
|---|---|
| New Overture release | run pipeline steps 1–3 (including 1b and 1c) with `--release X`, check `chains_manifest.json` spot checks, regenerate the briefs if the top metros changed, then `TURF_HOST=ovh scripts/deploy_data.sh X` |
| Ship a schema change with its code (e.g. v5) | upload the new `turf.duckdb` next to the current one and `mv` it into place **without** restarting: the running container keeps the old file open, so nothing breaks; then push the code, and the new container opens the new file |
| Roll back data | `TURF_HOST=ovh scripts/deploy_data.sh --rollback <older release>` |
| Roll back code | re-run the `deploy` job of an older commit in GitHub Actions, or on the server: `sudo -u turfdeploy SSH_ORIGINAL_COMMAND="deploy <sha>" /opt/turf/bin/server-deploy </dev/null` (only works if that image is still on the box or the package is public) |
| Logs | `ssh ovh docker logs --tail 100 turf` |
| Refresh Cloudflare ranges | `TURF_HOST=ovh scripts/update_caddy_cloudflare_ips.sh` |
| Change server setup | edit `deploy/docker-compose.yml` or `scripts/server_deploy.sh`, then `TURF_HOST=ovh scripts/setup_server.sh <ci-key.pub>` |

---

## 5. Testing

- **Unit:** `test_normalize.py`, `test_ratelimit.py` (fake clock).
- **Pipeline:** `test_chains.py` and `test_states.py` build tiny DuckDB tables shaped like real cases and run the real `build()` / `fill_states()`.
- **Markets and API:** `conftest.py` builds a serving database **with the real `write_serving_db`** from 12 synthetic metros with known category mixes (metro *i* has *i* pet groomers and 12 − *i* dog parks), so rankings are predictable. `test_markets.py` covers ranking, lookalikes, reasons, filters, validation, exports, ZIP assignment, presets and briefs; `test_performance.py` matching, totals, KPI direction, trusted metros, the strong / weak / too-few verdicts, recommendation statuses, exports, validation and the samples end to end; `test_api.py` the page, headers, versioned assets and rate limiting; `test_mcp.py` the MCP endpoint over HTTP; `test_connector.py` the simulated platform (auth, paging, late conversions, faults), the client (retries, `Retry-After`, giving up, no retry on 4xx, token refresh, invalid rows) against `httpx.MockTransport`, the store (idempotent upserts, one active job, reaping), sync ranges and diffs, and the API end to end (backfill with injected faults, the event stream and resuming it, an incremental sync after advancing the platform, 409, delete). Rate limiting is disabled in the shared client and tested separately.
- **Convention:** a test case taken from real data says so in a comment, quoting the actual row ("Real case: …").
- **Browser:** `scripts/browser_smoke.py` (Playwright, headless Chromium). It fails on any console error, page error, CSP violation or **axe-core accessibility violation** (report, dark mode, phone), and walks the report flow: the disclaimer on first visit (and not again), the sample report (5 recommendations with expected results, confidence, drivers, markets today), a recommendation's data and brief, show more, both exports, URL restore, the low-confidence sample, an uploaded report with a category and an unmatched row, adjusting a signal, connecting a simulated ad account (backfill, report, incremental sync, disconnect), dark mode, phone layout. Earlier flows it also covered: presets, removing and adding signals, seed metros and lookalikes, the detail panel and its brief, both exports, URL restore, the population filter, a sample brand (verdict, best markets, recommendation tags, export), the no-relationship sample ("No evidence"), an uploaded ad report with real column names, layer toggles, dark mode (kept after reload), phone layout. It runs locally against any base URL, and in CI after every deploy.
- **Not covered by automated tests:** the S3 and Census extract scripts (network, minutes), the shell deploy scripts beyond syntax, and pasting the ZIP export into Meta or Google Ads.
