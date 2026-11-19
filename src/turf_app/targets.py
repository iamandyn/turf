"""Build a target list: competitors' locations, filtered by distance to yours, as ad-ready rows.

Export formats:

* ``google_ads_editor``: ``Campaign`` and ``Location`` columns, with radius
  targets written as ``(1mi:30.267153:-97.743057)``, the proximity format in
  Google Ads Editor's "Add multiple location targets" help page. Verified by
  hand in Google Ads Editor on 2026-11-18 (locations resolve as radius targets).
* ``generic``: one row per target with address, coordinates, radius, and (when
  you gave your own locations) the nearest one and its distance, for
  Meta Ads Manager or any other tool. Meta doesn't document a bulk file format
  for radius targets that we could verify, so this is deliberately plain.
"""

from __future__ import annotations

import csv
import io
from enum import StrEnum
from typing import Annotated

import duckdb
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Meta Ads Manager allows a 1-50 mile radius around an address in the US;
# staying inside that range keeps exports valid for both Meta and Google.
MIN_RADIUS_MI = 1.0
MAX_RADIUS_MI = 50.0

StateCode = Annotated[str, Field(pattern="^[A-Za-z]{2}$")]


class ExportFormat(StrEnum):
    generic = "generic"
    google_ads_editor = "google_ads_editor"


# Above this, a list is too big to show in a browser or upload as one file;
# the user is asked to pick a state. Nearly every chain fits nationwide (Subway,
# ~23k open); U-Haul (~29.5k) and broad categories (restaurants, 280k) don't.
MAX_TARGETS = 25_000


# Up to this many competitor chains in one list ("Orangetheory + F45 + Barry's").
MAX_CHAINS = 10
# Uploaded store lists: enough for nearly any brand, small enough to send as JSON.
MAX_UPLOADED_LOCATIONS = 5_000


class OwnFilter(StrEnum):
    # Keep competitor locations close to yours: where customers could switch to
    # you. The useful default for geo-conquesting, since a rival 40 miles from
    # your nearest store is a wasted impression.
    only_near = "only_near"
    # Drop competitor locations close to yours, so you don't pay to reach
    # people who are already your customers.
    skip_near = "skip_near"


DEFAULT_OWN_RADIUS_MI = {OwnFilter.only_near: 3.0, OwnFilter.skip_near: 1.0}


