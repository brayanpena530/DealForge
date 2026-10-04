"""Tests for the H-E-B in-store location parser.

Every pattern below was observed verbatim on heb.com (2026-10-04). The
parser is deterministic and never raises: unparseable input returns None.
"""

from __future__ import annotations

from dealforge.providers.heb.location import StoreLocation, parse_location


def loc(raw: str) -> StoreLocation:
    parsed = parse_location(raw)
    assert parsed is not None, f"expected {raw!r} to parse"
    return parsed


# ---------------------------------------------------------------------------
# observed patterns
# ---------------------------------------------------------------------------


def test_zone_position_and_aisle_code():
    p = loc("In Dairy on the Left Wall, A24")
    assert p.zone == "Dairy"
    assert p.position_hint == "Left Wall"
    assert p.aisle_code == "A24"
    assert p.aisle_letter == "A"
    assert p.aisle_number == 24
    assert p.raw == "In Dairy on the Left Wall, A24"


def test_store_suffix_is_stripped():
    p = loc("In Dairy on the Left Wall, A25 at The Heights H-E-B")
    assert p.zone == "Dairy"
    assert p.aisle_code == "A25"
    assert "H-E-B" not in p.zone


def test_bare_aisle_word():
    p = loc("Aisle 10")
    assert p.zone is None
    assert p.aisle_code is None
    assert p.aisle_letter is None
    assert p.aisle_number == 10


def test_aisle_word_with_letter_code_prefers_code():
    p = loc("Aisle 11, A12")
    assert p.aisle_code == "A12"
    assert p.aisle_letter == "A"
    assert p.aisle_number == 12


def test_zone_with_aisle_code():
    p = loc("In Produce, A3")
    assert p.zone == "Produce"
    assert p.aisle_code == "A3"


def test_zone_only():
    p = loc("In Bakery")
    assert p.zone == "Bakery"
    assert p.aisle_code is None
    assert p.position_hint is None


def test_zone_with_position_no_aisle():
    p = loc("In Produce on the Front Wall")
    assert p.zone == "Produce"
    assert p.position_hint == "Front Wall"
    assert p.aisle_code is None


def test_near_checkout():
    p = loc("Near Checkout 1")
    assert p.zone == "Checkout"
    assert p.aisle_number == 1


def test_left_edge_of_zone():
    p = loc("On the Left Edge of Meat Market")
    assert p.zone == "Meat Market"
    assert p.position_hint == "Left Edge"


def test_zone_position_and_b_code():
    p = loc("In Meat Market on the Right Wall, B17")
    assert p.zone == "Meat Market"
    assert p.position_hint == "Right Wall"
    assert p.aisle_code == "B17"
    assert p.aisle_letter == "B"
    assert p.aisle_number == 17


def test_zone_back_wall_no_aisle():
    p = loc("In Dairy on the Back Wall")
    assert p.zone == "Dairy"
    assert p.position_hint == "Back Wall"
    assert p.aisle_code is None


def test_frozen_zone_variant():
    p = loc("In Frozen Foods, B12")
    assert p.zone == "Frozen"
    assert p.aisle_code == "B12"


# ---------------------------------------------------------------------------
# edge cases: never raise, return None
# ---------------------------------------------------------------------------


def test_none_empty_whitespace():
    assert parse_location(None) is None
    assert parse_location("") is None
    assert parse_location("   ") is None


def test_nonsense_returns_none():
    assert parse_location("Somewhere over there") is None
    assert parse_location("???") is None
    assert parse_location("at The Heights H-E-B") is None


def test_raw_is_preserved_verbatim():
    raw = "In Dairy on the Left Wall, A25 at The Heights H-E-B"
    assert loc(raw).raw == raw


def test_case_insensitive_position():
    p = loc("in dairy ON THE back WALL")
    assert p.zone == "Dairy"
    assert p.position_hint == "Back Wall"
