# Turf

> **Geo-conquesting target lists for media buyers: pick a client's competitors, get ad-ready radius targets around their locations near the client's stores, built from open Overture Maps data.**

**Live:** https://turf.dunkasaur.gg

![Orangetheory Fitness studios in Texas, skipping those within 1 mile of a Planet Fitness](documents/img/turf-results.png)

## Who it's for

**Agency media buyers and local marketers running location-based campaigns for several brands.** Each client has a different competitive set (a boutique gym competes with Orangetheory, F45 *and* Barry's), so building the list is a per-client, per-campaign job, and it's usually done by hand: searching Google Maps and pasting coordinates into a spreadsheet. Turf turns that into a minute: pick the competitors (or a whole category, for industries made of independents, like pet boarding), add the client's stores, and export.

An in-house marketer at a single brand would set Turf up once; for them the value is the list itself, and keeping it current as competitors open new locations.

**What the ad platforms already do.** Google Ads can build *location groups* from "common chain businesses", but its documentation frames them around your own locations or stores that sell your products, and doesn't say which chains are available or whether a competitor's chain can be selected. Meta has no chain targeting at all. Turf works the same for any platform that takes coordinates and a radius, covers categories and independents, and filters by distance to the client's own stores, which neither platform does.

**How to use it:**
1. Search for competitor chains (up to 10 in one list), or a whole category, like pet boarding.
2. Optionally narrow it to states and metro areas (e.g. "Texas + Oklahoma City metro") and set the ad radius.
3. Optionally add your own locations: pick your chain, upload your store list (CSV with latitude/longitude), or both. By default Turf keeps only competitor locations **near yours**, where customers could switch to you; or flip it to **skip** those near yours so you don't pay to reach your own customers.
4. Next month, tick **"Only locations added since the last data"** to export just the competitor locations that weren't in last month's list (and see which were removed), to update a running campaign instead of rebuilding it.
5. Export a **Google Ads Editor** CSV (proximity targets, checked by importing into Editor) or a plain CSV for Meta and other tools.

Every view is a shareable link.

## Why this

<!-- Draft: rewrite in your own words. -->
Geo-conquesting, which means showing ads to people near a competitor's locations, is a standard local-advertising tactic. Today, building the location list usually means searching Google Maps and copying coordinates into a spreadsheet by hand. Overture is unusually good at exactly what that list needs: where businesses are, what they are, and which brand they belong to, for the whole US, free.

I first considered an "opportunity score" (demand vs. competition by neighborhood). I dropped it because Overture has no demographics, income, or foot traffic, so the score would have been a guess I couldn't validate. Turf only claims what the data supports.

## Trade-offs and cuts

**Data quality: the real work.** The interesting problems were in Overture's place data, not the UI:
- **Brand tags were missing on many real chain locations and wrong on some.** One brand was attached to dozens of unrelated individual beauty professionals. Places join a chain by brand *or* name, and each chain's brand tags are trusted only if at least 30% of its tagged places also carry its name.
- **Duplicates.** The same store often appears twice from different sources ("9500 S IH 35" vs. "9500 S Interstate 35", 518 m apart). They're flagged by matching street number and street name within 1 km. The rules are kept conservative on purpose: about 1–2% of duplicates remain, because merging two real downtown Starbucks is worse.
- **"Open" means recently seen.** 18% of chain locations have no recent activity signal in Overture; they explain most of the over-counting (Starbucks: 19,330 listed, 17,522 confirmed open). Turf shows confirmed-open locations by default, with a toggle for the rest, labeled as unverified in exports.

**Product choices:**
- **"Only near mine" is the default.** I first built "skip near yours" (don't pay to reach your own customers), then reversed it: in geo-conquesting the valuable targets are competitor locations *close* to yours, where switching is realistic. A Planet Fitness 40 miles from your nearest gym is a wasted impression. Skip-near is still one click away.
- **Metro areas come from the Census, not Overture.** Buyers plan by metro, and Overture has no metro boundaries, so Turf uses the Census Bureau's official metro/micro areas (pinned file), tagged to places by location.
- **"Added since last month", not "opened".** Comparing Overture releases naively is mostly noise: by ID alone, 155k chain locations were "new" in one month. Turf matches by same chain within 150 m or the same address within 2 km, and drops chains whose grouping flipped upstream (all of Jiffy Lube vanished; "Sprint" reappeared). Even then, most of what remains is Overture's coverage growing (Subway: +9% in a month), so Turf presents it as campaign list maintenance, not market intelligence.
- **Store lists need coordinates.** Many brands aren't clean chains in Overture, so you can upload your own list, but Turf doesn't geocode addresses: that needs a paid service and would make results depend on its accuracy.
- **Radius is limited to 1–50 mi**, Meta's US range, so one export works on both platforms.
- **Overlapping circles aren't merged.** Both platforms accept overlaps, and merged circles get bigger and less precise.
- **One list is capped at 25,000 targets.** Nearly every chain fits nationwide (Subway: 22,953); U-Haul (29,531) and broad categories ask for a state.

**Engineering choices:**
- **Precompute offline, serve a read-only DuckDB file.** Queries take milliseconds, and there's no database server to run. Refreshing the data means rerunning the pipeline and uploading the file (about 5 minutes).
- **No-build frontend.** Plain JavaScript and MapLibre, served by the same container, so there's one deploy.
- **CI/CD:** every merge to `main` runs lint, strict typing, and 149 tests. It then builds an immutable Docker image and deploys it over SSH with a key that can *only* run `deploy <sha>`. The server health-checks the new version and rolls back automatically if it fails. CI then drives the live site in a real browser, including an automated accessibility check (axe-core).

**Not built (yet):**
- Live ad-platform API integration.
- Countries other than the US.
- Search synonyms (e.g. "dog daycare" vs. Overture's "pet boarding").
- Real demographic scoring.
- Verified Meta bulk-upload format: Meta doesn't publish one for radius targets, so the generic CSV stays deliberately plain.

## Repo

- **How it works:** a Python + DuckDB pipeline (`scripts/`, logic in `src/turf_app/`) turns a pinned Overture release into `turf.duckdb`. FastAPI (`src/turf_app/api.py`) serves search, targets, and exports, plus the page (`src/turf_app/web/`).
- **Developer docs:** [High-level design](documents/HLD.md) · [Low-level design](documents/LLD.md) · [Code guide](documents/CODE_GUIDE.md)

```bash
uv sync && uv run pytest -q
# Rebuild the data for one Overture release (about 5 minutes; see documents/LLD.md §1)
R=2026-11-18.0
uv run python scripts/extract_us.py --release $R
uv run python scripts/extract_states.py --release $R
uv run python scripts/extract_metros.py --release $R   # Census metro areas (not in Overture)
uv run python scripts/build_chains.py --release $R
# Optional: the "added since last month" view (needs build_chains.py for the previous release too)
uv run python scripts/build_changes.py --release $R --previous 2026-10-21.0
uv run python scripts/build_serving_db.py --release $R --previous 2026-10-21.0
TURF_DB=data/$R/turf.duckdb uv run uvicorn turf_app.api:app --reload
```
