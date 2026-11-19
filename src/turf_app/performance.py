"""Recommendations from a brand's own results.

A brand uploads its ad results by geography (an ad-platform export: ZIP codes
or market names, with spend and conversions or revenue). Turf:

1. **matches** each row to a metro (ZIPs through the metro_zips table; names
   like "Austin, TX", "Austin TX" or "Denver-Aurora, CO" through each metro's
   principal cities and states; 5-digit CBSA codes directly) and totals the
   results per metro;
2. picks the **best markets** by the KPI (ROAS: revenue / spend, higher is
   better; CPA: spend / conversion, lower is better), among metros with enough
   spend to trust (``min_spend_share`` of the total);
3. **checks the signals against the results**: across the brand's metros, how
   each signal's rank tracks the KPI's rank (Spearman), and how well signals
   *predict a metro they weren't fitted on* (leave-one-out: weights from the
   other metros, score this one, correlate the scores with the real KPI).
   That number is biased upward (weights keep only helpful signals, so even
   shuffled results score around +0.3), so the verdict comes from a
   **permutation test**: the same procedure on 999 shufflings of the results
   gives the chance level and a p-value. Signals that don't help get no weight;
4. **recommends** lookalikes of the best markets, weighted by what predicted
   this brand's results, among metros where it spends little or nothing.

Nothing is stored: the rows arrive with the request and are gone after it.
"""

from __future__ import annotations

import math
import random
import re
import statistics
from dataclasses import dataclass
from typing import Literal

import duckdb
from pydantic import BaseModel, Field, model_validator

from turf_app.markets import (
    GROWTH_SIGNAL,
    MarketRequest,
    MarketResponse,
    MarketResult,
    build_profiles,
    rank_markets,
    signal_label,
)
from turf_app.normalize import normalize_text

MAX_ROWS = 20_000
MIN_METROS_FOR_CHECK = 6
SIMILAR_MARKETS = 3
PERMUTATIONS = 999
# The KPI's per-metro value is only trusted with at least this share of spend.
DEFAULT_MIN_SPEND_SHARE = 0.01
# A metro where the brand already spends at least this share counts as "active"
# and isn't recommended (it's already in the plan).
ACTIVE_SPEND_SHARE = 0.005


class ResultRow(BaseModel):
    geo: str = Field(min_length=1, max_length=120, description="ZIP, metro name, or CBSA code")
    spend: float = Field(ge=0)
    conversions: float | None = Field(default=None, ge=0)
    revenue: float | None = Field(default=None, ge=0)


class PerformanceRequest(BaseModel):
    model_config = {"extra": "forbid"}

    rows: list[ResultRow] = Field(min_length=1, max_length=MAX_ROWS)
    geo_type: Literal["zip", "metro"] = "metro"
    kpi: Literal["roas", "cpa"] = "roas"
    preset: str | None = None
    categories: list[str] | None = None
    use_growth: bool = False
    best_count: int = Field(
        default=5, ge=1, le=10, description="How many best markets to seed with"
    )
    min_spend_share: float = Field(default=DEFAULT_MIN_SPEND_SHARE, ge=0, le=0.5)
    min_population: int = Field(default=250_000, ge=0, le=5_000_000)
    include_micro: bool = False
    limit: int = Field(default=25, ge=1, le=100)

    @model_validator(mode="after")
    def _kpi_data(self) -> PerformanceRequest:
        field = "revenue" if self.kpi == "roas" else "conversions"
        if not any(getattr(r, field) for r in self.rows):
            raise ValueError(f"The {self.kpi.upper()} KPI needs a {field} column with values")
        return self

    def market_request(self, seeds: list[str]) -> MarketRequest:
        return MarketRequest(
            preset=self.preset,
            categories=self.categories,
            use_growth=self.use_growth,
            seeds=seeds,
            min_population=self.min_population,
            include_micro=self.include_micro,
            limit=self.limit,
        )


