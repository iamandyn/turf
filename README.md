# Turf

> Pick a competitor, get an ad-ready list of target areas around every one of their locations, built from open Overture Maps data.

**Status:** early development.

## Why
_TODO: who this is for and why I chose it._

## How it works
1. **Pipeline** (`scripts/`, logic in `src/turf_app/`): DuckDB reads Overture's GeoParquet for a pinned release, matches places to chains, flags duplicates, fills in states, and builds a read-only `turf.duckdb`.
2. **API** (`src/turf_app/api.py`): FastAPI serves chain search, locations, and target lists with CSV export from that file.
3. **Deploy:** every merge to `main` is tested, built into a Docker image, and deployed by GitHub Actions to https://turf.dunkasaur.gg, with a health check and automatic rollback.
4. **Web** (`web/`): a map frontend (not built yet).

**Developer docs:** [High-level design](documents/HLD.md) · [Low-level design](documents/LLD.md) · [Code guide](documents/CODE_GUIDE.md)

## Development
```bash
uv sync
uv run pytest -q
# Pipeline for one Overture release (see documents/LLD.md §1)
R=2026-11-18.0
uv run python scripts/extract_us.py --release $R
uv run python scripts/extract_states.py --release $R
uv run python scripts/build_chains.py --release $R
uv run python scripts/build_serving_db.py --release $R
TURF_DB=data/$R/turf.duckdb uv run uvicorn turf_app.api:app --reload
```

## Trade-offs and cuts
_TODO_
