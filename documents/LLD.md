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
| 1c | `scripts/extract_metros.py` | Census CBSA boundaries (pinned download) | `us_metros.parquet` (5 MB) | seconds |
| 2 | `scripts/build_chains.py` | steps 1, 1b | `places.parquet`, `chains.parquet`, `chains_manifest.json` | ~3 min / 8.6 GB |
| 3 | `scripts/build_serving_db.py` | steps 2, 1c | `turf.duckdb` (1.36 GB) | 27 s / 4.6 GB (`--memory-limit`, default 4 GB) |
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

### 1.2b Step 1c: metro areas (`extract_metros.py`)

- **Why not Overture:** Overture's divisions have no metro areas (they go country → region → county → locality). Media buyers plan by metro, and the standard public definition is the Census Bureau's Core-Based Statistical Areas.
- **Source:** Census cartographic boundary file `cb_2024_us_cbsa_500k.zip` (2024 vintage, July 2023 delineations), downloaded to `data/<release>/census_cbsa_2024/` and **checked against a pinned SHA-256**, so a rebuild uses the same file. Read with DuckDB spatial (`ST_Read` over `/vsizip/`).
- **Output:** `code` (CBSA code, e.g. `12420`), `name` ("Austin-Round Rock-San Marcos, TX"), `kind` (`metro` = metropolitan, `micro` = micropolitan), `search_key` (`normalize_text(name)`), `geometry` (NAD83, close enough to WGS84 for this). Puerto Rico is dropped: 387 metros and 538 micro areas.
- **Updating:** when the Census publishes a new vintage, change `URL` and `SHA256` in the script.

### 1.3 Step 2: chains, duplicates, states (`build_chains.py` → `chains.build()`, `states.fill_states()`)

See §2 for the rules. Output `places.parquet` columns:
`id, name, brand_name, chain_key, chain_match ('brand'|'name'|NULL), basic_category, taxonomy_primary, confidence, operating_status, address, locality, state, postcode, lat, lon, source_datasets, is_duplicate`.

`chains.parquet`: `chain_key, n_tagged, name_agreement, display_name, categories (list, by share)`.

`chains_manifest.json` records totals, the state-fill report, and spot-check counts for four known chains. **Compare it between releases**: large swings in the spot checks mean a rule broke or the upstream data changed.

### 1.4 Step 3: serving database (`serving.write_serving_db()`)

The file is written to `turf.duckdb.building`, then renamed, so a reader never sees a half-written file.

| Table | Contents |
|---|---|
| `places` | Non-duplicate rows only: `id, name, chain_key, basic_category, category (= taxonomy_primary), confidence, status, address, locality, state, postcode, lat, lon, metro` (CBSA code or NULL; 94.2% of places are in one). **Sorted by `chain_key, state`**, so DuckDB's per-block min/max statistics skip everything except the requested chain. Category filters scan all rows, which still takes about 8 ms |
| `chains` | `chain_key, display_name, categories, n_total, n_open`; only chains with at least 1 open location |
| `categories` | `category_key` (e.g. `pet_groomer`), `label` ("Pet groomer"), `search_key` ("pet groomer"), `parent` (most common `basic_category`), `n_total, n_open`; only categories with at least 1 open place |
| `metros` | `metro_code, name, kind, search_key, n_total, n_open, min_lat, min_lon, max_lat, max_lon` (the extent of its places, to frame it on the map); only areas with at least 1 place: 925 |
| `meta` | `release, schema_version (3), built_at_utc` |

**Metro tagging** is a point-in-polygon join of the unique places against `us_metros.parquet`, written as an **inner** join into a temp table and then left-joined by id: DuckDB plans the inner `ST_Contains` join with its spatial join operator (about 14 s for 18M points), while a LEFT spatial join would fall back to a nested loop. CBSAs don't overlap, so each place gets at most one metro. Metro membership comes from coordinates, not the address, so it's unaffected by bad address states: about 1,100 places inside the New York metro have an address state that isn't NY or NJ. The API never loads the spatial extension; it reads the plain `metro` code.

Result: 18,226,778 places, 40,026 chains, 2,060 categories, and 925 metro areas (1.36 GB).

