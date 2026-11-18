# Turf

> Pick a competitor, get an ad-ready list of target areas around every one of their locations, built from open Overture Maps data.

**Status:** early development.

## Why
_TODO: who this is for and why I chose it._

## How it works
1. **Pipeline** (`scripts/`): Python and DuckDB read Overture's GeoParquet for a pinned release, normalize brands, validate the result, and build `targets.duckdb`.
2. **API** (`src/turf_app/`): FastAPI serves brand search, locations, and target-list export from that file.
3. **Web** (`web/`): a MapLibre frontend for searching, refining, and exporting.

## Development
```bash
uv sync
uv run python scripts/inspect_places.py --release <release>
```

## Trade-offs and cuts
_TODO_
