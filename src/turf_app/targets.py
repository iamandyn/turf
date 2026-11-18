"""Build a target list: a competitor's locations, minus areas near your own, as ad-ready rows.

Export formats:

* ``google_ads_editor``: ``Campaign`` and ``Location`` columns, with radius
  targets written as ``(1mi:30.267153:-97.743057)``, the proximity format in
  Google Ads Editor's "Add multiple location targets" help page.
* ``generic``: one row per target with address, coordinates, and radius, for
  Meta Ads Manager or any other tool. Meta doesn't document a bulk file format
  for radius targets that we could verify, so this is deliberately plain.
"""

from __future__ import annotations

import csv
import io
from enum import StrEnum
from typing import Annotated

import duckdb
from pydantic import BaseModel, Field, field_validator

# Meta Ads Manager allows a 1-50 mile radius around an address in the US;
# staying inside that range keeps exports valid for both Meta and Google.
MIN_RADIUS_MI = 1.0
MAX_RADIUS_MI = 50.0

StateCode = Annotated[str, Field(pattern="^[A-Za-z]{2}$")]


class ExportFormat(StrEnum):
    generic = "generic"
    google_ads_editor = "google_ads_editor"


class TargetRequest(BaseModel):
    chain_key: str = Field(min_length=1, description="Competitor chain to target")
    radius_mi: float = Field(default=1.0, ge=MIN_RADIUS_MI, le=MAX_RADIUS_MI)
    include_unverified: bool = False
    states: list[StateCode] | None = Field(default=None, max_length=51)
    exclude_chain_key: str | None = Field(
        default=None, description="Your own chain: drop targets near your locations"
    )
    exclude_radius_mi: float = Field(default=1.0, ge=0.1, le=MAX_RADIUS_MI)
    campaign_name: str | None = Field(default=None, max_length=200)

    @field_validator("states")
    @classmethod
    def _upper_states(cls, v: list[str] | None) -> list[str] | None:
        return sorted({s.upper() for s in v}) if v else None


class Target(BaseModel):
    id: str
    name: str
    status: str | None
    address: str | None
    locality: str | None
    state: str | None
    postcode: str | None
    lat: float
    lon: float


class Excluded(BaseModel):
    id: str
    name: str
    address: str | None
    state: str | None
    nearest_own_location: str
    distance_mi: float


class TargetResponse(BaseModel):
    chain_key: str
    radius_mi: float
    candidates: int
    excluded_count: int
    targets: list[Target]
    excluded: list[Excluded]


class UnknownChainError(LookupError):
    pass


def _haversine_mi(lat1: str, lon1: str, lat2: str, lon2: str) -> str:
    return f"""(2 * 3958.7613 * asin(sqrt(
        pow(sin(radians({lat2} - {lat1}) / 2), 2)
        + cos(radians({lat1})) * cos(radians({lat2}))
          * pow(sin(radians({lon2} - {lon1}) / 2), 2))))"""


def _check_chain(db: duckdb.DuckDBPyConnection, chain_key: str) -> None:
    if db.execute("SELECT 1 FROM chains WHERE chain_key = ?", [chain_key]).fetchone() is None:
        raise UnknownChainError(chain_key)


def build_targets(db: duckdb.DuckDBPyConnection, req: TargetRequest) -> TargetResponse:
    _check_chain(db, req.chain_key)
    if req.exclude_chain_key:
        _check_chain(db, req.exclude_chain_key)

    params: dict[str, object] = {
        "chain": req.chain_key,
        "unverified": req.include_unverified,
        "states": req.states,
        "own": req.exclude_chain_key,
        "excl_mi": req.exclude_radius_mi,
        # Latitude window for the distance prefilter: 1 degree ~ 69 miles.
        "excl_deg": req.exclude_radius_mi / 69.0 + 0.01,
    }
    dist = _haversine_mi("c.lat", "c.lon", "o.lat", "o.lon")
    rows = db.execute(
        f"""
        WITH candidates AS (
            SELECT id, name, status, address, locality, state, postcode, lat, lon
            FROM places
            WHERE chain_key = $chain
              AND ($unverified OR status = 'open')
              AND ($states IS NULL OR list_contains($states, state))
        ),
        -- Your own locations count even when unverified: better to skip a
        -- target near a store of yours that may be open than to pay to
        -- advertise to its customers.
        own AS (
            SELECT name, address, lat, lon FROM places
            WHERE $own IS NOT NULL AND chain_key = $own
        ),
        nearest AS (
            SELECT c.id,
                   arg_min(coalesce(o.address, o.name), {dist}) AS own_location,
                   min({dist}) AS distance_mi
            FROM candidates c
            JOIN own o ON abs(c.lat - o.lat) < $excl_deg
            GROUP BY c.id
            HAVING min({dist}) <= $excl_mi
        )
        SELECT c.*, n.own_location, n.distance_mi
        FROM candidates c LEFT JOIN nearest n USING (id)
        ORDER BY c.state, c.locality, c.id
        """,
        params,
    ).fetchall()

    target_fields = list(Target.model_fields)
    targets: list[Target] = []
    excluded: list[Excluded] = []
    for row in rows:
        values = dict(zip(target_fields, row[: len(target_fields)], strict=True))
        own_location, distance_mi = row[len(target_fields)], row[len(target_fields) + 1]
        if own_location is None:
            targets.append(Target.model_validate(values))
        else:
            excluded.append(
                Excluded(
                    id=values["id"],
                    name=values["name"],
                    address=values["address"],
                    state=values["state"],
                    nearest_own_location=own_location,
                    distance_mi=round(distance_mi, 2),
                )
            )
    return TargetResponse(
        chain_key=req.chain_key,
        radius_mi=req.radius_mi,
        candidates=len(rows),
        excluded_count=len(excluded),
        targets=targets,
        excluded=excluded,
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
            ]
        )  # fmt: skip
        for t in result.targets:
            writer.writerow(
                [
                    t.name, t.address or "", t.locality or "", t.state or "", t.postcode or "",
                    f"{t.lat:.6f}", f"{t.lon:.6f}", radius, "mi", t.status or "unverified", t.id,
                ]
            )  # fmt: skip
    return buf.getvalue()
