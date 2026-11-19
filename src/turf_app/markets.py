"""Market selection: rank US metro areas for a consumer brand's ad spend.

A brand (say, a pet-care app) picks *signals*: Overture place categories whose
density hints at its audience (pet groomers, vets, dog parks). Each metro gets
a profile from those signals, and metros are ranked one of two ways:

* **Lookalikes** (with seed metros, i.e. the brand's best markets today):
  closest profile to the seeds' average. This is the main use: "we win in
  Denver and Austin; where else looks like that?"
* **Signal strength** (no seeds): highest average signal.

Signals are each category's **share of the metro's open places**, not a rate
per resident: Overture's coverage varies a lot between metros (2,100 to 6,200
open places per 100k residents), and a per-capita rate would partly measure how
well a city is mapped. Shares cancel that out. Population is still used for
the size filter and the optional growth signal (Census, 2020-2025).

Shares are log-scaled and standardized (z-scores) across the metros being
ranked, so every signal counts equally. This is a proxy for audience presence,
not a measured one: the product says so, and the way to validate it is to
compare the ranking with the brand's own results by metro.

Pure Python on purpose: a few hundred metros x a few signals, and the API
image doesn't ship numpy.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any

import duckdb
from pydantic import BaseModel, Field, field_validator, model_validator

MAX_SIGNALS = 20
MAX_SEEDS = 10
MAX_RESULTS = 100
# Below this many metros there's too little spread to standardize against.
MIN_UNIVERSE = 10
GROWTH_SIGNAL = "population_growth"


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    example: str
    categories: tuple[str, ...]


# Client types matching Valid's verticals (valid.co: health & fitness, home
# services, DTC, consumer apps). Categories are Overture keys; each must exist
# in the data (checked at startup).
PRESETS: dict[str, Preset] = {
    p.key: p
    for p in (
        Preset(
            "pets",
            "Pet care",
            "a dog-walking or pet-sitting app",
            (
                "pet_groomer", "pet_store", "veterinarian", "pet_boarding",
                "dog_park", "dog_trainer", "dog_walker",
            ),
        ),
        Preset(
            "fitness",
            "Health & fitness",
            "a fitness or workout app",
            (
                "gym", "yoga_studio", "pilates_studio", "fitness_trainer",
                "martial_arts_club", "boxing_class",
            ),
        ),
        Preset(
            "home_services",
            "Home services",
            "a home maintenance subscription",
            (
                "hvac_service", "plumbing", "roofing", "home_cleaning",
                "home_improvement_store", "hardware_store",
            ),
        ),
        Preset(
            "beauty_wellness",
            "Beauty & wellness",
            "a DTC supplement or beauty brand",
            (
                "nail_salon", "hair_salon", "spa", "medical_spa",
                "vitamin_and_supplement_store", "health_food_store",
            ),
        ),
        Preset(
            "young_professionals",
            "Young professionals",
            "a fintech or productivity app",
            (
                "coffee_shop", "coworking_space", "smoothie_juice_bar",
                "college_university", "bar",
            ),
        ),
        Preset(
            "families",
            "Families with kids",
            "a parenting or kids' learning app",
            (
                "child_care_and_day_care", "pediatric_clinic", "pediatric_dentistry",
                "childrens_clothing_store", "gymnastics_center",
            ),
        ),
    )
}  # fmt: skip


class MarketRequest(BaseModel):
    model_config = {"extra": "forbid"}

    preset: str | None = Field(default=None, description="A preset key from GET /api/presets")
    categories: list[str] | None = Field(
        default=None, max_length=MAX_SIGNALS, description="Overture categories used as signals"
    )
    use_growth: bool = Field(
        default=False, description="Add population growth 2020-2025 (Census) as a signal"
    )
    seeds: list[str] = Field(
        default_factory=list,
        max_length=MAX_SEEDS,
        description="Census metro codes of the brand's best markets (empty: rank by signals)",
    )
    min_population: int = Field(default=250_000, ge=0, le=5_000_000)
    include_micro: bool = Field(default=False, description="Include micropolitan areas")
    limit: int = Field(default=25, ge=1, le=MAX_RESULTS)

    @field_validator("seeds")
    @classmethod
    def _seed_codes(cls, v: list[str]) -> list[str]:
        if any(not (len(c) == 5 and c.isdigit()) for c in v):
            raise ValueError("Seeds are 5-digit Census metro codes, e.g. 12420")
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _signals(self) -> MarketRequest:
        if self.preset is not None and self.preset not in PRESETS:
            raise ValueError(f"Unknown preset: {self.preset}")
        if self.categories is not None:
            self.categories = list(dict.fromkeys(self.categories))
        if not self.signal_categories and not self.use_growth:
            raise ValueError("Pick a preset, some categories, or population growth")
        return self

    @property
    def signal_categories(self) -> list[str]:
        if self.categories is not None:
            return self.categories
        return list(PRESETS[self.preset].categories) if self.preset else []


class Reason(BaseModel):
    signal: str  # category key, or "population_growth"
    label: str
    ratio: float  # this metro's value / the typical (median) metro's
    value: float  # share of open places in %, or growth in %
    typical: float


class MarketResult(BaseModel):
    rank: int
    metro_code: str
    name: str
    kind: str
    population: int
    growth_pct: float
    score: float  # mean z (signal strength), or distance to the seeds (lookalikes)
    match: int | None  # lookalikes: 0-100, higher is closer to the seeds
    reasons: list[Reason]  # every signal, strongest first
    center: tuple[float, float]  # lon, lat
    bounds: tuple[float, float, float, float]  # min_lon, min_lat, max_lon, max_lat
    zip_count: int


class MarketResponse(BaseModel):
    mode: str  # "lookalike" or "signal_strength"
    signals: list[str]
    universe: int  # metros compared
    seeds: list[MarketResult]
    results: list[MarketResult]


class UnknownCategoriesError(LookupError):
    pass


class UnknownSeedsError(LookupError):
    pass


class UniverseTooSmallError(ValueError):
    pass


def label_for(category: str) -> str:
    return category.replace("_", " ")


def _zscores(values: dict[str, float]) -> dict[str, float]:
    mean = statistics.fmean(values.values())
    sd = statistics.pstdev(values.values()) or 1.0
    return {k: (v - mean) / sd for k, v in values.items()}


@dataclass
class Profiles:
    """Every compared metro's signal values: raw (for display) and standardized."""

    metros: dict[str, tuple[Any, ...]]  # metro_code -> row (see _METRO_SQL)
    signals: list[str]
    raw: dict[str, dict[str, float]]  # signal -> metro -> share of places (%) or growth (%)
    z: dict[str, dict[str, float]]  # signal -> metro -> z-score across the compared metros
    typical: dict[str, float]  # signal -> median raw value

    def strength(self, code: str, weights: dict[str, float] | None = None) -> float:
        w = weights or {}
        total = sum(w.get(s, 1.0) for s in self.signals) or 1.0
        return sum(w.get(s, 1.0) * self.z[s][code] for s in self.signals) / total

    def reasons(self, code: str) -> list[Reason]:
        out = []
        for signal in self.signals:
            value, med = self.raw[signal][code], self.typical[signal]
            if signal == GROWTH_SIGNAL:
                ratio = (100 + value) / (100 + med)  # growth: compare as multipliers
            else:
                ratio = value / med if med else 0.0
            out.append(
                Reason(
                    signal=signal,
                    label=signal_label(signal),
                    ratio=round(ratio, 2),
                    value=round(value, 3),
                    typical=round(med, 3),
                )
            )
        return sorted(out, key=lambda r: self.z[r.signal][code], reverse=True)

    def result(self, code: str, rank: int, score: float, match: int | None) -> MarketResult:
        r = self.metros[code]
        return MarketResult(
            rank=rank,
            metro_code=code,
            name=r[1],
            kind=r[2],
            population=r[3],
            growth_pct=float(r[4]),
            score=round(score, 3),
            match=match,
            reasons=self.reasons(code),
            center=(r[6], r[7]),
            bounds=(r[8], r[9], r[10], r[11]),
            zip_count=r[12],
        )


