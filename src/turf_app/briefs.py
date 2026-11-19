"""Pre-written creative briefs: one per preset and metro, shipped with the app.

The live demo never calls an LLM, so it costs nothing to run however much it's
used. Briefs were written ahead of time from each metro's actual numbers (the
signals and Census figures the API returns) and are stored in briefs.json; a
production deployment would generate them on demand with an API key, cache
them, and cap daily spend. See the README's "Briefs" decision.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

BRIEFS_FILE = Path(__file__).parent / "briefs.json"


class Brief(BaseModel):
    preset: str
    metro_code: str
    metro_name: str
    headline: str = Field(description="One-line campaign idea for this market")
    audience: str = Field(description="Who to reach here, in a sentence")
    angles: list[str] = Field(description="Two or three creative angles or hooks")
    local_context: list[str] = Field(description="Local details the creative can use")
    grounded_in: list[str] = Field(description="The data points the brief is based on")


def load(path: Path | None = None) -> dict[tuple[str, str], Brief]:
    path = path or BRIEFS_FILE
    if not path.exists():
        return {}
    items = [Brief.model_validate(b) for b in json.loads(path.read_text())]
    return {(b.preset, b.metro_code): b for b in items}
