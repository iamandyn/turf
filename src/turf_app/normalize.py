"""Text normalization shared by the pipeline and the API.

The API must normalize search queries exactly the way the pipeline normalized
place and brand names, so both import from here.
"""

from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Words dropped from the end of brand names, so "Starbucks Coffee Company" and
# "Starbucks" produce the same chain key. Applied repeatedly, longest first.
_GENERIC_SUFFIXES = (
    "coffee company",
    "corporation",
    "company",
    "corp",
    "inc",
    "llc",
    "ltd",
    "co",
)

# Hand-maintained aliases for chains whose names vary in ways suffix
# stripping cannot fix. Keys and values are already-normalized text.
CHAIN_ALIASES: dict[str, str] = {
    "exxon and mobil stations": "exxonmobil",
    "exxon": "exxonmobil",
    "mobil": "exxonmobil",
}


def normalize_text(value: str | None) -> str:
    """Lowercase, strip accents and punctuation, and collapse whitespace.

    Apostrophes are removed rather than turned into spaces, so "McDonald's"
    becomes "mcdonalds" and matches a user typing "mcdonalds".
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    no_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    text = no_accents.lower().replace("&", " and ")
    text = text.replace("'", "").replace("’", "")
    return _NON_ALNUM.sub(" ", text).strip()


def chain_key(brand_or_name: str | None) -> str:
    """Canonical key for a chain, e.g. "Starbucks Coffee Company" -> "starbucks"."""
    text = normalize_text(brand_or_name)
    changed = True
    while changed and text:
        changed = False
        for suffix in _GENERIC_SUFFIXES:
            if text.endswith(" " + suffix):
                text = text[: -len(suffix) - 1].rstrip()
                changed = True
                break
    return CHAIN_ALIASES.get(text, text)


def name_prefixes(name: str | None, max_tokens: int = 5) -> list[str]:
    """Token prefixes of a normalized name, longest first.

    "Starbucks Coffee Reserve" -> ["starbucks coffee reserve", "starbucks coffee", "starbucks"].
    Matching chains by whole-token prefixes avoids "Subway" matching "Subwayside Deli".
    """
    tokens = normalize_text(name).split()
    return [" ".join(tokens[:n]) for n in range(min(len(tokens), max_tokens), 0, -1)]


_DIRECTIONS = frozenset(
    {"n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east", "west"}
)
_STREET_WORD_ALIASES = {
    "ih": "interstate", "i": "interstate",
    "hwy": "highway", "hiway": "highway",
    "st": "saint", "mt": "mount", "ft": "fort",
    "1st": "first", "2nd": "second", "3rd": "third", "4th": "fourth", "5th": "fifth",
    "6th": "sixth", "7th": "seventh", "8th": "eighth", "9th": "ninth", "10th": "tenth",
}  # fmt: skip


def street_key(address: str | None) -> str | None:
    """Street number plus first street word, for spotting the same address written twice.

    "9500 S Ih 35 Ste L800" and "9500 S Interstate 35 Frontage Rd" both give
    "9500 interstate"; "1 Plattekill Ave" and "1 Hawk Dr" stay different.
    Returns None when the address doesn't start with a street number.
    """
    tokens = normalize_text(address).split()
    if len(tokens) < 2 or not tokens[0].isdigit():
        return None
    rest = tokens[1:]
    if len(rest) > 1 and rest[0] in _DIRECTIONS:
        rest = rest[1:]
    word = _STREET_WORD_ALIASES.get(rest[0], rest[0])
    return f"{tokens[0]} {word}"


_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}  # fmt: skip
STATE_CODES = frozenset(_STATE_NAMES.values())


def normalize_state(region: str | None) -> str | None:
    """Map "US-TX", "TX", "Tx", or "Texas" to "TX". Anything else becomes None.

    Territory codes (US-PR, US-GU, ...) also become None: in this data they are
    usually mislabeled places inside the 50 states, so they are left for the
    location-based state lookup to fill in.
    """
    if not region:
        return None
    text = region.strip()
    if text.upper().startswith("US-"):
        text = text[3:]
    if text.upper() in STATE_CODES:
        return text.upper()
    return _STATE_NAMES.get(normalize_text(text))