def signal_label(signal: str) -> str:
    return "population growth 2020-25" if signal == GROWTH_SIGNAL else label_for(signal)


def build_profiles(
    db: duckdb.DuckDBPyConnection, req: MarketRequest, extra_metros: list[str] | None = None
) -> Profiles:
    """Profiles for the metros ``req`` compares, plus its seeds and ``extra_metros``
    (e.g. every metro a brand already runs in), whatever their size."""
    categories = req.signal_categories
    if categories:
        rows = db.execute(
            "SELECT category_key FROM categories WHERE list_contains(?, category_key)",
            [categories],
        ).fetchall()
        missing = sorted(set(categories) - {r[0] for r in rows})
        if missing:
            raise UnknownCategoriesError(", ".join(missing))

    kinds = ["metro", "micro"] if req.include_micro else ["metro"]
    always = list(dict.fromkeys([*req.seeds, *(extra_metros or [])]))
    metro_rows = db.execute(
        """
        SELECT m.metro_code, m.name, m.kind, m.population, m.growth_pct, m.n_open,
               m.center_lon, m.center_lat, m.min_lon, m.min_lat, m.max_lon, m.max_lat,
               (SELECT count(*) FROM metro_zips z WHERE z.metro_code = m.metro_code)
        FROM metros m
        WHERE (list_contains($kinds, m.kind) AND m.population >= $min_pop)
           OR list_contains($always, m.metro_code)
        ORDER BY m.metro_code
        """,
        {"kinds": kinds, "min_pop": req.min_population, "always": always},
    ).fetchall()
    metros = {r[0]: r for r in metro_rows}
    unknown = sorted(set(req.seeds) - set(metros))
    if unknown:
        raise UnknownSeedsError(", ".join(unknown))
    if len(metros) < MIN_UNIVERSE:
        raise UniverseTooSmallError(
            f"Only {len(metros)} metro areas match; lower the minimum population."
        )

    # Share of each metro's open places, per signal category (zero if none).
    counts: dict[tuple[str, str], int] = {}
    if categories:
        for code, cat, n in db.execute(
            """
            SELECT metro_code, category, n_open FROM metro_category
            WHERE list_contains(?, category) AND list_contains(?, metro_code)
            """,
            [categories, list(metros)],
        ).fetchall():
            counts[(code, cat)] = n

    raw: dict[str, dict[str, float]] = {}
    for cat in categories:
        raw[cat] = {code: 100.0 * counts.get((code, cat), 0) / r[5] for code, r in metros.items()}
    if req.use_growth:
        raw[GROWTH_SIGNAL] = {code: float(r[4]) for code, r in metros.items()}

    # Standardize: shares are log-scaled first (they're skewed: a few metros
    # have several times the typical share); growth is already a percentage.
    z: dict[str, dict[str, float]] = {}
    for signal, values in raw.items():
        scaled = (
            values
            if signal == GROWTH_SIGNAL
            else {k: math.log1p(100.0 * v) for k, v in values.items()}  # per 10k places
        )
        z[signal] = _zscores(scaled)
    typical = {signal: statistics.median(values.values()) for signal, values in raw.items()}
    return Profiles(metros=metros, signals=list(raw), raw=raw, z=z, typical=typical)


