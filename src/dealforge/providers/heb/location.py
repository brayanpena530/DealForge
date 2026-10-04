"""Deterministic parsing of H-E-B in-store location strings.

H-E-B shows a location on search results and product pages, verbatim, e.g.::

    In Dairy on the Left Wall, A24
    In Dairy on the Left Wall, A25 at The Heights H-E-B
    Aisle 10
    Aisle 11, A12
    In Produce, A3
    In Bakery
    In Produce on the Front Wall
    Near Checkout 1
    On the Left Edge of Meat Market
    In Meat Market on the Right Wall, B17
    In Dairy on the Back Wall

This module turns those strings into structured :class:`StoreLocation`
values. The parser is deliberately regex-based, not an LLM: the grammar is
small and closed, and a route plan must be reproducible and auditable.

``parse_location`` never raises -- unparseable input returns ``None`` and the
caller treats the item as unplaced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["StoreLocation", "parse_location", "normalize_zone"]

#: "A24", "B17" -- lettered aisle codes. Preferred over a bare "Aisle N"
#: when both appear ("Aisle 11, A12" -> A12).
_AISLE_CODE = re.compile(r"\b([A-Z])(\d{1,3})\b")

#: "Aisle 10" with no letter code.
_AISLE_WORD = re.compile(r"\baisle\s+(\d{1,3})\b", re.I)

#: "on the Left Wall" / "on the Back Edge" etc.
_POSITION = re.compile(r"\bon the\s+(left|right|front|back)\s+(wall|edge)\b", re.I)

#: "On the Left Edge of Meat Market" -- position first, zone after "of".
_POSITION_FIRST = re.compile(
    r"\bon the\s+(left|right|front|back)\s+(wall|edge)\s+of\s+([a-z][a-z&' ]*?)\s*$",
    re.I,
)

#: "Near Checkout 1" / "Near Checkout".
_NEAR_CHECKOUT = re.compile(r"\bnear\s+checkout\s*(\d{0,3})\b", re.I)

#: "In Dairy", "In Meat Market on the Right Wall", ...
_ZONE = re.compile(r"^\s*in\s+([a-z][a-z&' ]*?)(?:\s+on the|\s*,|\s*$)", re.I)

#: Trailing " at The Heights H-E-B" (any store name ending in H-E-B).
_STORE_SUFFIX = re.compile(r"\s+at\s+[\w\s.'-]*H-?E-?B\.?\s*$", re.I)

#: Lowercased zone fragment -> canonical zone name.
_ZONE_CANONICAL = {
    "dairy": "Dairy",
    "produce": "Produce",
    "bakery": "Bakery",
    "meat market": "Meat Market",
    "meat": "Meat Market",
    "seafood": "Seafood",
    "deli": "Deli",
    "frozen": "Frozen",
    "frozen foods": "Frozen",
    "market": "Market",
    "grocery": "Grocery",
    "personal care": "Personal Care",
    "healthy living": "Healthy Living",
    "baby": "Baby",
    "pharmacy": "Pharmacy",
    "floral": "Floral",
    "checkout": "Checkout",
    "coffee shop": "Coffee Shop",
}


def normalize_zone(fragment: str | None) -> str | None:
    """Canonical department name for a raw zone fragment, or None."""
    if not fragment:
        return None
    key = re.sub(r"\s+", " ", fragment.strip().lower())
    if not key:
        return None
    if key in _ZONE_CANONICAL:
        return _ZONE_CANONICAL[key]
    # Unknown department: title-case it rather than dropping the signal.
    return " ".join(w.capitalize() for w in key.split())


@dataclass(frozen=True)
class StoreLocation:
    """One parsed in-store location.

    ``zone`` is the normalized department ("Dairy", "Produce", ...) or None
    for plain numbered grocery aisles. ``aisle_letter``/``aisle_number`` come
    from the letter code ("A24" -> "A"/24) or a bare "Aisle 10" (None/10).
    ``raw`` is always the verbatim string H-E-B displayed.
    """

    zone: str | None
    aisle_code: str | None
    aisle_letter: str | None
    aisle_number: int | None
    position_hint: str | None
    raw: str


def parse_location(raw: str | None) -> StoreLocation | None:
    """Parse an H-E-B location string. Returns None when unparseable.

    Never raises: ``None``, empty, and nonsense inputs all yield ``None``.
    """
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None

    # Drop the store suffix: "In Dairy on the Left Wall, A25 at The Heights H-E-B".
    text = _STORE_SUFFIX.sub("", text).strip()
    if not text:
        return None

    aisle_letter: str | None = None
    aisle_number: int | None = None
    aisle_code: str | None = None

    code = _AISLE_CODE.search(text)
    if code:
        aisle_letter = code.group(1).upper()
        aisle_number = int(code.group(2))
        aisle_code = f"{aisle_letter}{aisle_number}"
    else:
        word = _AISLE_WORD.search(text)
        if word:
            aisle_number = int(word.group(1))

    position_hint: str | None = None
    pos = _POSITION.search(text)
    if pos:
        position_hint = f"{pos.group(1).capitalize()} {pos.group(2).capitalize()}"

    zone: str | None = None
    near = _NEAR_CHECKOUT.search(text)
    if near:
        zone = "Checkout"
        if near.group(1):
            aisle_number = int(near.group(1))
    else:
        zm = _ZONE.match(text)
        if zm:
            zone = normalize_zone(zm.group(1))
        else:
            # "On the Left Edge of Meat Market" -- zone follows the position.
            pf = _POSITION_FIRST.search(text)
            if pf:
                zone = normalize_zone(pf.group(3))

    # Nothing recognizable at all: not a location we can use.
    if (
        zone is None
        and aisle_code is None
        and aisle_number is None
        and position_hint is None
    ):
        return None

    return StoreLocation(
        zone=zone,
        aisle_code=aisle_code,
        aisle_letter=aisle_letter,
        aisle_number=aisle_number,
        position_hint=position_hint,
        raw=raw.strip(),
    )
