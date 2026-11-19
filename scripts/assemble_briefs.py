"""Assemble src/turf_app/briefs.json: the pre-written creative briefs.

The briefs cover what the report shows: each sample brand's top five
recommendations. The prose (headline, audience, angles, local context) below
was written once, by Claude in a Claude Code session on 2026-11-19, from each
metro's numbers as dumped by brief_inputs.py: its signals vs. the typical metro, Census figures,
and the categories it's most over-represented in. Local context uses those
data points plus widely known facts about each place; data quirks (e.g. a
category 180x typical) were left out. The "grounded in" lines are generated
here from the data, never typed, so they can't drift from it.

Writing them ahead of time keeps the live demo free to run (no LLM calls).
A production setup would generate briefs on demand with an API key, cache them
per preset and metro, and cap daily spend.

Usage:
    uv run python scripts/brief_inputs.py \\
        --db data/2026-11-18.0/turf.duckdb --out data/brief_inputs.json
    uv run python scripts/assemble_briefs.py --inputs data/brief_inputs.json
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from turf_app.briefs import BRIEFS_FILE, Brief

# (preset, metro code) -> (headline, audience, angles, local context)
TEXTS: dict[tuple[str, str], tuple[str, str, list[str], list[str]]] = {
    # ---- Sample pet-care app ----
    ("pets", "15380"): (
        "Buffalo dogs need care through the long winter",
        "Dog owners in a big, settled metro who board their dogs when they travel.",
        ["Boarding for winter getaways", "Walkers for snow days when you can't get home"],
        [
            "Pet boarding at 1.6x and dog walkers at 1.9x the typical share",
            "Lake Erie shoreline and marinas",
        ],
    ),
    ("pets", "19780"): (
        "Des Moines dogs, well looked after",
        "Growing suburban households (+6.7% since 2020) who want reliable care close by.",
        ["Boarding and training near home", "Walks for long office days downtown"],
        [
            "Dog parks at 1.6x and walkers at 2.1x the typical share",
            "Insurance and agriculture jobs",
        ],
    ),
    ("pets", "34940"): (
        "Naples dogs, cared for in season and out",
        "Seasonal and year-round residents in a fast-growing coastal metro (+10.6% since 2020).",
        ["Boarding while you're up north", "Walkers who know beach-day routines"],
        [
            "Dog walkers at 2x and pet boarding at 1.4x the typical share",
            "Boat tours, beaches and vacation homes",
        ],
    ),
    ("pets", "14500"): (
        "Boulder's dogs live an active lifestyle. Keep up.",
        "Health-minded professionals who treat their dogs like training partners.",
        ["Walks and runs while you're at the lab", "Boarding for ski weekends"],
        ["Dog walkers at 3.2x the typical share", "Research labs, wellness and the Flatirons"],
    ),
    ("pets", "49340"): (
        "Worcester dog owners plan ahead",
        "Commuting households in central Massachusetts who rely on boarding and training.",
        ["Boarding for Boston-commute days", "Training for the new family dog"],
        [
            "Pet boarding at 1.9x and dog parks at 1.4x the typical share",
            "Biotech and labs along the I-90 corridor",
        ],
    ),
    # ---- Sample fitness app ----
    ("fitness", "22660"): (
        "Fort Collins trains for the mountains",
        "Active adults near the Front Range who mix trail time with studio classes.",
        ["Strength and mobility for hikers and climbers", "Yoga and pilates in one plan"],
        [
            "Yoga at 1.7x and pilates at 1.8x the typical share",
            "Next to Rocky Mountain National Park",
        ],
    ),
    ("fitness", "46060"): (
        "Tucson's pilates boom",
        "Adults in a sunny desert metro who train indoors through the heat.",
        ["Pilates and core at home on hot afternoons", "Early-morning plans before the heat"],
        ["Pilates studios at 2.2x the typical share", "Mountains and national parks all around"],
    ),
    ("fitness", "35300"): (
        "New Haven stretches between classes and labs",
        "Students, researchers and young professionals in a university city.",
        ["Yoga for study and lab schedules", "Trainer-guided plans in your pocket"],
        ["Yoga studios at 1.7x the typical share", "A university town with biotech labs"],
    ),
    ("fitness", "37100"): (
        "Ventura County boxes and stretches",
        "Coastal suburban adults who already book boxing and pilates classes.",
        ["Boxing workouts at home", "Pilates for beach-ready strength"],
        ["Pilates at 2.1x and boxing classes at 1.8x the typical share", "Beaches and wineries"],
    ),
    ("fitness", "25540"): (
        "Hartford's yoga commuters",
        "Insurance-industry professionals and families in central Connecticut.",
        ["Yoga for the commute home", "Short trainer-led workouts before work"],
        [
            "Yoga at 1.8x and fitness trainers at 1.5x the typical share",
            "Nature reserves and the Connecticut River",
        ],
    ),
    # ---- Sample home-services brand ----
    ("home_services", "35380"): (
        "New Orleans homes face heat, humidity and storms",
        "Homeowners with older houses who need dependable upkeep.",
        ["Humidity-proof AC care", "Storm-season checks on a schedule"],
        [
            "Home-service businesses at about the typical share",
            "Historic homes, Creole food and river life",
        ],
    ),
    ("home_services", "14260"): (
        "Boise's new homeowners need a plan",
        "Families moving into one of the fastest-growing metros (+12.2% since 2020).",
        ["One plan for the first years in a new home", "Plumbing check-ups before winter"],
        [
            "Plumbing at 1.4x the typical share",
            "Wineries and outdoor living around the Treasure Valley",
        ],
    ),
    ("home_services", "22180"): (
        "Fayetteville homes, kept ready for every move",
        "Military families who move often and need homes maintained quickly.",
        ["Move-in and move-out cleaning", "Maintenance while you're deployed"],
        ["Home cleaning at 1.7x the typical share", "A large military community"],
    ),
    ("home_services", "12260"): (
        "Augusta homes, ready for southern summers",
        "Homeowners in a growing metro (+4.7% since 2020) on the Georgia-South Carolina line.",
        ["AC care before the heat", "Cleaning on a schedule"],
        ["Home cleaning at 1.2x the typical share", "Golf country and southern food"],
    ),
    ("home_services", "24340"): (
        "Grand Rapids homes through four seasons",
        "Homeowners in West Michigan who prepare for long winters.",
        ["Furnace checks before winter", "Spring roof and gutter care"],
        [
            "Home improvement stores at about the typical share",
            "Manufacturing jobs and lakes nearby",
        ],
    ),
    # ---- Sample DTC beauty brand ----
    ("beauty_wellness", "44180"): (
        "Springfield's med-spa crowd",
        "Adults in a growing Ozarks metro (+5.1% since 2020) who visit med spas.",
        ["Skincare that extends med-spa results", "Wellness on a budget"],
        ["Medical spas at 1.4x the typical share", "Ozarks lakes and outdoor life"],
    ),
    ("beauty_wellness", "22220"): (
        "Northwest Arkansas is growing fast",
        "Young professionals and families moving in (+13.1% since 2020).",
        ["Routines for a new city", "Wellness for busy weeks"],
        ["Medical spas at 1.2x the typical share", "Food trucks and Ozark cabins"],
    ),
    ("beauty_wellness", "46060"): (
        "Tucson skin needs sun care",
        "Adults in a hot, sunny desert metro who already use med spas.",
        ["Sun repair and hydration", "Supplements for hot, active days"],
        ["Medical spas at 1.6x the typical share", "Mountains, desert sun and national parks"],
    ),
    ("beauty_wellness", "19780"): (
        "Des Moines wellness, close to home",
        "Health-conscious adults in a growing metro (+6.7% since 2020).",
        ["Supplements that fit a Midwest routine", "Self-care for long winters"],
        ["Supplement stores at 1.1x the typical share", "Insurance and agriculture jobs"],
    ),
    ("beauty_wellness", "17820"): (
        "Colorado Springs takes care of itself",
        "Active adults and military families at altitude.",
        ["Skincare for dry mountain air", "Supplements for an active lifestyle"],
        ["Medical spas at 1.7x the typical share", "Pikes Peak and a large military community"],
    ),
}

_SIGNAL = re.compile(r"^(?P<label>.+): (?P<ratio>[\d.]+)x typical share")


def grounded(item: dict[str, object]) -> list[str]:
    """The three strongest signals (by ratio) and the Census figures, from the data."""
    parsed = []
    for raw in item["signals"]:  # type: ignore[attr-defined]
        m = _SIGNAL.match(str(raw))
        if m:
            parsed.append((float(m["ratio"]), m["label"]))
    lines = [
        f"{label}: {ratio:g}x the typical metro's share of businesses"
        for ratio, label in sorted(parsed, reverse=True)[:3]
    ]
    growth = float(item["growth_2020_25_pct"])  # type: ignore[arg-type]
    population = int(item["population"])  # type: ignore[call-overload]
    lines.append(f"{population:,} people in 2025, {growth:+.1f}% since 2020 (Census)")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=BRIEFS_FILE)
    args = parser.parse_args()

    items = json.loads(args.inputs.read_text())
    by_key = {(i["preset"], i["metro_code"]): i for i in items}
    missing = sorted(set(by_key) - set(TEXTS))
    extra = sorted(set(TEXTS) - set(by_key))
    if missing or extra:
        raise SystemExit(f"texts and inputs differ: missing {missing}, extra {extra}")

    briefs = []
    for key, item in by_key.items():
        headline, audience, angles, context = TEXTS[key]
        brief = Brief(
            preset=key[0],
            metro_code=key[1],
            metro_name=item["metro_name"],
            headline=headline,
            audience=audience,
            angles=angles,
            local_context=context,
            grounded_in=grounded(item),
        )
        briefs.append(brief.model_dump())
    args.out.write_text(json.dumps(briefs, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {len(briefs)} briefs to {args.out}")


if __name__ == "__main__":
    main()
