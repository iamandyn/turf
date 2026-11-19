# Turf

> **Pick a competitor, get an ad-ready list of target areas around every one of their locations, built from open Overture Maps data.**

**Live:** https://turf.dunkasaur.gg

![Orangetheory Fitness studios in Texas, skipping those within 1 mile of a Planet Fitness](documents/img/turf-results.png)

**How to use it:**
1. Search for a competitor chain (or a whole category, like pet boarding).
2. Optionally narrow it to a state, set the ad radius, and name your own chain so Turf skips targets near your locations.
3. Export a **Google Ads Editor** CSV (proximity targets) or a plain CSV for Meta and other tools.

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
- **Radius is limited to 1–50 mi**, Meta's US range, so one export works on both platforms.
- **Overlapping circles aren't merged.** Both platforms accept overlaps, and merged circles get bigger and less precise.
- **One list is capped at 25,000 targets.** Nearly every chain fits nationwide (Subway: 22,953); U-Haul (29,531) and broad categories ask for a state.

**Engineering choices:**
- **Precompute offline, serve a read-only DuckDB file.** Queries take milliseconds, and there's no database server to run. Refreshing the data means rerunning the pipeline and uploading the file (about 5 minutes).
- **No-build frontend.** Plain JavaScript and MapLibre, served by the same container, so there's one deploy.
- **CI/CD:** every merge to `main` runs lint, strict typing, and 115 tests. It then builds an immutable Docker image and deploys it over SSH with a key that can *only* run `deploy <sha>`. The server health-checks the new version and rolls back automatically if it fails. CI then drives the live site in a real browser.

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
uv run python scripts/build_chains.py --release $R
uv run python scripts/build_serving_db.py --release $R
TURF_DB=data/$R/turf.duckdb uv run uvicorn turf_app.api:app --reload
```
