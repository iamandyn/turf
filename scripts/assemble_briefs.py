"""Assemble src/turf_app/briefs.json: the pre-written creative briefs.

The prose (headline, audience, angles, local context) below was written once,
by Claude in a Claude Code session on 2026-11-19, from each metro's numbers as
dumped by brief_inputs.py: its signals vs. the typical metro, Census figures,
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
    # ---- Pet care ----
    ("pets", "14740"): (
        "Kitsap's dogs know the beach better than you do",
        "Dog owners around the Sound who plan weekends around shoreline walks and ferry trips.",
        ["Book a walker for ferry-commute days", "Sitters who handle sandy, salty paws"],
        ["Beaches are 11x the typical metro's share of places", "Marinas and waterfront living"],
    ),
    ("pets", "31540"): (
        "Madison has the dog parks. You bring the schedule.",
        "Busy professionals and grad students with dogs who are out of the house all day.",
        [
            "Midday walks between lectures and lab shifts",
            "Boarding for game weekends and trips home",
        ],
        ["A university and health-care town", "Dog parks at nearly 4x the typical share"],
    ),
    ("pets", "24540"): (
        "Fast-growing Greeley, with dogs to match",
        "New homeowners in one of the fastest-growing metros in the country (+14% since 2020).",
        [
            "Trusted care for families who just moved in",
            "Trainers for the new puppy in the new yard",
        ],
        ["Farm and ranch country", "No dog walkers listed yet: an open lane"],
    ),
    ("pets", "38860"): (
        "Portland, Maine: dogs welcome, all year round",
        "Coastal dog owners who travel in summer and need boarding they can trust.",
        ["Summer-season boarding before the rush", "Walkers for long work days in a small city"],
        ["Beaches, inns and boat tours set the rhythm", "Pet boarding at 2.5x the typical share"],
    ),
    ("pets", "36500"): (
        "Olympia's dogs deserve a trainer who gets them",
        "Owners of young or rescue dogs in the state capital region.",
        ["Training that fits around state-office hours", "Local sitters for rainy-day check-ins"],
        ["Dog trainers at 2.2x the typical share", "Beer bars and a laid-back South Sound pace"],
    ),
    ("pets", "22660"): (
        "Trail dogs need walkers too",
        "Outdoorsy owners near the Front Range whose dogs come along on every trip.",
        ["Walks on the days you can't hit the trail", "Care while you're up in the mountains"],
        ["Next door to Rocky Mountain National Park", "Dog walkers at 4.7x the typical share"],
    ),
    ("pets", "39740"): (
        "Reading's dog lovers, one walk at a time",
        "Dog owners in Berks County with long commutes to Philadelphia and beyond.",
        ["Midday walks for commuter households", "Groomers and walkers booked in one place"],
        ["Dog walkers at 3.1x the typical share", "Farmland and small towns around the city"],
    ),
    ("pets", "49620"): (
        "York County pets, pampered",
        "Suburban families who already spend on grooming and want one app for everything.",
        ["Grooming plus walks in a single booking", "Sitters for long weekends at the shore"],
        ["Groomers at 1.9x the typical share", "Small-town shops, wineries and produce stands"],
    ),
    ("pets", "17820"): (
        "Colorado Springs: your dog's mountain town",
        "Active households, including military families who move often and need trusted care fast.",
        ["Vetted walkers when you've just moved in", "Trainers for mountain-ready dogs"],
        ["Dog walkers at 3.9x the typical share", "Pikes Peak and mountain cabins nearby"],
    ),
    ("pets", "14260"): (
        "Boise is booming, and so are its dogs",
        "Newcomers in a metro that grew 12% since 2020, many with dogs and no local network yet.",
        ["Find your first sitter in a new city", "Walkers who know the Greenbelt"],
        [
            "Dog walkers at 3.8x the typical share",
            "Wineries and outdoor living around the Treasure Valley",
        ],
    ),
    ("pets", "14500"): (
        "Boulder's dogs live an active lifestyle. Keep up.",
        "Health-minded professionals who treat their dogs like training partners.",
        ["Walks and runs while you're at the lab", "Boarding for ski weekends"],
        ["Dog walkers at 3.2x the typical share", "Research labs, wellness and the Flatirons"],
    ),
    ("pets", "39580"): (
        "Raleigh's newest residents have four legs too",
        "Young professionals moving to the Triangle (+12.6% since 2020) for tech and pharma jobs.",
        ["A walker on day one in a new city", "Care that fits a hybrid work week"],
        ["Dog walkers at 3.8x the typical share", "Pharma and research jobs across the Triangle"],
    ),
    ("pets", "37980"): (
        "Philly rowhouse dogs need their walks",
        "City dwellers without yards, where a midday walk isn't optional.",
        ["Midday walks for rowhouse dogs", "Sitters a train ride away"],
        ["Dog walkers at 3.2x the typical share", "Dense, transit-connected neighborhoods"],
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
    ("pets", "38900"): (
        "Portland's dogs go everywhere. Their sitters should too.",
        "Urban dog owners in Portland and Vancouver who take their dogs to breweries and parks.",
        ["Boarding for wine-country weekends", "Walkers for rainy months"],
        ["Pet boarding at 1.7x the typical share", "Wineries and beer bars well above typical"],
    ),
    # ---- Health & fitness ----
    ("fitness", "42100"): (
        "Train like Santa Cruz: ocean first, then pilates",
        "Coastal, wellness-minded adults who mix surfing, yoga and studio classes.",
        ["Workouts that fit around the swell", "Pilates and yoga in one membership"],
        [
            "Pilates studios at 2.8x the typical share",
            "Beaches, skate parks and wellness everywhere",
        ],
    ),
    ("fitness", "11460"): (
        "Ann Arbor stretches between lectures",
        "Students, researchers and young professionals in a university town.",
        ["Yoga for exam weeks", "Strength plans that fit a lab schedule"],
        ["Yoga studios at 2.9x the typical share", "A research and university economy"],
    ),
    ("fitness", "14860"): (
        "Fairfield County's commuters train early",
        "Affluent commuters along the New York line who book premium classes.",
        ["Pilates before the 7:12", "Boxing for the post-market crowd"],
        [
            "Pilates at 2.8x and boxing classes at 2.4x the typical share",
            "Finance jobs and train stations",
        ],
    ),
    ("fitness", "14500"): (
        "In Boulder, everyone trains. Train smarter.",
        "Highly active adults who already run, climb and do yoga, and want structure.",
        [
            "Recovery and mobility for trail runners",
            "A plan that ties studio and outdoor training together",
        ],
        ["Pilates studios at 4.3x the typical share", "Wellness, acupuncture and mountain sports"],
    ),
    ("fitness", "14460"): (
        "Boston's biotech crowd needs a workout plan",
        "Young professionals in research, health care and tech across Boston and Cambridge.",
        ["Workouts between experiments", "Yoga that travels with you on the T"],
        ["Yoga studios at 2.1x the typical share", "Biotech and labs at 13x typical"],
    ),
    ("fitness", "31700"): (
        "Southern New Hampshire's martial arts and yoga belt",
        "Families and commuters near Manchester and Nashua who train close to home.",
        ["Classes for parents and kids together", "Yoga for the drive home"],
        [
            "Martial arts clubs at 1.7x and yoga at 2.0x the typical share",
            "Outlets and nature reserves",
        ],
    ),
    ("fitness", "39300"): (
        "Providence gets moving, from boxing to yoga",
        "Students and young professionals in a compact, walkable metro.",
        ["Boxing classes after work", "Yoga by the water"],
        ["Boxing at 2.1x and yoga at 2.1x the typical share", "Marinas and a coastal lifestyle"],
    ),
    ("fitness", "12420"): (
        "Austin's newcomers want a trainer, not just a gym",
        "Tech workers new to one of the fastest-growing big metros (+14% since 2020).",
        ["Personal-trainer guidance in your pocket", "Pilates and strength in one plan"],
        ["Fitness trainers at 1.8x the typical share", "Tech jobs, taco trucks and Tex-Mex"],
    ),
    ("fitness", "41740"): (
        "San Diego trains outdoors. Make it count.",
        "Beach-going, health-focused adults, many in biotech and the military.",
        ["Pilates for core strength on and off the board", "Outdoor workouts with structure"],
        ["Pilates studios at 2.8x the typical share", "Beaches and biotech well above typical"],
    ),
    ("fitness", "47930"): (
        "Waterbury trains close to home",
        "Working families in the Naugatuck Valley who fit workouts around shifts.",
        ["Martial arts for the whole family", "Short workouts for shift workers"],
        ["Martial arts clubs at 1.7x the typical share", "Manufacturing and machine shops"],
    ),
    # ---- Home services ----
    ("home_services", "15980"): (
        "Southwest Florida homes work hard in the heat",
        "Homeowners and second-home owners in a fast-growing coastal metro (+14% since 2020).",
        ["AC tune-ups before summer", "Storm-season roof checks"],
        [
            "HVAC at 2.1x and roofing at 2.3x the typical share",
            "Vacation rentals and boating everywhere",
        ],
    ),
    ("home_services", "24540"): (
        "New homes in Greeley need a maintenance plan",
        "Families buying new builds in a metro that grew 14% since 2020.",
        [
            "One plan for the first years in a new home",
            "Hail and roof checks for Front Range weather",
        ],
        [
            "Roofing at 2.3x and plumbing at 1.9x the typical share",
            "Ranching and oil and gas country",
        ],
    ),
    ("home_services", "19300"): (
        "Baldwin County homes face the Gulf",
        "Retirees and families moving to Alabama's coast (+15% since 2020).",
        ["Hurricane-season prep, scheduled for you", "Upkeep for homes you're not always in"],
        ["HVAC at 1.7x the typical share", "Vacation rental homes far above typical"],
    ),
    ("home_services", "38940"): (
        "Port St. Lucie: a new home every day",
        "Buyers in one of the fastest-growing metros in the US (+16% since 2020).",
        ["AC care that never gets skipped", "A cleaner and a plumber you already trust"],
        ["HVAC at 1.9x the typical share", "Boating and beaches along the Treasure Coast"],
    ),
    ("home_services", "47930"): (
        "Old New England homes need steady care",
        "Owners of older homes in the Naugatuck Valley who'd rather plan repairs than react.",
        ["Plumbing check-ups for older pipes", "Seasonal plans for long winters"],
        ["Home improvement stores at 1.7x the typical share", "Masonry and manufacturing trades"],
    ),
    ("home_services", "37340"): (
        "Space Coast homes, kept mission-ready",
        "Engineers and families moving in for aerospace jobs (+9% since 2020).",
        ["Preventive maintenance for busy engineers", "Storm-season roof and AC checks"],
        ["Home cleaning at 1.8x and roofing at 1.7x the typical share", "Aerospace and beaches"],
    ),
    ("home_services", "35840"): (
        "Sarasota homes need year-round care, even when you're away",
        "Retirees and seasonal residents in a metro that grew 13% since 2020.",
        ["Checks while you're up north", "AC and plumbing on a schedule"],
        ["Home cleaning at 1.6x the typical share", "Vacation homes and boating"],
    ),
    ("home_services", "49620"): (
        "York County homeowners like to do it right",
        "DIY-minded suburban homeowners who want a pro for the big jobs.",
        ["A pro for what you'd rather not DIY", "One plan for HVAC, cleaning and repairs"],
        ["Home improvement stores at 1.6x the typical share", "Small towns and farmland"],
    ),
    ("home_services", "43640"): (
        "Northshore homes take on Louisiana weather",
        "Families north of Lake Pontchartrain dealing with heat, humidity and storms.",
        ["Humidity-proof HVAC care", "Storm prep on a schedule"],
        ["HVAC at 1.5x the typical share", "Lakefront living, marinas and Cajun food"],
    ),
    ("home_services", "34940"): (
        "Naples homes, cared for in season and out",
        "Affluent seasonal residents who want their homes ready when they arrive.",
        ["Cleaning before you fly in", "Roof checks after storm season"],
        [
            "Home cleaning at 1.9x the typical share",
            "Boat tours, beaches and the Everglades nearby",
        ],
    ),
    # ---- Beauty & wellness ----
    ("beauty_wellness", "32580"): (
        "The Rio Grande Valley invests in wellness",
        "Health-conscious families who already shop at health food and supplement stores.",
        ["Bilingual wellness content", "Supplements for hot, active days"],
        ["Health food stores at 3x the typical share", "Tex-Mex and family-centered culture"],
    ),
    ("beauty_wellness", "43640"): (
        "Northshore self-care, done right",
        "Suburban women who book spa and salon visits regularly.",
        ["Glow-up routines for humid summers", "Spa-quality results at home"],
        [
            "Health food stores at 1.9x and med spas at 1.6x the typical share",
            "Lake and bayou lifestyle",
        ],
    ),
    ("beauty_wellness", "22180"): (
        "Fayetteville's salon culture runs deep",
        "Military families and young adults who keep a regular salon routine.",
        ["Haircare that travels through a PCS move", "Supplements for active lifestyles"],
        [
            "Hair salons at 1.6x the typical share",
            "A large military community; Caribbean and soul food",
        ],
    ),
    ("beauty_wellness", "45300"): (
        "Tampa Bay's med-spa boom",
        "Image-conscious adults in a growing metro (+7% since 2020) where med spas are everywhere.",
        ["Between-appointment skincare", "Sun-proof routines for beach life"],
        ["Med spas at 1.9x the typical share", "Beaches and boating culture"],
    ),
    ("beauty_wellness", "34940"): (
        "Naples spends on looking good",
        "Affluent residents, many seasonal, who already use med spas.",
        ["Premium skincare that matches the med-spa routine", "Sun repair for year-round Florida"],
        ["Med spas at 2.5x the typical share", "Boat tours and beaches far above typical"],
    ),
    ("beauty_wellness", "12060"): (
        "Atlanta's hair care capital",
        "Style-forward adults in a big, fast-growing metro with a strong salon culture.",
        ["Haircare that protects the salon look", "Wellness for a busy city"],
        ["Hair salons at 1.5x the typical share", "Southern and Caribbean food, talent agencies"],
    ),
    ("beauty_wellness", "17300"): (
        "Clarksville's self-care routine",
        "Young military families and service members near Fort Campbell.",
        ["Nails and wellness on a budget", "Supplements for active duty schedules"],
        [
            "Nail salons at 1.5x and health food stores at 1.6x the typical share",
            "A large military community",
        ],
    ),
    ("beauty_wellness", "38060"): (
        "Phoenix skin needs serious care",
        "Adults in a hot, sunny, fast-growing metro (+7% since 2020) who visit med spas.",
        ["Desert-proof skincare", "Supplements for heat and hydration"],
        ["Med spas at 2.9x the typical share", "Pools, sun and mountain trails"],
    ),
    ("beauty_wellness", "15180"): (
        "Brownsville's spa-day habit",
        "Families in the lower Rio Grande Valley who spend on spa and wellness.",
        ["Bilingual skincare routines", "Wellness for coastal heat"],
        ["Spas at 1.7x and med spas at 1.8x the typical share", "South Padre beaches nearby"],
    ),
    ("beauty_wellness", "28660"): (
        "Killeen-Temple looks after itself",
        "Military families near Fort Cavazos and a growing population (+7% since 2020).",
        ["Salon-quality results between appointments", "Wellness for shift schedules"],
        [
            "Nail salons at 1.4x the typical share",
            "A large military community; Korean and Caribbean food",
        ],
    ),
    # ---- Young professionals ----
    ("young_professionals", "22020"): (
        "Fargo's young workforce is growing",
        "Students and early-career professionals in a metro up 7.7% since 2020.",
        ["Budgeting for a first apartment", "Productivity for students who work"],
        ["Coworking at 2x and colleges at 2.2x the typical share", "Apartments well above typical"],
    ),
    ("young_professionals", "35380"): (
        "New Orleans: work hard, then go out",
        "Young adults in a city with a big nightlife scene.",
        ["Money tools for nights out and big festivals", "Side-gig income tracking"],
        ["Bars at 3x the typical share", "Cocktail bars, live music and Creole food"],
    ),
    ("young_professionals", "23540"): (
        "Gainesville: built around campus",
        "University students and new grads in health care and research.",
        ["A first budget, built for students", "Tools for grad-school productivity"],
        ["Colleges at 3.9x the typical share", "Research institutes and game-day culture"],
    ),
    ("young_professionals", "46520"): (
        "Honolulu's young professionals juggle high costs",
        "Young adults in one of the most expensive metros in the US.",
        ["Saving goals for island living", "Coffee-shop productivity"],
        ["Smoothie bars and coffee shops above typical", "Beaches and outdoor living"],
    ),
    ("young_professionals", "45780"): (
        "Toledo works from the coffee shop",
        "Young workers and students in an affordable Great Lakes city.",
        ["Make the most of a lower cost of living", "Coworking-friendly productivity"],
        ["Bars at 2x and coworking at 1.7x the typical share", "Marinas and lake life"],
    ),
    ("young_professionals", "11460"): (
        "Ann Arbor's startup and study crowd",
        "Students, researchers and startup founders in a university town.",
        ["Tools for the lab-to-startup path", "Student-friendly finance"],
        [
            "Colleges at 4x and coworking at 3.9x the typical share",
            "Research institutes and biotech",
        ],
    ),
    ("young_professionals", "24580"): (
        "Green Bay works, then hits the bar",
        "Young workers in a tight-knit city with a big game-day culture.",
        ["Money tools for game days and road trips", "Coworking productivity"],
        ["Bars at 3.5x and coworking at 3.2x the typical share", "Lake life and dairy country"],
    ),
    ("young_professionals", "30700"): (
        "Lincoln's campus-to-career city",
        "University students and young professionals in Nebraska's capital.",
        ["A first-job money plan", "Study tools for campus life"],
        ["Colleges at 2.7x the typical share", "Campus buildings and agricultural research"],
    ),
    ("young_professionals", "17780"): (
        "College Station runs on students",
        "University students in a fast-growing college town (+6.9% since 2020).",
        ["Budgets built around the semester", "Study tools that work on game days too"],
        ["Colleges at 3.8x the typical share", "Research institutes and Tex-Mex"],
    ),
    ("young_professionals", "39300"): (
        "Providence: small city, big coffee culture",
        "Students and young professionals in a walkable, college-heavy metro.",
        ["Productivity from the coffee shop", "Student-to-first-job finance"],
        ["Coffee shops at 1.6x the typical share", "Marinas and a coastal lifestyle"],
    ),
    # ---- Families with kids ----
    ("families", "36260"): (
        "Ogden's kids are always on the move",
        "Young families along the Wasatch Front who sign kids up for everything.",
        ["Learning that fits between gymnastics and practice", "Tools for big, busy families"],
        [
            "Gymnastics centers at 2.6x and pediatric dentists at 2.5x the typical share",
            "Hiking trails and amusement parks",
        ],
    ),
    ("families", "36540"): (
        "Omaha families plan ahead",
        "Young families in a growing metro (+4.2% since 2020) with lots of pediatric care.",
        ["Organize the pediatrician, daycare and activities", "Early learning at home"],
        ["Pediatric clinics at 2.5x the typical share", "Daycare at 1.9x typical"],
    ),
    ("families", "29180"): (
        "Acadiana's kids, dressed for every occasion",
        "Families in Cajun country who spend on kids' clothing and activities.",
        ["Learning rooted in local culture", "Activities beyond the gymnastics gym"],
        [
            "Children's clothing stores at 2.7x the typical share",
            "Cajun food, festivals and oil and gas jobs",
        ],
    ),
    ("families", "12940"): (
        "Baton Rouge: family life, full schedule",
        "Families who balance pediatric visits, daycare and activities.",
        ["One place for the family calendar", "Learning between school and practice"],
        ["Pediatric clinics at 2.4x the typical share", "Children's clothing at 2x typical"],
    ),
    ("families", "42340"): (
        "Savannah's growing families",
        "Young families in a metro up 8% since 2020.",
        ["Settling kids into a new city", "Learning on the go"],
        [
            "Children's clothing at 2.5x the typical share",
            "Historic squares, beaches and river life",
        ],
    ),
    ("families", "21780"): (
        "Evansville families, well looked after",
        "Working families on the Ohio River who rely on local pediatric care.",
        ["Organize appointments and school life", "Affordable at-home learning"],
        ["Pediatric clinics at 2.4x the typical share", "Manufacturing jobs"],
    ),
    ("families", "39340"): (
        "Utah County's big young families",
        "Families with young kids in one of the fastest-growing metros (+14.5% since 2020).",
        ["Learning tools for siblings of every age", "Activity planning for busy households"],
        ["Gymnastics centers at 2.4x the typical share", "Tech jobs in Silicon Slopes"],
    ),
    ("families", "31140"): (
        "Louisville families, from checkups to classes",
        "Families who use the city's large pediatric and medical network.",
        ["Keep health visits and activities in one place", "Learning for school-age kids"],
        ["Pediatric clinics at 2.3x the typical share", "A major medical center; bourbon country"],
    ),
    ("families", "14860"): (
        "Fairfield County invests in its kids",
        "Affluent commuter families who spend on childcare, clothes and enrichment.",
        ["Premium learning at home", "Enrichment that fits commuter schedules"],
        ["Children's clothing at 1.8x the typical share", "Finance jobs and train stations"],
    ),
    ("families", "29700"): (
        "Laredo's families, close-knit and growing",
        "Bilingual families in a border city up 5% since 2020.",
        ["Bilingual learning at home", "Activities that keep kids moving"],
        ["Children's clothing at 2.6x the typical share", "A logistics and trade hub"],
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