class MetroResult(BaseModel):
    metro_code: str
    name: str
    spend: float
    conversions: float
    revenue: float
    kpi: float | None  # None when the KPI can't be computed (no spend or conversions)
    trusted: bool  # enough spend to use its KPI
    best: bool  # one of the best markets (seeds)
    rows: int
    center: tuple[float, float]  # lon, lat, for the map


class SignalFit(BaseModel):
    signal: str
    label: str
    rho: float  # Spearman: + means more of this signal goes with better results
    weight: float  # used for recommendations (0 = ignored)
    best_ratio: float  # median, over the best markets, of value / typical metro


class SignalCheck(BaseModel):
    metros: int  # trusted metros the check used
    signals: list[SignalFit]
    predictive_rho: float | None  # leave-one-out; None when there are too few metros
    chance_rho: float | None  # 95th percentile of the same statistic on shuffled results
    p_value: float | None  # share of shufflings scoring at least predictive_rho
    verdict: Literal["strong", "moderate", "weak", "too_few"]
    summary: str


class SimilarMarket(BaseModel):
    metro_code: str
    name: str
    kpi: float


class RecommendationDetail(BaseModel):
    metro_code: str
    status: Literal["not_running", "low_spend"]
    spend: float
    # The brand's most similar trusted metros (same weighted signal distance as
    # the ranking) and their actual KPI: the data behind "expected".
    similar: list[SimilarMarket]
    expected_kpi: float | None  # median KPI of ``similar``
    expected_range: tuple[float, float] | None  # min and max KPI of ``similar``
    snapshot: str  # the metro in a sentence or two, from the data


class PerformanceResponse(BaseModel):
    kpi: Literal["roas", "cpa"]
    rows: int
    matched_rows: int
    unmatched_examples: list[str]
    total_spend: float
    metros: list[MetroResult]  # best KPI first; untrusted last
    check: SignalCheck
    recommendations: MarketResponse
    details: list[RecommendationDetail]  # per recommended metro, same order


class NoUsableMetrosError(ValueError):
    pass


# ---------- Matching ----------

_ZIP = re.compile(r"^\d{5}")


@dataclass(frozen=True)
class _MetroName:
    code: str
    cities: tuple[str, ...]  # normalized principal cities, e.g. ("austin", "round rock")
    states: tuple[str, ...]  # e.g. ("tx",)
    full: str  # normalized full name


def _metro_names(db: duckdb.DuckDBPyConnection) -> list[_MetroName]:
    out = []
    for code, name in db.execute("SELECT metro_code, name FROM metros").fetchall():
        cities_part, _, states_part = name.rpartition(", ")
        out.append(
            _MetroName(
                code=code,
                cities=tuple(normalize_text(c) for c in cities_part.split("-")),
                states=tuple(normalize_text(st) for st in states_part.split("-")),
                full=normalize_text(name),
            )
        )
    return out


def _match_name(text: str, names: list[_MetroName], codes: set[str]) -> str | None:
    raw = text.strip()
    if raw.isdigit() and raw in codes:
        return raw  # a CBSA code
    norm = normalize_text(raw)
    for m in names:
        if norm == m.full:
            return m.code
    # "Austin, TX", "Austin TX", "Austin-Round Rock, TX", "Denver" (any state)
    state = None
    city_part = raw
    if "," in raw:
        city_part, _, state_text = raw.rpartition(",")
        state = normalize_text(state_text).split(" ")[0] or None
    else:
        tokens = norm.split(" ")
        if len(tokens) > 1 and len(tokens[-1]) == 2:
            state, city_part = tokens[-1], " ".join(tokens[:-1])
    city = normalize_text(city_part.split("-")[0])
    hits = [m for m in names if city in m.cities and (state is None or state in m.states)]
    if not hits:
        return None
    # Prefer metros where it's the first-named (main) city, then metros over micros.
    hits.sort(key=lambda m: (m.cities[0] != city, m.code))
    return hits[0].code


