"""Time build_targets on the real serving database for typical and worst-case requests.

Usage:
    uv run python scripts/bench_targets.py --db data/2026-11-18.0/turf.duckdb
"""

from __future__ import annotations

import argparse
import random
import time
from pathlib import Path

import duckdb

from turf_app.targets import (
    MAX_UPLOADED_LOCATIONS,
    OwnFilter,
    OwnLocation,
    TargetRequest,
    build_targets,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    # Same limits as the API container (see create_app).
    con = duckdb.connect(str(args.db), read_only=True, config={"memory_limit": "1GB", "threads": 2})

    rng = random.Random(0)
    uploaded = [
        OwnLocation(name=f"store {i}", lat=rng.uniform(25, 48), lon=rng.uniform(-123, -70))
        for i in range(MAX_UPLOADED_LOCATIONS)
    ]
    cases: dict[str, TargetRequest] = {
        "one chain, no own": TargetRequest(chain_keys=["planet fitness"]),
        "only near own chain, 3 mi": TargetRequest(
            chain_keys=["planet fitness"], own_chain_key="orangetheory fitness"
        ),
        "3 chains, skip near own chain": TargetRequest(
            chain_keys=["orangetheory fitness", "f45 training", "anytime fitness"],
            own_chain_key="planet fitness",
            own_filter=OwnFilter.skip_near,
        ),
        "Subway vs 5k uploaded, 50 mi (worst case)": TargetRequest(
            chain_keys=["subway"], own_locations=uploaded, own_radius_mi=50
        ),
        "Subway vs 5k uploaded, 3 mi": TargetRequest(chain_keys=["subway"], own_locations=uploaded),
    }
    for label, req in cases.items():
        times = []
        for _ in range(args.repeat):
            start = time.perf_counter()
            res = build_targets(con.cursor(), req)
            times.append(time.perf_counter() - start)
        print(
            f"{label:45s} best {min(times) * 1000:7.0f} ms  "
            f"targets {len(res.targets):6,}  dropped {res.dropped_count:6,}"
        )


if __name__ == "__main__":
    main()
