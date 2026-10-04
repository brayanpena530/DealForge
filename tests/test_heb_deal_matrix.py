"""Adversarial near-miss matrix for the deal matcher.

Each row is (deal row, product name, row kind, expect_match, veto_pin):

- ``row kind``: "coupon" (the text is a digital-coupon headline) or "ad"
  (the text is a weekly-ad item name; it gets a sale price).
- ``expect_match``: whether ``match_deals_to_items`` must attach the row.
- ``veto_pin``: for rows that must NOT match, the substring the
  :func:`veto_reason` must return. This pins the *mechanism*, not just the
  outcome, so a future change that rejects the row for the wrong reason
  (or stops rejecting it) fails loudly. ``None`` means the row is rejected
  before the gates (below the overlap threshold) and only the outcome is
  asserted.

Dimensions covered: same head noun but different product type (milk vs
yogurt, sausage vs salsa), brand mismatch (Pete and Gerry's vs Sunups,
Dole vs H-E-B, Oatly vs H-E-B), qualifier conflicts (seasoned/fajita vs
natural, round top vs grain & glory vs split top), quantity qualifiers
("buy 2" must name the product exactly), and size mismatches that SHOULD
still match (pack size is not distinguishing: "12 ct" deal vs "18 ct"
product of the same line matches).

Positive controls guard the other direction: exact matches, minor wording
differences, and same-line different-size rows must keep matching, so the
stricter rules cannot silently start rejecting good deals.

How to extend: when a new false positive appears in a live run, add the
pair here with expect_match=False and the veto reason you want. If no
existing gate fires for it, add the gate first (a brand in ``_BRANDS``, a
pair in ``_CONFLICTS``, a word in ``_TRAILING_MODIFIERS``, or a pattern in
``_QUANTITY_RE`` in ``deals.py``), then pin it here.

All offline: rows are parsed by the real headline/ad parsers, no browser.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from dealforge.providers.heb import catalog as catalog_mod
from dealforge.providers.heb.deals import (
    collect_deals,
    match_deals_to_items,
    veto_reason,
)
from dealforge.providers.heb.location import parse_location
from dealforge.providers.heb.route import LocatedItem
from dealforge.providers.heb.schemas import (
    CouponOffer,
    ProductResult,
    WeeklyAdItem,
)

# (offer text, product name, row kind, expect_match, veto_pin)
MATRIX: list[tuple[str, str, str, bool, str | None]] = [
    # -- same head noun, different product type ---------------------------
    (
        "$1 off H-E-B Whole Milk Plain Greek Yogurt, 32 oz.",
        "H-E-B Whole Milk, 1 gal",
        "coupon",
        False,
        "offer head 'yogurt' not in product",
    ),
    (
        "H-E-B Ground Pork Italian Sausage - Mild, 16 oz",
        "H-E-B Fresh Salsa - Mild, 16 oz",
        "ad",
        False,
        "offer head 'sausage' not in product",
    ),
    # -- qualifier conflicts ----------------------------------------------
    (
        "H-E-B Seasoned Chicken Breast for Fajitas",
        "H-E-B Chicken Breast",
        "ad",
        False,
        "offer head 'fajita' not in product",
    ),
    (
        "H-E-B Round Top Large White Enriched Sliced Bread, 20 oz",
        "H-E-B Organics Grain & Glory Sliced Bread, 26 oz",
        "ad",
        False,
        "conflicting qualifiers: round top vs grain glory",
    ),
    (
        "H-E-B Split Top White Enriched Sliced Bread, 20 oz",
        "H-E-B Round Top Large White Enriched Sliced Bread, 20 oz",
        "ad",
        False,
        "conflicting qualifiers: round top vs split top",
    ),
    # -- brand conflicts ---------------------------------------------------
    (
        "$1.50 off Pete and Gerry's Large Brown Eggs, 12 ct.",
        "Sunups Large Brown Eggs, 12 ct",
        "coupon",
        False,
        "brand conflict",
    ),
    (
        "25% off Core Power Milk, 14 oz.",
        "Fairlife Milk, 52 oz",
        "coupon",
        False,
        "brand conflict",
    ),
    (
        "$1 off Dole Fruit Bars, 6 ct.",
        "H-E-B Fruit Bars, 6 ct.",
        "coupon",
        False,
        "brand conflict",
    ),
    (
        "$1 off Hill Country Fare Whole Milk, 1 gal",
        "H-E-B Whole Milk, 1 gal",
        "coupon",
        False,
        "brand conflict",
    ),
    # -- quantity qualifiers: "buy 2" must name the product exactly --------
    (
        "$1 off 2 H-E-B Tortilla Chips or Pork Rinds, 4 - 14 oz.",
        "H-E-B White Corn Tortilla Chips, 14 oz",
        "coupon",
        False,
        "quantity-gated",
    ),
    # -- live-run false positives rejected below the overlap threshold -----
    # (kept as behavioral guards; the mechanism is the catalog score)
    (
        "$1.50 off Pete and Gerry's Organic Large Brown Eggs, 12 ct.",
        "Sunups Grade A Large White Eggs, 12 ct",
        "coupon",
        False,
        None,
    ),
    (
        "H-E-B Seasoned Chicken Breast for Fajitas, Avg. 3.95 lbs",
        "H-E-B Natural Boneless Chicken Breast, Thin Sliced, Avg. 1.7 lbs",
        "ad",
        False,
        None,
    ),
    (
        "$2 off H-E-B Texas Pets Adult Complete Chicken & Beef Flavor Dry Dog Food, 50 lb.",
        "H-E-B Natural Boneless Chicken Breasts, Avg. 2.85 lbs",
        "coupon",
        False,
        None,
    ),
    (
        "$1 off Thomas' Bagels, 6 ct.",
        "H-E-B Round Top Large White Enriched Sliced Bread, 20 oz",
        "coupon",
        False,
        None,
    ),
    (
        "$3 off 2 Oatly Oat Milk, 64 oz.",
        "H-E-B Whole Milk, 1 gal",
        "coupon",
        False,
        None,
    ),
    # -- positive controls: true matches that must keep working ------------
    (
        "$1.00 OFF ANY ONE (1) H-E-B Large White Eggs 12 ct",
        "H-E-B Large White Eggs 12 ct",
        "coupon",
        True,
        None,
    ),
    (
        "$1.50 off Pete and Gerry's Organic Large Brown Eggs, 12 ct.",
        "Pete and Gerry's Organic Large Brown Eggs, 12 ct",
        "coupon",
        True,
        None,
    ),
    (
        # Pack size is not distinguishing: same line, different count.
        "Pete and Gerry's Organic Large Brown Eggs, 12 ct",
        "Pete and Gerry's Organic Large Brown Eggs, 18 ct",
        "ad",
        True,
        None,
    ),
    (
        # Quantity-gated rows still match when they name the product
        # exactly; the optimizer values per unit with a "buy 2" note.
        "SAVE $1.50 OFF 2 H-E-B Casa Magnifica Yellow Corn Tortilla Chips",
        "H-E-B Casa Magnifica Yellow Corn Tortilla Chips 11 oz",
        "coupon",
        True,
        None,
    ),
    (
        "H-E-B Whole Milk 1 gal",
        "H-E-B Whole Milk, 1 gal",
        "ad",
        True,
        None,
    ),
    (
        "Fresh Bunch of Bananas, 4-7 Bananas, Avg. 2.4 lbs",
        "Fresh Bunch of Bananas, 4-7 Bananas, Avg. 2.4 lbs",
        "ad",
        True,
        None,
    ),
    (
        "50¢ off H-E-B Fruit Bars, 6 ct.",
        "H-E-B Fruit Bars, 6 ct.",
        "coupon",
        True,
        None,
    ),
    (
        # Same line, different size: the ad names the product.
        "H-E-B Seasoned Chicken Breast for Fajitas, Avg. 3.95 lbs",
        "H-E-B Seasoned Chicken Breast for Fajitas, Avg. 2.1 lbs",
        "ad",
        True,
        None,
    ),
    (
        "$1 off H-E-B Fresh Salsa - Mild, 16 oz",
        "H-E-B Fresh Salsa - Mild, 16 oz",
        "coupon",
        True,
        None,
    ),
    (
        # Exact product on a "buy 2 get free" bundle: matches; the bundle
        # is surfaced as a note, not a phantom discount.
        "Buy 2 H-E-B Wavy Potato Chips, 9 oz. get FREE H-E-B Round Top Large White Bread, 20 oz.",
        "H-E-B Wavy Potato Chips, 9 oz.",
        "coupon",
        True,
        None,
    ),
    (
        # The yogurt coupon is real -- it just belongs on yogurt.
        "$1 off H-E-B Whole Milk Plain Greek Yogurt, 32 oz.",
        "H-E-B Whole Milk Plain Greek Yogurt, 32 oz.",
        "coupon",
        True,
        None,
    ),
]


def _offers_for(kind: str, text: str):
    """The deal row as the matcher sees it, via the real parsers."""

    class _Client:
        def weekly_ad(self):
            if kind == "ad":
                return [
                    WeeklyAdItem(
                        name=text, url="", sale_price=Decimal("2.00")
                    )
                ]
            return []

        def list_coupons(self, department=None, *, max_pages: int = 20):
            if kind == "coupon":
                return [CouponOffer(headline=text)]
            return []

    offers = collect_deals(_Client())
    assert offers, f"row parsed to no promotion: {text!r}"
    return offers


def _item(product_name: str) -> LocatedItem:
    slug = product_name.lower().replace(" ", "-")
    return LocatedItem(
        query=product_name,
        product=ProductResult(
            item_id="1",
            name=product_name,
            url=f"https://www.heb.com/product-detail/{slug}/1",
            price=Decimal("2.00"),
            location="Aisle 10",
        ),
        location=parse_location("Aisle 10"),
        unplaced=False,
    )


def _case_id(case: tuple) -> str:
    offer, product, _kind, expect, _pin = case
    verdict = "match" if expect else "nomatch"
    short = product[:28].replace(" ", "_")
    return f"{verdict}-{short}"


@pytest.mark.parametrize("case", MATRIX, ids=_case_id)
def test_matrix(case: tuple[str, str, str, bool, str | None]) -> None:
    offer_text, product_name, kind, expect, _pin = case
    offers = _offers_for(kind, offer_text)
    assignment = match_deals_to_items([_item(product_name)], offers)
    ((_, matches),) = assignment.per_item
    if expect:
        assert matches, (
            f"expected a match but got none: {offer_text!r} vs {product_name!r}"
        )
    else:
        assert not matches, (
            f"expected NO match but got {[m.offer.name for m in matches]}: "
            f"{offer_text!r} vs {product_name!r}"
        )


@pytest.mark.parametrize(
    "case", [c for c in MATRIX if c[4] is not None], ids=_case_id
)
def test_matrix_veto_mechanism(
    case: tuple[str, str, str, bool, str | None],
) -> None:
    """Pin the mechanism: the veto must fire with the expected reason."""
    offer_text, product_name, _kind, _expect, pin = case
    assert pin is not None
    flyer = catalog_mod.FlyerOffer(
        flyer_item_id=None,
        name=offer_text,
        promotions=(),
        terms=catalog_mod._terms(offer_text),
        is_brand_family=False,
    )
    candidates = catalog_mod.match_query(product_name, [flyer])
    assert candidates, (
        f"veto pin {pin!r} needs a catalog candidate: "
        f"{offer_text!r} vs {product_name!r}"
    )
    best = candidates[0]
    reason = veto_reason(
        best.term,
        offer_name=offer_text,
        product_name=product_name,
        score=best.score,
    )
    assert reason is not None, (
        f"expected veto {pin!r} but no veto fired: "
        f"{offer_text!r} vs {product_name!r}"
    )
    assert pin in reason, (
        f"expected veto {pin!r} but got {reason!r}: "
        f"{offer_text!r} vs {product_name!r}"
    )
