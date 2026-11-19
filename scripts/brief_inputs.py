"""Dump the data each pre-written brief is grounded in (see briefs.py).

Briefs are written for what the report shows: each sample brand's top
recommendations. For each (preset, metro) pair this prints the metro's figures,
every signal vs. the typical metro, and the categories the metro is most
over-represented in overall (local colour the brief may use). The briefs
themselves are written from it into scripts/assemble_briefs.py.

Usage:
    uv run python scripts/brief_inputs.py \\
        --db data/2026-11-18.0/turf.duckdb --out data/brief_inputs.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

from turf_app.markets import PRESETS
from turf_app.performance import PerformanceRequest, analyze
from turf_app.samples import SAMPLES, generate

TOP_PER_SAMPLE = 5
# Over-representation is only meaningful for categories common enough to compare.
MIN_US_PLACES = 2000


def distinctive(db: duckdb.DuckDBPyConnection, code: str, n: int = 6) -> list[str]:
    rows = db.execute(
        f"""
        WITH share AS (
            SELECT mc.metro_code, mc.category, mc.n_open::DOUBLE / m.n_open AS s
            FROM metro_category mc JOIN metros m USING (metro_code)
            WHERE m.kind = 'metro' AND m.population >= 250000
        ),
        typical AS (SELECT category, median(s) AS med FROM share GROUP BY 1)
        SELECT s.category, s.s / t.med AS ratio, mc.n_open
        FROM share s JOIN typical t USING (category)
        JOIN categories c ON c.category_key = s.category
        JOIN metro_category mc ON mc.metro_code = s.metro_code AND mc.category = s.category
        WHERE s.metro_code = ? AND c.n_open >= {MIN_US_PLACES} AND mc.n_open >= 10
        ORDER BY ratio DESC LIMIT ?
        """,
        [code, n],
    ).fetchall()
    return [
        f"{cat.replace('_', ' ')} {ratio:.1f}x typical ({n_open} places)"
        for cat, ratio, n_open in rows
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    db = duckdb.connect(str(args.db), read_only=True)

    pairs: dict[tuple[str, str], dict[str, object]] = {}

    def add(preset: str, result: object) -> None:
        for r in result.results:  # type: ignore[attr-defined]
            key = (preset, r.metro_code)
            if key in pairs:
                continue
            pairs[key] = {
                "preset": preset,
                "preset_label": PRESETS[preset].label,
                "example": PRESETS[preset].example,
                "metro_code": r.metro_code,
                "metro_name": r.name,
                "population": r.population,
                "growth_2020_25_pct": r.growth_pct,
                "signals": [
                    f"{x.label}: {x.ratio:g}x typical share ({x.value}% vs {x.typical}%)"
                    for x in r.reasons
                ],
                "distinctive_overall": distinctive(db, r.metro_code),
            }

    for sample in SAMPLES.values():
        data = generate(db, sample)
        result = analyze(
            db,
            PerformanceRequest(
                rows=data.rows, geo_type=data.geo_type, kpi=data.kpi, preset=data.preset,
                limit=TOP_PER_SAMPLE,
            ),
        )  # fmt: skip
        add(sample.preset, result.recommendations)
    args.out.write_text(json.dumps(list(pairs.values()), indent=1))
    print(f"{len(pairs)} preset x metro pairs -> {args.out}")


if __name__ == "__main__":
    main()