class OwnLocation(BaseModel):
    """One of your stores, from an uploaded list (lat/lon; geocoding is out of scope)."""

    name: str | None = Field(default=None, max_length=200)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class TargetRequest(BaseModel):
    # Unknown fields are errors, so a client still sending a renamed field
    # (e.g. the old exclude_chain_key) fails loudly instead of being ignored.
    model_config = ConfigDict(extra="forbid")

    chain_keys: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_CHAINS,
        description="Competitor chains to target",
    )
    category: str | None = Field(
        default=None,
        min_length=1,
        description="Or: every business in an Overture category, e.g. pet_groomer",
    )
    radius_mi: float = Field(default=1.0, ge=MIN_RADIUS_MI, le=MAX_RADIUS_MI)
    include_unverified: bool = False
    states: list[StateCode] | None = Field(default=None, max_length=51)
    own_chain_key: str | None = Field(default=None, description="Your own chain in Overture")
    own_locations: list[OwnLocation] | None = Field(
        default=None,
        max_length=MAX_UPLOADED_LOCATIONS,
        description="Your own locations, e.g. from an uploaded store list",
    )
    own_filter: OwnFilter = OwnFilter.only_near
    own_radius_mi: float | None = Field(
        default=None,
        ge=0.1,
        le=MAX_RADIUS_MI,
        description="Miles from your locations; default 3 (only_near) or 1 (skip_near)",
    )
    campaign_name: str | None = Field(default=None, max_length=200)

    @field_validator("chain_keys")
    @classmethod
    def _dedupe_chains(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if any(not k for k in v):
            raise ValueError("chain keys can't be empty")
        return list(dict.fromkeys(v))

    @field_validator("states")
    @classmethod
    def _upper_states(cls, v: list[str] | None) -> list[str] | None:
        return sorted({s.upper() for s in v}) if v else None

    @model_validator(mode="after")
    def _check(self) -> TargetRequest:
        if (self.chain_keys is None) == (self.category is None):
            raise ValueError("Give exactly one of chain_keys or category")
        if self.own_chain_key and self.chain_keys and self.own_chain_key in self.chain_keys:
            raise ValueError("Your own chain can't also be a competitor")
        if self.own_locations == []:
            self.own_locations = None
        return self

    @property
    def has_own(self) -> bool:
        return bool(self.own_chain_key or self.own_locations)

    @property
    def effective_own_radius_mi(self) -> float:
        return self.own_radius_mi or DEFAULT_OWN_RADIUS_MI[self.own_filter]

    @property
    def target_key(self) -> str:
        """Short name for filenames and the default campaign name."""
        if self.category:
            return self.category
        keys = self.chain_keys or []
        return keys[0] if len(keys) == 1 else f"{keys[0]} and {len(keys) - 1} more"


class Target(BaseModel):
    id: str
    name: str
    chain_key: str | None
    status: str | None
    address: str | None
    locality: str | None
    state: str | None
    postcode: str | None
    lat: float
    lon: float
    # Set when you gave your own locations and the target is within the own
    # radius of one (always, for only_near).
    nearest_own_location: str | None = None
    distance_mi: float | None = None


class DropReason(StrEnum):
    near_yours = "near_yours"  # skip_near: too close to one of your locations
    far_from_yours = "far_from_yours"  # only_near: no location of yours nearby


class Dropped(BaseModel):
    id: str
    name: str
    address: str | None
    state: str | None
    lat: float
    lon: float
    reason: DropReason
    # Only for near_yours; a far location's nearest store isn't computed.
    nearest_own_location: str | None = None
    distance_mi: float | None = None


class TargetResponse(BaseModel):
    chain_keys: list[str] | None
    category: str | None
    radius_mi: float
    own_filter: OwnFilter | None
    own_radius_mi: float | None
    own_location_count: int
    candidates: int
    dropped_count: int
    targets: list[Target]
    dropped: list[Dropped]


class UnknownChainError(LookupError):
    pass


class UnknownCategoryError(LookupError):
    pass


class TooManyTargetsError(ValueError):
    def __init__(self, count: int) -> None:
        super().__init__(
            f"That's {count:,} locations, more than the {MAX_TARGETS:,} limit for one list. "
            "Pick a state to narrow it down."
        )
        self.count = count


def _haversine_mi(lat1: str, lon1: str, lat2: str, lon2: str) -> str:
    return f"""(2 * 3958.7613 * asin(sqrt(
        pow(sin(radians({lat2} - {lat1}) / 2), 2)
        + cos(radians({lat1})) * cos(radians({lat2}))
          * pow(sin(radians({lon2} - {lon1}) / 2), 2))))"""


def _check_chain(db: duckdb.DuckDBPyConnection, chain_key: str) -> None:
    if db.execute("SELECT 1 FROM chains WHERE chain_key = ?", [chain_key]).fetchone() is None:
        raise UnknownChainError(chain_key)


def _check_category(db: duckdb.DuckDBPyConnection, category: str) -> None:
    row = db.execute("SELECT 1 FROM categories WHERE category_key = ?", [category]).fetchone()
    if row is None:
        raise UnknownCategoryError(category)


# Candidate filter shared by the size check and the main query. Your own
# chain's locations are never targets (they can turn up in a category).
_CANDIDATE_FILTER = """
    ($chains IS NULL OR list_contains($chains, chain_key))
    AND ($category IS NULL OR category = $category)
    AND ($unverified OR status = 'open')
    AND ($states IS NULL OR list_contains($states, state))
    AND ($own IS NULL OR chain_key IS DISTINCT FROM $own)
"""


def _own_locations_sql(req: TargetRequest) -> str:
    parts = []
    if req.own_chain_key:
        # Your own locations count even when unverified: a store of yours that
        # may be open is still a reason to keep (or skip) a nearby target.
        parts.append(
            "SELECT coalesce(address, name) AS label, lat, lon FROM places WHERE chain_key = $own"
        )
    if req.own_locations:
        parts.append(
            "SELECT coalesce(u.name, 'uploaded location') AS label, u.lat, u.lon "
            "FROM (SELECT unnest($uploaded) AS u)"
        )
    return " UNION ALL ".join(parts)


def build_targets(db: duckdb.DuckDBPyConnection, req: TargetRequest) -> TargetResponse:
    for key in req.chain_keys or []:
        _check_chain(db, key)
    if req.category:
        _check_category(db, req.category)
    if req.own_chain_key:
        _check_chain(db, req.own_chain_key)

    filters: dict[str, object] = {
        "chains": req.chain_keys,
        "category": req.category,
        "unverified": req.include_unverified,
        "states": req.states,
        "own": req.own_chain_key,
    }
    count_row = db.execute(
        f"SELECT count(*) FROM places WHERE {_CANDIDATE_FILTER}", filters
    ).fetchone()
    count = int(count_row[0]) if count_row else 0
    if count > MAX_TARGETS:
        raise TooManyTargetsError(count)

    target_fields = [
        f for f in Target.model_fields if f not in ("nearest_own_location", "distance_mi")
    ]
    columns = ", ".join(target_fields)
    own_radius = req.effective_own_radius_mi
    own_count = 0
    if not req.has_own:
        rows = db.execute(
            f"SELECT {columns}, NULL, NULL FROM places WHERE {_CANDIDATE_FILTER} "
            "ORDER BY state, locality, id",
            filters,
        ).fetchall()
    else:
        params: dict[str, object] = {
            **filters,
            "own_mi": own_radius,
            # Latitude window for the distance prefilter: 1 degree ~ 69 miles.
            "own_deg": own_radius / 69.0 + 0.01,
        }
        if req.own_locations:
            params["uploaded"] = [loc.model_dump() for loc in req.own_locations]
        dist = _haversine_mi("c.lat", "c.lon", "o.lat", "o.lon")
        own_sql = _own_locations_sql(req)
        # DuckDB rejects named parameters a query doesn't use.
        own_params = {
            k: params[k] for k in ("own", "uploaded") if k in params and f"${k}" in own_sql
        }
        count_own = db.execute(f"SELECT count(*) FROM ({own_sql})", own_params).fetchone()
        own_count = int(count_own[0]) if count_own else 0
        rows = db.execute(
            f"""
            WITH candidates AS (
                SELECT {columns} FROM places WHERE {_CANDIDATE_FILTER}
            ),
            own AS ({own_sql}),
            nearest AS (
                SELECT c.id,
                       arg_min(o.label, {dist}) AS own_location,
                       min({dist}) AS distance_mi
                FROM candidates c
                JOIN own o ON abs(c.lat - o.lat) < $own_deg
                GROUP BY c.id
                HAVING min({dist}) <= $own_mi
            )
            SELECT c.*, n.own_location, n.distance_mi
            FROM candidates c LEFT JOIN nearest n USING (id)
            ORDER BY c.state, c.locality, c.id
            """,
            params,
        ).fetchall()

    n = len(target_fields)
    targets: list[Target] = []
    dropped: list[Dropped] = []
    for row in rows:
        values = dict(zip(target_fields, row[:n], strict=True))
        own_location, distance_mi = row[n], row[n + 1]
        near = own_location is not None
        if near:
            values["nearest_own_location"] = own_location
            values["distance_mi"] = round(distance_mi, 2)
        keep = (
            not req.has_own
            or (req.own_filter is OwnFilter.only_near and near)
            or (req.own_filter is OwnFilter.skip_near and not near)
        )
        if keep:
            targets.append(Target.model_validate(values))
            continue
        dropped.append(
            Dropped(
                id=values["id"],
                name=values["name"],
                address=values["address"],
                state=values["state"],
                lat=values["lat"],
                lon=values["lon"],
                reason=DropReason.near_yours if near else DropReason.far_from_yours,
                nearest_own_location=values.get("nearest_own_location"),
                distance_mi=values.get("distance_mi"),
            )
        )
    return TargetResponse(
        chain_keys=req.chain_keys,
        category=req.category,
        radius_mi=req.radius_mi,
        own_filter=req.own_filter if req.has_own else None,
        own_radius_mi=own_radius if req.has_own else None,
        own_location_count=own_count,
        candidates=len(rows),
        dropped_count=len(dropped),
        targets=targets,
        dropped=dropped,
    )


def _radius_text(radius_mi: float) -> str:
    # 1.0 -> "1", 2.5 -> "2.5"
    return f"{radius_mi:g}"


def to_csv(result: TargetResponse, fmt: ExportFormat, campaign_name: str) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    radius = _radius_text(result.radius_mi)
    if fmt is ExportFormat.google_ads_editor:
        writer.writerow(["Campaign", "Location"])
        for t in result.targets:
            writer.writerow([campaign_name, f"({radius}mi:{t.lat:.6f}:{t.lon:.6f})"])
    else:
        writer.writerow(
            [
                "name", "address", "city", "state", "postcode",
                "latitude", "longitude", "radius", "radius_unit", "status", "overture_id",
                "nearest_yours", "miles_to_yours",
            ]
        )  # fmt: skip
        for t in result.targets:
            writer.writerow(
                [
                    t.name, t.address or "", t.locality or "", t.state or "", t.postcode or "",
                    f"{t.lat:.6f}", f"{t.lon:.6f}", radius, "mi", t.status or "unverified", t.id,
                    t.nearest_own_location or "",
                    "" if t.distance_mi is None else f"{t.distance_mi:g}",
                ]
            )  # fmt: skip
    return buf.getvalue()