Schema history: v1 had no categories; v2 added `places.category` and the `categories` table; **v3** added `places.metro` and the `metros` table. Each time, old code runs against the new file but new code needs the new data, so **deploy the data first** (`scripts/deploy_data.sh`), then push the code.

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

**Product rule:** the API returns **open-only by default**; `include_unverified` adds the rest, labeled `unverified` in exports.

---

## 3. API

Built by `create_app(db_path=None, rate_limiter=None)` in `api.py`. `uvicorn turf_app.api:app` builds it lazily through a module `__getattr__`, so importing the module (e.g. in tests) needs no database.

### 3.1 Configuration

| Env var | Default | Meaning |
|---|---|---|
| `TURF_DB` | `data/turf.duckdb` | Serving database (opened read-only) |
| `TURF_DB_MEMORY` | `1GB` | DuckDB `memory_limit` per worker |
| `TURF_DB_THREADS` | `2` | DuckDB threads per worker |
| `TURF_COMMIT` | `dev` | Baked into the image by CI (`--build-arg TURF_COMMIT=<sha>`); returned by `/api/meta` |
| `TURF_CLIENT_IP_HEADER` | unset (image: `X-Client-IP`) | Header holding the real client IP, for rate limiting. Unset means the TCP peer address is used |

**Concurrency:** one DuckDB connection per worker, opened in `lifespan`, and one **cursor per request** (the `Db` dependency). Connections aren't thread-safe; cursors are.

### 3.2 Endpoints

| Method and path | Params / body | Returns | Errors |
|---|---|---|---|
| `GET /healthz` | | `{"status":"ok"}` (runs `SELECT 1`) | |
| `GET /api/meta` | | `{commit, release, built_at_utc, places, chains}` | |
| `GET /api/search` | `q` (2–80 chars), `kind` (`chain`/`category`, optional), `limit` (1–25) | `[SearchResult]`: chains and categories ranked together | 422 |
| `GET /api/chains/search` | `q` (2–80 chars), `limit` (1–25, default 10) | `[ChainSummary]` (chains only; kept for API users) | 422 |
| `GET /api/metros/search` | `q` (2–80 chars), `limit` (1–25) | `[Metro]`: `{metro_code, name, kind, n_open, n_total, bounds: [min_lon, min_lat, max_lon, max_lat]}`, same tiers as chain search, then metros before micros, then size | 422 |
| `GET /api/metros/{metro_code}` | | `Metro` (used to restore `?metro=` links) | 404 |
| `GET /api/chains/{chain_key}/locations` | `include_unverified` (default false), `state` (2 letters), `metro` (5 digits) | `{chain, include_unverified, locations: [Location]}` | 404, 422 |
| `POST /api/targets` | `TargetRequest` | `TargetResponse` | 404 unknown chain, category or metro; 422 |
| `POST /api/targets/export?format=generic\|google_ads_editor` | `TargetRequest` | CSV attachment | 404, 422 |

`ChainSummary = {chain_key, display_name, category, n_open, n_total}`. `SearchResult = {kind, key, display_name, detail, n_open, n_total}`, where `detail` is the chain's main category or the category's parent.

**Search ranking** (`search_chains`):
1. Tier 1: prefix match, *including exact matches*. Exact and prefix share a tier so "orangetheory" ranks Orangetheory Fitness (1,275) above a 3-location chain called "Orangetheory".
2. Tier 2: word-start match.
3. Tier 3: substring match.
4. Tier 4: Jaro-Winkler similarity ≥ 0.88, which catches typos like `starbuks`.

Within a tier, entries with more open locations come first. `/api/search` uses the same tiers over chains and categories together. **Fallback:** if a multi-word query matches nothing as a phrase, it returns entries where any word starts a word, ordered by how many words matched, then by size. Real case: "dog daycare" matched nothing; the fallback finds "Lucy's Doggy Daycare and Spa", Dog trainer, Dog park.

### 3.3 Targets (`targets.py`)

`TargetRequest` (unknown fields are a **422**, so a client still sending a renamed field fails loudly instead of being ignored):