def match_rows(
    db: duckdb.DuckDBPyConnection, rows: list[ResultRow], geo_type: str
) -> list[str | None]:
    """The metro code for each row, or None."""
    if geo_type == "zip":
        zips = {
            z: code for code, z in db.execute("SELECT metro_code, zip FROM metro_zips").fetchall()
        }
        out: list[str | None] = []
        for r in rows:
            m = _ZIP.match(r.geo.strip())
            out.append(zips.get(m.group(0)) if m else None)
        return out
    names = _metro_names(db)
    codes = {n.code for n in names}
    cache: dict[str, str | None] = {}
    result = []
    for r in rows:
        if r.geo not in cache:
            cache[r.geo] = _match_name(r.geo, names, codes)
        result.append(cache[r.geo])
    return result


# ---------- Statistics ----------


def _ranks(values: list[float]) -> list[float]:
    """Average ranks (ties share the mean rank), 1-based."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(x: list[float], y: list[float]) -> float:
    if len(x) < 3:
        return 0.0
    rx, ry = _ranks(x), _ranks(y)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return cov / (sx * sy) if sx and sy else 0.0


def _fit_weights(z: dict[str, list[float]], goodness: list[float]) -> dict[str, float]:
    """Weight = positive Spearman with the results; signals that don't help get 0."""
    return {s: max(0.0, spearman(values, goodness)) for s, values in z.items()}


def _pearson_from_sums(n: int, sx: float, sy: float, sxx: float, syy: float, sxy: float) -> float:
    den = (n * sxx - sx * sx) * (n * syy - sy * sy)
    return (n * sxy - sx * sy) / math.sqrt(den) if den > 0 else 0.0


def _leave_one_out(
    rank_x: dict[str, list[float]], z: dict[str, list[float]], ry: list[float]
) -> float:
    """Out-of-sample rank correlation of signal scores with the results.

    For each metro, weights come from the other metros (rank correlation of
    each signal with the results, negatives dropped), and the metro is scored
    with them. Fold correlations use the full-sample ranks with the held-out
    metro's terms subtracted, so each fold is O(1) per signal; that's what
    makes 999 permutations affordable.
    """
    n = len(ry)
    sy, syy = sum(ry), sum(v * v for v in ry)
    sums = {
        s: (sum(rx), sum(v * v for v in rx), sum(a * b for a, b in zip(rx, ry, strict=True)))
        for s, rx in rank_x.items()
    }
    scores = []
    for i in range(n):
        w = {}
        for s, rx in rank_x.items():
            sx, sxx, sxy = sums[s]
            w[s] = max(
                0.0,
                _pearson_from_sums(
                    n - 1, sx - rx[i], sy - ry[i], sxx - rx[i] ** 2, syy - ry[i] ** 2,
                    sxy - rx[i] * ry[i],
                ),
            )  # fmt: skip
        total = sum(w.values())
        scores.append(sum(w[s] * z[s][i] for s in z) / total if total else 0.0)
    return spearman(scores, ry)


def check_signals(
    z: dict[str, list[float]], goodness: list[float], permutations: int = PERMUTATIONS
) -> tuple[float, float, float]:
    """(out-of-sample rho, chance level (95th percentile under shuffling), p-value)."""
    rank_x = {s: _ranks(v) for s, v in z.items()}
    ry = _ranks(goodness)
    observed = _leave_one_out(rank_x, z, ry)
    rng = random.Random(0)  # deterministic: the same data gets the same verdict
    shuffled = ry[:]
    null = []
    for _ in range(permutations):
        rng.shuffle(shuffled)
        null.append(_leave_one_out(rank_x, z, shuffled))
    null.sort()
    chance = null[int(0.95 * len(null)) - 1]
    p_value = (1 + sum(1 for v in null if v >= observed)) / (1 + len(null))
    return observed, chance, p_value


