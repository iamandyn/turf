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
| 2 | `scripts/build_chains.py` | steps 1, 1b | `places.parquet`, `chains.parquet`, `chains_manifest.json` | ~3 min / 8.6 GB |
| 3 | `scripts/build_serving_db.py` | step 2 | `turf.duckdb` (1.32 GB) | 5 s |
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

### 1.3 Step 2: chains, duplicates, states (`build_chains.py` → `chains.build()`, `states.fill_states()`)

See §2 for the rules. Output `places.parquet` columns:
`id, name, brand_name, chain_key, chain_match ('brand'|'name'|NULL), basic_category, taxonomy_primary, confidence, operating_status, address, locality, state, postcode, lat, lon, source_datasets, is_duplicate`.

`chains.parquet`: `chain_key, n_tagged, name_agreement, display_name, categories (list, by share)`.

`chains_manifest.json` records totals, the state-fill report, and spot-check counts for four known chains. **Compare it between releases**: large swings in the spot checks mean a rule broke or the upstream data changed.

### 1.4 Step 3: serving database (`serving.write_serving_db()`)

The file is written to `turf.duckdb.building`, then renamed, so a reader never sees a half-written file.

| Table | Contents |
|---|---|
| `places` | Non-duplicate rows only: `id, name, chain_key, basic_category, confidence, status, address, locality, state, postcode, lat, lon`. **Sorted by `chain_key, state`**, so DuckDB's per-block min/max statistics skip everything except the requested chain |
| `chains` | `chain_key, display_name, categories, n_total, n_open`; only chains with at least 1 open location |
| `meta` | `release, schema_version (1), built_at_utc` |

Result: 18,226,778 places and 40,026 chains.

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
| `GET /api/chains/search` | `q` (2–80 chars), `limit` (1–25, default 10) | `[ChainSummary]` | 422 |
| `GET /api/chains/{chain_key}/locations` | `include_unverified` (default false), `state` (2 letters) | `{chain, include_unverified, locations: [Location]}` | 404, 422 |
| `POST /api/targets` | `TargetRequest` | `TargetResponse` | 404 unknown chain, 422 |
| `POST /api/targets/export?format=generic\|google_ads_editor` | `TargetRequest` | CSV attachment | 404, 422 |

`ChainSummary = {chain_key, display_name, category, n_open, n_total}`.

**Search ranking** (`search_chains`):
1. Tier 1: prefix match, *including exact matches*. Exact and prefix share a tier so "orangetheory" ranks Orangetheory Fitness (1,275) above a 3-location chain called "Orangetheory".
2. Tier 2: word-start match.
3. Tier 3: substring match.
4. Tier 4: Jaro-Winkler similarity ≥ 0.88, which catches typos like `starbuks`.

Within a tier, chains with more open locations come first.

### 3.3 Targets (`targets.py`)

`TargetRequest`:

| Field | Type / limits | Default |
|---|---|---|
| `chain_key` | required | |
| `radius_mi` | 1–50 (Meta Ads Manager's US range) | 1 |
| `include_unverified` | bool | false |
| `states` | list of 2-letter codes, upper-cased, deduplicated | all |
| `exclude_chain_key` | your own chain | none |
| `exclude_radius_mi` | 0.1–50 | 1 |
| `campaign_name` | ≤ 200 chars | `Turf - <chain_key>` |

**Algorithm** (one SQL query):
1. Candidates are the chain's places matching the status and state filters.
2. "Own" locations are **all** of `exclude_chain_key`'s places, *including unverified*: better to skip a target near a store of yours that may be open.
3. Each candidate's nearest own location is found by haversine distance (miles), prefiltered to a latitude window of `exclude_radius_mi / 69 + 0.01` degrees.
4. A candidate is excluded if that distance ≤ `exclude_radius_mi`.

`TargetResponse = {chain_key, radius_mi, candidates, excluded_count, targets: [Target], excluded: [{id, name, address, state, nearest_own_location, distance_mi}]}`.

**CSV formats** (`to_csv`):
- `google_ads_editor`: header `Campaign,Location`; each row is `<campaign>,(<r>mi:<lat 6dp>:<lon 6dp>)`. The proximity format comes from Google Ads Editor's "Add multiple location targets" help page. The `Campaign` column name is inferred from Editor conventions, not stated on that page. Radius is written with `:g`, so `1` stays `1` and `2.5` stays `2.5`.
- `generic`: `name,address,city,state,postcode,latitude,longitude,radius,radius_unit,status,overture_id`. Status is `open` or `unverified`.

The filename comes from `chain_key`, which only contains `[a-z0-9 ]`, so it's safe in `Content-Disposition`.

**Not done:** merging overlapping circles (see HLD §5).

### 3.4 Rate limiting (`ratelimit.py`)

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
| `deploy` | after `image` | SSH as `turfdeploy` with `deploy $GITHUB_SHA` (job token on stdin), then a public smoke test: `/healthz`, `/api/meta` commit equals SHA, search, targets |

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
| New Overture release | run pipeline steps 1–3 with `--release X`, check `chains_manifest.json` spot checks, then `TURF_HOST=ovh scripts/deploy_data.sh X` |
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
- **Not covered by automated tests:** the S3 extract scripts (they need network and run for minutes), the shell deploy scripts beyond syntax and the command check, and actually importing an export into Google Ads Editor.