| Field | Type / limits | Default |
|---|---|---|
| `chain_keys` | 1–10 competitor chains (`MAX_CHAINS`), deduplicated in order; **exactly one** of `chain_keys` / `category` | |
| `category` | every business in an Overture category, e.g. `pet_groomer` | |
| `radius_mi` | 1–50 (Meta Ads Manager's US range) | 1 |
| `include_unverified` | bool | false |
| `states` | list of 2-letter codes, upper-cased, deduplicated | all |
| `metros` | up to 10 CBSA codes (5 digits), deduplicated; ANDed with `states` if both are given (the UI sends one or the other) | all |
| `own_chain_key` | your own chain; can't also be in `chain_keys` (422) | none |
| `own_locations` | your stores as `[{name?, lat, lon}]`, ≤ 5,000 (`MAX_UPLOADED_LOCATIONS`); the frontend fills it from an uploaded CSV. Combines with `own_chain_key` | none |
| `own_filter` | `only_near` (keep targets within `own_radius_mi` of yours) or `skip_near` (drop them) | `only_near` |
| `own_radius_mi` | 0.1–50 | 3 for `only_near`, 1 for `skip_near` |
| `campaign_name` | ≤ 200 chars | `Turf - <key>`: the chain key, `<first> and <n> more`, or the category |

**Why `only_near` is the default.** Geo-conquesting works when the competitor's customers could realistically switch: someone leaving a Planet Fitness 40 miles from your nearest gym won't. So the useful list is competitor locations *near* yours. `skip_near` (the original behavior) is still there for when you'd otherwise pay to reach your own customers.

**Algorithm** (one SQL query, plus a count):
1. Candidates are the chains' (or category's) places matching the status, state and metro filters. **Your own chain's places are never candidates** (they can appear in a category). They're counted first: more than `MAX_TARGETS` (**25,000**) returns **422 "Pick a state or metro area…"**. Nearly every chain fits nationwide (Subway, 22,953 open; Dollar General, 20,572); U-Haul (29,531) and broad categories (restaurants, 280k) need a state.
2. "Own" locations are **all** of `own_chain_key`'s places, *including unverified* and **regardless of the state/metro filter** (your store just across the line still makes a nearby competitor "near yours") (a store of yours that may be open still counts), `UNION ALL` the uploaded points, passed to DuckDB as one `STRUCT[]` parameter and `unnest`ed. Uploaded points without a name are labeled `uploaded location`.
3. Each candidate's nearest own location is found by haversine distance (miles), prefiltered to a latitude window of `own_radius_mi / 69 + 0.01` degrees, and kept if ≤ `own_radius_mi`.
4. `only_near`: candidates with a nearest own location are targets (with `nearest_own_location`, `distance_mi`); the rest are dropped as `far_from_yours` (their nearest store isn't computed, by design of the window). `skip_near`: the reverse, dropped as `near_yours` with the distance.

`TargetResponse = {chain_keys, category, metros, radius_mi, own_filter, own_radius_mi, own_location_count, candidates, dropped_count, targets: [Target], dropped: [Dropped]}`. `Target` carries `chain_key` (which competitor) and, when near one of yours, `nearest_own_location` and `distance_mi`. `Dropped = {id, name, address, state, lat, lon, reason, nearest_own_location?, distance_mi?}`. `own_filter` and `own_radius_mi` are `null` when no own locations were given.

**Performance** (`scripts/bench_targets.py`, real DB, API container limits of 2 threads / 1 GB): one chain 18 ms; only-near your chain 39 ms; three chains skip-near 82 ms; worst case, Subway (22,953) against 5,000 uploaded stores at 50 mi, 445 ms.

**CSV formats** (`to_csv`):
- `google_ads_editor`: header `Campaign,Location`; each row is `<campaign>,(<r>mi:<lat 6dp>:<lon 6dp>)`. The proximity format comes from Google Ads Editor's "Add multiple location targets" help page. **Verified manually on 2026-11-18:** a Turf export loaded into Google Ads Editor with every location resolving as a radius target. Editor's *file import* gives "A required account column is missing" if you choose "My data includes account information"; choose "Use selected accounts" instead, or paste through Locations → Make multiple changes. The `Campaign` value must match an existing campaign. Radius is written with `:g`, so `1` stays `1` and `2.5` stays `2.5`.
- `generic`: `name,address,city,state,postcode,latitude,longitude,radius,radius_unit,status,overture_id,nearest_yours,miles_to_yours`. Status is `open` or `unverified`. The last two are filled when the target is within the own radius of one of your locations.

The filename comes from `TargetRequest.target_key` (e.g. `turf-orangetheory-fitness-and-1-more-generic.csv`), reduced to `[a-z0-9-]`, so it's safe in `Content-Disposition`.

**Not done:** merging overlapping circles (see HLD §5).

### 3.4 Frontend and static serving

`create_app()` mounts `StaticFiles(src/turf_app/web, html=True)` at `/` **after** all API routes, so `/api/*` always wins and `/` serves `index.html`. The files ship inside the Python package (hatch includes them in the wheel), so the image needs no separate frontend step.

**Response headers** (same middleware as rate limiting):
- Every response gets `X-Content-Type-Options: nosniff` and `Referrer-Policy: strict-origin-when-cross-origin`.
- Frontend responses also get `Cache-Control: no-cache` (the filenames have no content hash; `StaticFiles` answers revalidation with ETags) and a CSP. `/api/*`, `/docs` and `/openapi.json` don't get either.
- **CSP:** `default-src 'self'`; `script-src 'self' https://static.cloudflareinsights.com` (Cloudflare Web Analytics is on for the zone and injects its beacon; `connect-src` also allows `https://cloudflareinsights.com`). Found by the browser test, since the injection only happens behind Cloudflare; `style-src 'self' 'unsafe-inline'` (MapLibre sets inline styles); `img-src`/`connect-src` add `https://tiles.openfreemap.org`; `worker-src blob:` (MapLibre's worker); `frame-ancestors 'none'`.

**`web/app.js`** (one ES module, no framework):

| Part | What it does |
|---|---|
| `state` | targets (up to 10 chains, or one category), own chain, uploaded store list, own filter + distance, US state **or** metro area (picking one clears the other), radius, include-unverified. Mirrored to the URL by `syncUrl` (`?chain=a&chain=b` or `?cat=`, then `&own=&mode=near|skip&d=&state=&metro=&r=&unverified=1`), restored by `restoreFromUrl`. **Old links** (from before `only_near`) have `own` without `mode` and meant skip-near with the distance in `skip`; they still open that way. The uploaded list isn't in the URL (too big); the UI says so |
| `setupCombo` | accessible combobox (ARIA roles, arrow keys/Enter/Escape) over a `search(q)` function: `/api/search` for competitors and your chain (the target box offers chains and categories until a chain is picked, then only chains), `/api/metros/search` for metros (`pickMetro` frames the metro's bounds when no competitor is picked yet). Chains show their main category ("gym · 1,275 open") to tell similar names apart. 200 ms debounce; out-of-order responses dropped |
| `addTarget`, `renderTargets` | a category replaces any chains (it already covers them); a chain replaces a category; duplicates and your own chain are refused |
| `parseStoreList` | client-side CSV parsing for the upload: comma/semicolon/tab, quoted fields, BOM. Finds `lat/latitude/y` and `lon/lng/long/longitude/x` columns (else the first two numeric columns of a headerless file), a name or address column, skips rows without valid coordinates (and reports how many), swaps columns if every row looks like (lon, lat) in the US, caps at 5,000 rows / 5 MB. Addresses alone are refused: no geocoding |
| `refresh` | `POST /api/targets`, plus `GET /api/chains/{own}/locations?include_unverified=true` when you set your chain (uploaded points are drawn from memory); a request counter drops stale responses |
| map | GeoJSON sources `targets`, `dropped`, `own`. Radius circles are drawn at true scale with a zoom-interpolated `circle-radius` (meters ÷ Web Mercator meters-per-pixel × cos(lat)), so there's no polygon generation even for 17k targets. `updateZoomHint` shows "Zoom in to see each target's ad radius" when the circle would be under 4 px at the map center. The legend buttons (`aria-pressed`) toggle layer groups via `setLayerVisible` |
| lists | targets, and a collapsed "N skipped" list with the reason, each showing the distance to your nearest location when known |
| export | `POST /api/targets/export`, then downloads the blob with the server's filename |
| errors | 429s show the `Retry-After` wait; network and API errors show in the action bar; upload errors under the upload button |

The sidebar list shows the first 300 targets; the map and exports have all of them. The phone layout (≤ 800 px) puts the map on top and the panel below.

### 3.5 Rate limiting (`ratelimit.py`)

An in-memory sliding window per `(client IP, rule)`, per worker. **The first matching rule wins:**

| Rule | Limit |
|---|---|
| `POST /api/targets*` | 30 / 60 s |
| `/api/chains/*` | 120 / 60 s |
| `/api/*` | 300 / 60 s |
| anything else (incl. `/healthz`) | unlimited |

Over the limit returns `429` with `Retry-After` in seconds. Tracked keys are capped at 50,000; past that, all counts are cleared. **Client IP chain:** Cloudflare's `CF-Connecting-IP`, which Caddy trusts only from Cloudflare's ranges, becomes Caddy's `{client_ip}`, which goes to the app as `X-Client-IP`, overwriting any visitor value. **Never** key on `X-Forwarded-For`: Cloudflare keeps a visitor-supplied value in it.

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

- **Container:** `turf`, image from `state/.env`, port `127.0.0.1:8090→8000`. Limits: 4 GB, 2 CPUs, read-only root filesystem, `/tmp` tmpfs, `no-new-privileges`. Data mounted read-only at `/data`. Health check on `/healthz`.
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
| `deploy` | after `image` | SSH as `turfdeploy` with `deploy $GITHUB_SHA` (job token on stdin), then a public smoke test (`/healthz`, `/api/meta` commit equals SHA, search, targets), then `scripts/browser_smoke.py` against the live site in headless Chromium, with screenshots uploaded as a workflow artifact |

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
| New Overture release | run pipeline steps 1–3 (including 1b and 1c) with `--release X`, check `chains_manifest.json` spot checks, then `TURF_HOST=ovh scripts/deploy_data.sh X` |
| Roll back data | `TURF_HOST=ovh scripts/deploy_data.sh --rollback <older release>` |
| Roll back code | re-run the `deploy` job of an older commit in GitHub Actions, or on the server: `sudo -u turfdeploy SSH_ORIGINAL_COMMAND="deploy <sha>" /opt/turf/bin/server-deploy </dev/null` (only works if that image is still on the box or the package is public) |
| Logs | `ssh ovh docker logs --tail 100 turf` |
| Refresh Cloudflare ranges | `TURF_HOST=ovh scripts/update_caddy_cloudflare_ips.sh` |
| Change server setup | edit `deploy/docker-compose.yml` or `scripts/server_deploy.sh`, then `TURF_HOST=ovh scripts/setup_server.sh <ci-key.pub>` |

---

## 5. Testing

- **Unit:** `test_normalize.py`, `test_ratelimit.py` (fake clock).
- **Pipeline:** `test_chains.py` and `test_states.py` build tiny DuckDB tables shaped like real cases and run the real `build()` / `fill_states()`.
- **API:** `conftest.py` builds a serving database **with the real `write_serving_db`**, then `test_api.py` and `test_targets.py` use FastAPI's `TestClient`. Rate limiting is disabled in the shared client and tested separately with small rules.
- **Convention:** a test case taken from real data says so in a comment, quoting the actual row ("Real case: …").
- **Browser:** `scripts/browser_smoke.py` (Playwright, headless Chromium). It fails on any console error, page error, CSP violation or **axe-core accessibility violation** (results page and phone layout, via `axe-playwright-python`), and walks the flow: search, state, radius, zoom hint, a second competitor, your chain in only-near then skip-near mode, layer toggle, both exports (row counts must match the on-screen count), URL restore, an old-style `&skip=` link, a store-list upload, a broad category, a metro area (fewer targets than nationwide, in the URL), phone layout. It runs locally against any base URL, and in CI after every deploy.
- **Not covered by automated tests:** the S3 extract scripts (they need network and run for minutes), the shell deploy scripts beyond syntax and the command check, and the Google Ads Editor import, which was checked by hand once (§3.3) and isn't automated.