def _verdict(
    rho: float | None, chance: float | None, p: float | None, n: int
) -> tuple[Literal["strong", "moderate", "weak", "too_few"], str]:
    if rho is None or chance is None or p is None:
        return "too_few", (
            f"Only {n} metros have enough spend to check the signals against; at least "
            f"{MIN_METROS_FOR_CHECK} are needed. Recommendations use the signals unweighted."
        )
    stats = (
        f"out-of-sample correlation {rho:+.2f}; chance alone gives up to {chance:+.2f}, p = {p:.2f}"
    )
    if p < 0.05:
        return "strong", (
            f"The signals predict this brand's results better than chance ({stats}): "
            "metros that look like the best ones tend to perform like them."
        )
    # With no real relationship, p is uniform: each cut-off is its false-alarm rate.
    if p < 0.10:
        return "moderate", (
            f"Some evidence the signals predict this brand's results ({stats}). "
            "Treat recommendations as a shortlist to test."
        )
    return "weak", (
        f"No evidence the signals predict this brand's results ({stats}). Other factors drive "
        "performance; recommendations use the signals unweighted and are exploratory."
    )


def _compact(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1_000_000 else f"{round(n / 1000)}K"


def snapshot(r: MarketResult) -> str:
    """A metro in plain words, from its numbers (no AI model involved)."""
    g = r.growth_pct
    if g >= 8:
        growth = f"growing fast (+{g:.1f}% since 2020)"
    elif g >= 3:
        growth = f"growing (+{g:.1f}% since 2020)"
    elif g <= -1:
        growth = f"shrinking ({g:.1f}% since 2020)"
    else:
        growth = f"stable ({g:+.1f}% since 2020)"
    parts = [f"{_compact(r.population)} people, {growth}."]
    reasons = [x for x in r.reasons if x.signal != GROWTH_SIGNAL]
    high = [x for x in reasons if x.ratio >= 1.2][:2]
    if high:
        listed = " and ".join(f"{x.label} at {x.ratio:g}x" for x in high)
        parts.append(f"Above typical: {listed} the typical share of local businesses.")
    low = [x for x in reasons if x.ratio < 0.7][:1]
    if low:
        parts.append(f"Below typical: {low[0].label} ({low[0].ratio:g}x).")
    return " ".join(parts)


# ---------- Analysis ----------


def analyze(db: duckdb.DuckDBPyConnection, req: PerformanceRequest) -> PerformanceResponse:
    matched = match_rows(db, req.rows, req.geo_type)
    totals: dict[str, list[float]] = {}  # code -> [spend, conversions, revenue, rows]
    unmatched: list[str] = []
    for row, code in zip(req.rows, matched, strict=True):
        if code is None:
            if row.geo not in unmatched and len(unmatched) < 10:
                unmatched.append(row.geo)
            continue
        t = totals.setdefault(code, [0.0, 0.0, 0.0, 0])
        t[0] += row.spend
        t[1] += row.conversions or 0.0
        t[2] += row.revenue or 0.0
        t[3] += 1
    if not totals:
        raise NoUsableMetrosError(
            "No rows matched a US metro area. Check the location column "
            '(ZIP codes, or names like "Austin, TX").'
        )
    total_spend = sum(t[0] for t in totals.values())

    def kpi(t: list[float]) -> float | None:
        spend, conversions, revenue, _ = t
        if req.kpi == "roas":
            return revenue / spend if spend else None
        return spend / conversions if conversions else None

    min_spend = req.min_spend_share * total_spend
    trusted = {
        c for c, t in totals.items() if t[0] > 0 and t[0] >= min_spend and kpi(t) is not None
    }
    if not trusted:
        raise NoUsableMetrosError("No metro has enough spend and results to compute the KPI.")

    def goodness(code: str) -> float:
        value = kpi(totals[code])
        assert value is not None
        return value if req.kpi == "roas" else -value  # CPA: lower is better

    ranked = sorted(trusted, key=lambda c: (-goodness(c), c))
    seeds = ranked[: req.best_count]

    # Profiles cover every metro the brand runs in, whatever its size.
    market_req = req.market_request(seeds)
    profiles = build_profiles(db, market_req, extra_metros=list(totals))

    # Every matched metro is in the profiles (extra_metros), so it has a name.
    names = {code: row[1] for code, row in profiles.metros.items()}

    # Signal check over trusted metros.
    check_codes = [c for c in ranked if c in profiles.metros]
    good = [goodness(c) for c in check_codes]
    zs = {s: [profiles.z[s][c] for c in check_codes] for s in profiles.signals}
    weights = _fit_weights(zs, good)
    predictive = chance = p_value = None
    if len(check_codes) >= MIN_METROS_FOR_CHECK:
        rho, chance_level, p = check_signals(zs, good)
        predictive, chance, p_value = round(rho, 3), round(chance_level, 3), round(p, 3)
    verdict, summary = _verdict(predictive, chance, p_value, len(check_codes))
    use_weights = weights if verdict in ("strong", "moderate") and any(weights.values()) else None

    active = {c for c, t in totals.items() if t[0] >= ACTIVE_SPEND_SHARE * total_spend}
    recs = rank_markets(db, market_req, weights=use_weights, exclude=active, profiles=profiles)
    # The data behind each recommendation: the brand's most similar markets.
    dist_w = use_weights or {sg: 1.0 for sg in profiles.signals}
    total_dw = sum(dist_w.get(sg, 0.0) for sg in profiles.signals) or 1.0

    def distance(a: str, b: str) -> float:
        return math.sqrt(
            sum(dist_w.get(sg, 0.0) * (profiles.z[sg][a] - profiles.z[sg][b]) ** 2
                for sg in profiles.signals) / total_dw
        )  # fmt: skip

    def detail(r: MarketResult) -> RecommendationDetail:
        nearest = sorted(check_codes, key=lambda c: (distance(r.metro_code, c), c))
        similar = []
        for c in nearest[:SIMILAR_MARKETS]:
            value = kpi(totals[c])
            assert value is not None  # trusted metros have a KPI
            similar.append(SimilarMarket(metro_code=c, name=names[c], kpi=round(value, 3)))
        values = [m.kpi for m in similar]
        return RecommendationDetail(
            metro_code=r.metro_code,
            status="low_spend" if r.metro_code in totals else "not_running",
            spend=round(totals.get(r.metro_code, [0.0])[0], 2),
            similar=similar,
            expected_kpi=round(statistics.median(values), 3) if values else None,
            expected_range=(min(values), max(values)) if values else None,
            snapshot=snapshot(r),
        )

    details = [detail(r) for r in recs.results]

    def best_ratio(signal: str) -> float:
        """How far above (or below) typical this signal is in the best markets."""
        typical = profiles.typical[signal]
        values = []
        for c in seeds:
            value = profiles.raw[signal][c]
            if signal == GROWTH_SIGNAL:
                values.append((100 + value) / (100 + typical))
            else:
                values.append(value / typical if typical else 0.0)
        return statistics.median(values) if values else 1.0

    def metro_result(code: str) -> MetroResult:
        t = totals[code]
        value = kpi(t)
        return MetroResult(
            metro_code=code,
            name=names[code],
            spend=round(t[0], 2),
            conversions=round(t[1], 2),
            revenue=round(t[2], 2),
            kpi=None if value is None else round(value, 3),
            trusted=code in trusted,
            best=code in seeds,
            rows=int(t[3]),
            center=(profiles.metros[code][6], profiles.metros[code][7]),
        )

    untrusted = sorted((c for c in totals if c not in trusted), key=lambda c: -totals[c][0])
    return PerformanceResponse(
        kpi=req.kpi,
        rows=len(req.rows),
        matched_rows=sum(1 for c in matched if c),
        unmatched_examples=unmatched,
        total_spend=round(total_spend, 2),
        metros=[metro_result(c) for c in [*ranked, *untrusted]],
        check=SignalCheck(
            metros=len(check_codes),
            signals=sorted(
                (
                    SignalFit(
                        signal=s,
                        label=signal_label(s),
                        rho=round(spearman(zs[s], good), 3),
                        weight=round(weights[s], 3),
                        best_ratio=round(best_ratio(s), 2),
                    )
                    for s in profiles.signals
                ),
                key=lambda f: -f.rho,
            ),
            predictive_rho=predictive,
            chance_rho=chance,
            p_value=p_value,
            verdict=verdict,
            summary=summary,
        ),
        recommendations=recs,
        details=details,
    )
