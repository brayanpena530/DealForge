"""Tests for the H-E-B sale_story parser.

Every string in this file is real ad copy, captured from the H-E-B weekly ad for
77008 (Houston) for the week of 2026-09-16. The full 55-item flyer is in
`fixtures/heb_flyer_8130390.json`.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from dealforge.models import PromoKind
from dealforge.providers.heb.sale_story import parse_item, parse_text

FIXTURE = Path(__file__).parent / "fixtures" / "heb_flyer_8130390.json"


@pytest.fixture(scope="module")
def flyer_items() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["items"]


def one(blob: str, **kwargs):
    """Parse a blob expected to yield exactly one promotion."""
    promos = parse_text(blob, **kwargs)
    assert len(promos) == 1, f"expected 1 promotion, got {len(promos)}: {promos}"
    return promos[0]


# ---------------------------------------------------------------------------
# each promo shape in the flyer
# ---------------------------------------------------------------------------


def test_flat_dollars_off_with_quantity_threshold():
    p = one("SAVE $6 with yellow coupon in-store or online WHEN YOU BUY 2")
    assert p.kind is PromoKind.SAVE_FLAT
    assert p.discount_amount == Decimal("6")
    assert p.min_quantity == 2
    assert p.requires_clip is True


def test_flat_dollars_off_with_threshold_stated_first():
    """Same rule, opposite word order -- the ad uses both."""
    p = one("SAVE $3 WHEN YOU BUY 2 with yellow coupon in-store or online")
    assert p.kind is PromoKind.SAVE_FLAT
    assert p.discount_amount == Decimal("3")
    assert p.min_quantity == 2


def test_percent_off():
    p = one("SAVE 20% with yellow coupon in-store or online")
    assert p.kind is PromoKind.SAVE_PERCENT
    assert p.discount_percent == Decimal("20")


def test_bare_percent_off():
    p = one("10% off")
    assert p.kind is PromoKind.SAVE_PERCENT
    assert p.discount_percent == Decimal("10")
    assert p.requires_clip is False


@pytest.mark.parametrize(
    "blob,qty,total,unit",
    [
        ("4 for 10.00 with yellow coupon in-store or online WHEN YOU BUY 4", 4, "10.00", "2.50"),
        ("2 for 7.00 with yellow coupon in-store or online WHEN YOU BUY 2", 2, "7.00", "3.50"),
        ("2 for 4.00 with yellow coupon in-store or online WHEN YOU BUY 2", 2, "4.00", "2.00"),
    ],
)
def test_multibuy_pricing_divides_out_unit_price(blob, qty, total, unit):
    p = one(blob)
    assert p.kind is PromoKind.N_FOR_M
    assert p.bundle_quantity == qty
    assert p.discount_amount == Decimal(total)
    assert p.unit_price == Decimal(unit)
    assert p.min_quantity == qty


def test_bogo_same_product():
    p = one("BUY one (1) BODYARMOR Sports Drink 16 oz. btl. GET 2ND FREE! with yellow coupon")
    assert p.kind is PromoKind.BOGO
    assert p.min_quantity == 1
    assert p.get_quantity == 1


def test_bundle_names_the_free_item():
    p = one("BUY Mug Root Beer GET FREE! H-E-B Creamy Creations Ice Cream, SAVE up to $2.48!")
    assert p.kind is PromoKind.BUNDLE
    assert p.get_quantity == 1
    assert p.get_free_item is not None
    assert "Creamy Creations" in p.get_free_item
    assert p.max_value == Decimal("2.48")


def test_bundle_with_spend_threshold():
    p = one(
        "BUY $12 of Meal Simple Microwaveable Meals, 10.5 - 14 oz. assorted varieties "
        "GET FREE! with yellow coupon in-store or online H-E-B Chopped Salad Bowl "
        "5.75 - 7.25 oz. assorted varieties SAVE up to $3."
    )
    assert p.kind is PromoKind.BUNDLE
    assert p.min_spend == Decimal("12")
    assert p.max_value == Decimal("3")


def test_bundle_giving_multiple_free_items():
    p = one(
        "get 4 items free! with yellow coupons in-store or online "
        "H-E-B Black or Pinto Beans, 27 oz. H-E-B Shredded Iceberg Lettuce, 16 oz."
    )
    assert p.kind is PromoKind.BUNDLE
    assert p.get_quantity == 4


def test_per_pound_savings():
    p = one("12.97 lb. SAVE up to $2 per lb.", unit_price=Decimal("12.97"))
    assert p.kind is PromoKind.SAVE_PER_LB
    assert p.discount_amount == Decimal("2")
    assert p.unit_price == Decimal("12.97")
    assert p.confidence < 1.0, "an 'up to' claim is not a guaranteed saving"


# ---------------------------------------------------------------------------
# basket-level offers -- the piggyback case
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blob,spend,discount",
    [
        (
            "WHEN YOU BUY $40 OF SAVE $10 OFF YOUR BASKET with yellow coupon in-store or "
            "online WHEN YOU BUY $40 OF Huggies Diapers, Little Swimmers, Wipes",
            "40",
            "10",
        ),
        (
            "when you buy $9 of TRESemme Hair Care Items Save $3 off your basket "
            "with yellow coupon in-store or online",
            "9",
            "3",
        ),
        (
            "WHEN YOU BUY $20 OF H-E-B Mi Tienda Items SAVE $4 OFF YOUR BASKET "
            "with yellow coupon in-store or online",
            "20",
            "4",
        ),
        (
            "SAVE $5 OFF YOUR BASKET with yellow coupon in-store or online WHEN YOU BUY $25",
            "25",
            "5",
        ),
    ],
)
def test_basket_threshold(blob, spend, discount):
    p = one(blob)
    assert p.kind is PromoKind.BASKET_THRESHOLD
    assert p.min_spend == Decimal(spend)
    assert p.discount_amount == Decimal(discount)
    assert p.basket_level is True


def test_basket_offers_are_stackable_item_offers_are_not():
    """H-E-B policy: one coupon per item, but $-off-basket piggybacks."""
    basket = one("SAVE $10 OFF YOUR BASKET WHEN YOU BUY $40 OF Huggies")
    item = one("SAVE $2 with yellow coupon in-store or online")
    assert basket.stackable is True
    assert item.stackable is False


def test_basket_offer_without_stated_threshold_is_flagged():
    p = one("SAVE $3 OFF YOUR BASKET with yellow coupon in-store or online")
    assert p.kind is PromoKind.BASKET_THRESHOLD
    assert p.min_spend is None
    assert p.confidence < 1.0
    assert p.caveats


def test_basket_discount_is_not_mistaken_for_an_item_discount():
    """Regression: 'SAVE $10 OFF YOUR BASKET' also matches the plain SAVE $X rule."""
    p = one("SAVE $10 OFF YOUR BASKET WHEN YOU BUY $40 OF Huggies")
    assert p.kind is not PromoKind.SAVE_FLAT
    assert p.basket_level is True


# ---------------------------------------------------------------------------
# price semantics
# ---------------------------------------------------------------------------


def test_stated_price_wins_and_saving_is_not_applied_twice():
    """`5.96 ea. ... SAVE $1` means 5.96 IS the post-coupon price.

    Emitting SAVE_FLAT here would let a pricing engine subtract the $1 again.
    """
    p = one("5.96 ea. with yellow coupon in-store or online SAVE $1", unit_price=Decimal("5.96"))
    assert p.kind is PromoKind.SALE_PRICE
    assert p.unit_price == Decimal("5.96")
    assert p.discount_amount == Decimal("1"), "stated saving is retained as metadata"


def test_price_with_no_offer():
    p = one("1.97 ea.", unit_price=Decimal("1.97"))
    assert p.kind is PromoKind.SALE_PRICE
    assert p.unit_price == Decimal("1.97")
    assert p.discount_amount is None


def test_price_fragments_are_not_read_as_prices():
    """`price_text` also carries non-price fragments."""
    for fragment in ("WHEN YOU BUY $40 OF", "SAVE up to $3.48!"):
        item = {"name": "x", "price_text": fragment, "sale_story": None}
        for p in parse_item(item):
            assert p.unit_price is None, f"{fragment!r} parsed as a price"


def test_empty_input_yields_nothing():
    assert parse_text("") == []
    assert parse_text("   ") == []
    assert parse_item({"name": "Bananas"}) == []


def test_promo_fields_are_joined_across_flipp_columns():
    """The same offer is split across columns inconsistently by the source."""
    split = parse_item(
        {"price_text": "5.96 ea. with yellow coupon in-store or online", "sale_story": "SAVE $1"}
    )
    whole = parse_item(
        {"price_text": "5.96 ea.", "sale_story": "SAVE $1 with yellow coupon in-store or online"}
    )
    assert split[0].kind is whole[0].kind
    assert split[0].unit_price == whole[0].unit_price == Decimal("5.96")
    assert split[0].requires_clip is whole[0].requires_clip is True


# ---------------------------------------------------------------------------
# whole-flyer invariants
# ---------------------------------------------------------------------------


def test_every_flyer_item_parses(flyer_items):
    assert len(flyer_items) == 55
    unparsed = [i["name"] for i in flyer_items if not parse_item(i)]
    assert not unparsed, f"{len(unparsed)} items produced no promotion: {unparsed[:5]}"


def test_no_flyer_item_falls_through_to_unknown(flyer_items):
    unknown = [
        (i["name"][:50], p.raw[:90])
        for i in flyer_items
        for p in parse_item(i)
        if p.kind is PromoKind.UNKNOWN
    ]
    assert not unknown, f"unmatched promo copy: {unknown}"


def test_flyer_shape_distribution(flyer_items):
    """Locks in the measured mix, so a regex change that silently reclassifies
    offers fails loudly rather than quietly shifting the optimizer's inputs."""
    counts: dict[str, int] = {}
    for item in flyer_items:
        for p in parse_item(item):
            counts[p.kind.value] = counts.get(p.kind.value, 0) + 1
    assert counts == {
        "sale_price": 18,
        "save_flat": 11,
        "bundle": 9,
        "basket_threshold": 5,
        "save_percent": 4,
        "n_for_m": 3,
        "save_per_lb": 3,
        "bogo": 2,
    }


def test_all_money_is_decimal(flyer_items):
    """Floats in a pricing engine are a correctness bug waiting to happen."""
    for item in flyer_items:
        for p in parse_item(item):
            for value in (p.discount_amount, p.unit_price, p.min_spend, p.max_value):
                assert value is None or isinstance(value, Decimal)


def test_raw_copy_is_always_retained(flyer_items):
    """Ad footnotes carry exclusions the parser does not model, so a plan must
    always be able to show the user the original wording."""
    for item in flyer_items:
        for p in parse_item(item):
            assert p.raw.strip()
