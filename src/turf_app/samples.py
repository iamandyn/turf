"""Sample brands with SYNTHETIC results, to try the "your results" flow in one click.

The brands are fictional and their numbers are generated, deterministically,
by the code below. Real companies' results by metro aren't public, and putting
invented numbers next to a real brand's name would misrepresent it.

How a sample is generated: the brand "runs" in the largest metros; spend
follows population with noise; the KPI is a base value times
exp(effect x signal strength + noise), where signal strength is the metro's
average signal z-score for the brand's preset. So the signal check *will*
find the relationship that was put in (except for the beauty brand, where
there is none, to show what "no evidence" looks like). That makes
the samples a demo of the flow, not evidence that the signals work; only a
real brand's data can show that. The page says so.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Literal

import duckdb
from pydantic import BaseModel

from turf_app.markets import MarketRequest, build_profiles
from turf_app.performance import ResultRow


@dataclass(frozen=True)
class Sample:
    key: str
    name: str
    description: str
    preset: str
    kpi: Literal["roas", "cpa"]
    geo_type: Literal["zip", "metro"]
    base: float  # typical ROAS or CPA
    effect: float  # how strongly signal strength drives results (0 = not at all)
    noise: float
    markets: int  # how many of the largest metros it runs in
    budget: float  # total monthly spend
    seed: int


SAMPLES: dict[str, Sample] = {
    s.key: s
    for s in (
        Sample("pets", "Sample pet-care app", "Dog walking and sitting marketplace",
               "pets", "cpa", "metro", 38.0, 0.45, 0.25, 45, 250_000, 11),
        Sample("fitness", "Sample fitness app", "Home and studio workout subscription",
               "fitness", "cpa", "zip", 24.0, 0.35, 0.25, 40, 180_000, 12),
        Sample("home_services", "Sample home-services brand", "Home maintenance membership",
               "home_services", "roas", "metro", 2.6, 0.30, 0.30, 35, 140_000, 13),
        Sample("beauty_wellness", "Sample DTC beauty brand", "Supplements and skincare online",
               "beauty_wellness", "roas", "metro", 3.1, 0.0, 0.30, 50, 300_000, 14),
    )
}  # fmt: skip


class SampleInfo(BaseModel):
    key: str
    name: str
    description: str
    preset: str
    kpi: Literal["roas", "cpa"]
    geo_type: Literal["zip", "metro"]
    synthetic: bool = True


class SampleData(SampleInfo):
    rows: list[ResultRow]


def info(sample: Sample) -> SampleInfo:
    return SampleInfo(
        key=sample.key,
        name=sample.name,
        description=sample.description,
        preset=sample.preset,
        kpi=sample.kpi,
        geo_type=sample.geo_type,
    )


def _market_label(name: str) -> str:
    """How ad platforms often label a market: "Austin, TX" (main city, first state)."""
    cities, _, states = name.rpartition(", ")
    return f"{cities.split('-')[0]}, {states.split('-')[0]}"


def generate(db: duckdb.DuckDBPyConnection, sample: Sample) -> SampleData:
    rng = random.Random(sample.seed)
    profiles = build_profiles(db, MarketRequest(preset=sample.preset))
    biggest = sorted(profiles.metros, key=lambda c: -profiles.metros[c][3])[: sample.markets]
    weights = {c: profiles.metros[c][3] ** 0.9 * rng.lognormvariate(0, 0.35) for c in biggest}
    total_w = sum(weights.values())
    rows: list[ResultRow] = []
    for code in biggest:
        spend = sample.budget * weights[code] / total_w
        strength = profiles.strength(code)
        shock = rng.gauss(0, sample.noise)
        if sample.kpi == "roas":
            roas = sample.base * math.exp(sample.effect * strength + shock)
            revenue = spend * roas
            conversions = revenue / 65.0  # average order value
        else:
            cpa = sample.base * math.exp(-sample.effect * strength + shock)
            conversions = spend / cpa
            revenue = 0.0
        if sample.geo_type == "metro":
            rows.append(
                ResultRow(
                    geo=_market_label(profiles.metros[code][1]),
                    spend=round(spend, 2),
                    conversions=round(conversions, 1),
                    revenue=round(revenue, 2) if revenue else None,
                )
            )
            continue
        # ZIP-level report: split the metro's results over its ZIPs by size.
        zips = db.execute(
            "SELECT zip, n_places FROM metro_zips WHERE metro_code = ? ORDER BY zip", [code]
        ).fetchall()
        total_places = sum(n for _, n in zips) or 1
        for zip_code, n in zips:
            share = n / total_places
            rows.append(
                ResultRow(
                    geo=zip_code,
                    spend=round(spend * share, 2),
                    conversions=round(conversions * share, 2),
                    revenue=round(revenue * share, 2) if revenue else None,
                )
            )
    return SampleData(**info(sample).model_dump(), rows=rows)