def rank_markets(
    db: duckdb.DuckDBPyConnection,
    req: MarketRequest,
    weights: dict[str, float] | None = None,
    exclude: set[str] | None = None,
    profiles: Profiles | None = None,
) -> MarketResponse:
    """Rank metros for ``req``. ``weights`` (signal -> weight, default 1) scale
    each signal's part in distance and strength; ``exclude`` drops metros from
    the results (e.g. where the brand already spends)."""
    prof = profiles or build_profiles(db, req)
    signals = prof.signals
    w = {s: (weights or {}).get(s, 1.0) for s in signals}
    total_w = sum(w.values()) or 1.0
    candidates = [c for c in prof.metros if c not in req.seeds and c not in (exclude or set())]
    if req.seeds:
        centroid = {s: statistics.fmean(prof.z[s][c] for c in req.seeds) for s in signals}

        def distance(code: str) -> float:
            return math.sqrt(
                sum(w[s] * (prof.z[s][code] - centroid[s]) ** 2 for s in signals) / total_w
            )

        ordered = sorted(candidates, key=lambda c: (distance(c), c))
        results = [
            prof.result(c, i + 1, distance(c), round(100 * math.exp(-distance(c))))
            for i, c in enumerate(ordered[: req.limit])
        ]
        mode = "lookalike"
    else:
        ordered = sorted(candidates, key=lambda c: (-prof.strength(c, w), c))
        results = [
            prof.result(c, i + 1, prof.strength(c, w), None)
            for i, c in enumerate(ordered[: req.limit])
        ]
        mode = "signal_strength"

    return MarketResponse(
        mode=mode,
        signals=signals,
        universe=len(prof.metros),
        seeds=[prof.result(c, 0, prof.strength(c, w), None) for c in req.seeds],
        results=results,
    )


def zip_rows(db: duckdb.DuckDBPyConnection, codes: list[str]) -> list[tuple[str, str, int]]:
    """(metro_code, zip, n_places) for the given metros, in the given order."""
    rows = db.execute(
        "SELECT metro_code, zip, n_places FROM metro_zips WHERE list_contains(?, metro_code)",
        [codes],
    ).fetchall()
    order = {c: i for i, c in enumerate(codes)}
    return sorted(((str(a), str(b), int(n)) for a, b, n in rows), key=lambda r: (order[r[0]], r[1]))
