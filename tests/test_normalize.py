"""Cases taken from real Overture rows found while inspecting the 2026-11-18.0 release."""

import pytest

from turf_app.normalize import (
    chain_key,
    name_prefixes,
    normalize_state,
    normalize_text,
    street_key,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("McDonald's", "mcdonalds"),
        ("McDonald’s", "mcdonalds"),
        ("Anh Phương", "anh phuong"),
        ("H-E-B", "h e b"),
        ("Exxon & Mobil Stations", "exxon and mobil stations"),
        ("  Starbucks-  Craig ", "starbucks craig"),
        (None, ""),
        ("", ""),
    ],
)
def test_normalize_text(raw: str | None, expected: str) -> None:
    assert normalize_text(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Starbucks", "starbucks"),
        ("Starbucks Coffee Company", "starbucks"),
        ("Orangetheory Fitness", "orangetheory fitness"),
        ("Exxon & Mobil Stations", "exxonmobil"),
        ("Exxon", "exxonmobil"),
        ("Acme Widgets, Inc.", "acme widgets"),
        ("Acme Co. LLC", "acme"),
        # A brand that is only a suffix word is left alone rather than emptied.
        ("Co", "co"),
    ],
)
def test_chain_key(raw: str, expected: str) -> None:
    assert chain_key(raw) == expected


def test_name_prefixes_longest_first() -> None:
    assert name_prefixes("Starbucks Coffee Reserve") == [
        "starbucks coffee reserve",
        "starbucks coffee",
        "starbucks",
    ]


def test_name_prefixes_whole_tokens_only() -> None:
    assert "subway" not in name_prefixes("Subwayside Deli")


def test_name_prefixes_caps_length() -> None:
    assert len(name_prefixes("a b c d e f g", max_tokens=5)) == 5


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("US-TX", "TX"),
        ("TX", "TX"),
        ("Tx", "TX"),
        ("Texas", "TX"),
        ("New York", "NY"),
        ("us-dc", "DC"),
        ("US-PR", None),
        ("FM", None),
        (None, None),
        ("", None),
    ],
)
def test_normalize_state(raw: str | None, expected: str | None) -> None:
    assert normalize_state(raw) == expected


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        # Real duplicate pairs from the data.
        ("9500 S Ih 35 Ste L800", "9500 S Interstate 35 Frontage Rd Ste L800", True),
        ("400 East Second Street", "400 E 2nd St", True),
        ("3322 132nd St SE (Albertsons - Thomas, Lake #413)", "3322 132nd St SE", True),
        ("2517 W. North Ave.", "2517 W North Ave (Melrose Park-North &, 25th)", True),
        # Real non-duplicate: same number, different street.
        ("1 Plattekill Ave", "1 Hawk Dr", False),
        ("100 Main St", "250 Main St", False),
    ],
)
def test_street_key_pairs(a: str, b: str, same: bool) -> None:
    assert (street_key(a) == street_key(b)) is same


@pytest.mark.parametrize("raw", [None, "", "Main St", "PO Box 12", "100"])
def test_street_key_none_without_number_and_street(raw: str | None) -> None:
    assert street_key(raw) is None


def test_street_key_keeps_street_named_like_a_direction() -> None:
    # "2517 W North Ave": skip "w", keep "north" as the street name.
    assert street_key("2517 W North Ave") == "2517 north"
