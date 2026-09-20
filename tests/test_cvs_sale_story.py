"""Tests for the CVS sale_story parser.

Every string in this file is real ad copy, captured from the CVS weekly ad for
77008 (Houston) for the week of 2026-09-20. The full 293-item flyer is in
`fixtures/cvs_flyer_8139677.json`.

The defining difference from H-E-B: one CVS string usually carries several
offers that apply together. Most of what is tested here is that they all come
out, in the right order, with the right issuer, and that the loyalty reward
never turns into a discount.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from dealforge.models import PromoKind, PromoSource
from dealforge.providers.cvs.sale_story import parse_item, parse_text

FIXTURE = Path(__file__).parent / "fixtures" / "cvs_flyer_8139677.json"


@pytest.fixture(scope="module")
def flyer_items() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["items"]


def one(blob: str, **kwargs):
    """Parse a blob expected to yield exactly one promotion."""
    promos = parse_text(blob, **kwargs)
    assert len(promos) == 1, f"expected 1 promotion, got {len(promos)}: {promos}"
    return promos[0]


# ---------------------------------------------------------------------------
# ExtraBucks -- the reason CVS needs a lambda term at all
# ---------------------------------------------------------------------------


def test_spend_threshold_earns_extrabucks():
    p = one("Spend $20 get $5 ExtraBucks Rewards®◆ WITH CARD")
    assert p.kind is PromoKind.EXTRABUCKS
    assert p.min_spend == Decimal("20")
    assert p.reward_amount == Decimal("5")
    assert p.basket_level is True
    assert p.source is PromoSource.LOYALTY_REWARD


def test_quantity_threshold_earns_extrabucks():
    p = one("Buy 2 get $6 ExtraBucks Rewards®⧫ WITH CARD")
    assert p.kind is PromoKind.EXTRABUCKS
    assert p.min_quantity == 2
    assert p.reward_amount == Decimal("6")
    assert p.basket_level is False, "a buy-N reward is tied to the item, not the order"


def test_extrabucks_are_never_a_discount():
    """The single most important semantic in this parser.

    ExtraBucks are future currency worth lambda x face value. An engine that
    reads them out of `discount_amount` would subtract them from today's bill
    and report a price the shopper will not actually pay.
    """
    for blob in (
        "Spend $30 get $10 ExtraBucks Rewards®◆ WITH CARD",
        "Buy 1 get $10 ExtraBucks Rewards®⧫ WITH CARD",
        "Buy 2 get $15 ExtraBucks Rewards®⧫ WITH CARD",
    ):
        p = one(blob)
        assert p.discount_amount is None
        assert p.discount_percent is None
        assert p.reward_amount is not None
        assert p.reduces_cash_today is False


def test_extrabucks_spelling_variants_all_parse():
    """The ad is inconsistent about case, the registered mark and word order."""
    variants = [
        "Spend $15 get $5 ExtraBucks Rewards®⧫ WITH CARD",
        "Spend $45 get $15 Extrabucks Rewards®⧫ WITH CARD",
        "Spend $18 Get $5 ExtraBucks Rewards®◆ WITH CARD",
        "$3.00 Digital mfr coupon + Spend $15 get $5 ExtraBucks®⧫ Rewards WITH CARD",
    ]
    for blob in variants:
        eb = [p for p in parse_text(blob) if p.kind is PromoKind.EXTRABUCKS]
        assert len(eb) == 1, f"no ExtraBucks parsed from {blob!r}"
        assert eb[0].reward_amount is not None


# ---------------------------------------------------------------------------
# buy-one-get-one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blob,buy,get,pct",
    [
        ("Buy 1 get 1 FREE* WITH CARD", 1, 1, "100"),
        ("Buy 1 get 1 50% OFF* WITH CARD", 1, 1, "50"),
        ("Buy 1 get 1 40% OFF* WITH CARD", 1, 1, "40"),
        ("Buy 2 get 2 FREE* WITH CARD", 2, 2, "100"),
        ("BOGO FREE* WITH CARD", 1, 1, "100"),
        ("BOGO 50% off* WITH CARD", 1, 1, "50"),
        ("Mix & Match BOGO 50% Off WITH CARD", 1, 1, "50"),
    ],
)
def test_bogo_shapes(blob, buy, get, pct):
    p = one(blob)
    assert p.kind is PromoKind.BOGO
    assert p.min_quantity == buy
    assert p.get_quantity == get
    assert p.discount_percent == Decimal(pct)


def test_ordinal_get_is_one_item_not_n_items():
    """`Buy 2 get 3rd FREE` gives you one free item -- the third one.

    Reading the ordinal as a count would make this three times as good as it is.
    """
    p = one("Buy 2 get 3rd FREE** WITH CARD + CRV")
    assert p.min_quantity == 2
    assert p.get_quantity == 1


def test_bogo_percent_is_not_read_as_a_flat_percent_off():
    """Regression: `Buy 1 get 1 50% OFF` contains `50% OFF`.

    Emitting SAVE_PERCENT as well would halve the price of every unit.
    """
    promos = parse_text("Buy 1 get 1 50% OFF* WITH CARD")
    assert [p.kind for p in promos] == [PromoKind.BOGO]


def test_impossible_percentage_in_the_ad_is_flagged_not_trusted():
    """The live flyer really does say `Buy 1 get 1 140% OFF*`."""
    p = one("Buy 1 get 1 140% OFF* WITH CARD")
    assert p.discount_percent == Decimal("140")
    assert p.confidence == 0.0
    assert p.caveats


# ---------------------------------------------------------------------------
# coupons and their issuers
# ---------------------------------------------------------------------------


def test_manufacturer_coupon():
    p = one("$1.00 Digital mfr coupon")
    assert p.kind is PromoKind.SAVE_FLAT
    assert p.discount_amount == Decimal("1.00")
    assert p.source is PromoSource.MFR_COUPON
    assert p.requires_clip is True


def test_manufacturer_coupon_with_quantity_requirement():
    p = one("$5.00 on 3 Digital mfr coupon")
    assert p.discount_amount == Decimal("5.00")
    assert p.min_quantity == 3


def test_manufacturer_coupon_with_no_stated_value_is_kept_but_flagged():
    """Some rows advertise a coupon whose value only appears in the app."""
    promos = parse_text("Digital mfr coupon + Buy 2 get $7 ExtraBucks Rewards®⧫ WITH CARD")
    coupon = next(p for p in promos if p.source is PromoSource.MFR_COUPON)
    assert coupon.discount_amount is None
    assert coupon.confidence < 1.0
    assert coupon.caveats


def test_extracare_coupon_is_a_store_coupon_not_a_manufacturer_one():
    """CVS's one-per-item limits are per issuer, so the two cannot be merged."""
    p = one("Get a $3 off 2 ExtraCare coupon^ in the CVS app")
    assert p.kind is PromoKind.SAVE_FLAT
    assert p.discount_amount == Decimal("3")
    assert p.min_quantity == 2
    assert p.source is PromoSource.STORE_COUPON


# ---------------------------------------------------------------------------
# prices
# ---------------------------------------------------------------------------


def test_sale_price_with_card():
    p = one("1.99 WITH CARD", unit_price=Decimal("1.99"))
    assert p.kind is PromoKind.SALE_PRICE
    assert p.unit_price == Decimal("1.99")
    assert p.requires_card is True


@pytest.mark.parametrize(
    "blob,qty,total,unit",
    [
        ("2/ 9.00 or reg retail ea. WITH CARD", 2, "9.00", "4.50"),
        ("2/ 3.00 or reg retail ea. WITH CARD", 2, "3.00", "1.50"),
        ("2/ 14.00 WITH CARD or reg retail ea.", 2, "14.00", "7.00"),
    ],
)
def test_multibuy_divides_out_unit_price(blob, qty, total, unit):
    p = one(blob)
    assert p.kind is PromoKind.N_FOR_M
    assert p.bundle_quantity == qty
    assert p.discount_amount == Decimal(total)
    assert p.unit_price == Decimal(unit)


def test_multibuy_records_the_single_unit_fallback_price():
    """`2/ 9.00 or $5.49 ea.` -- buying one is not half of nine."""
    p = one("2/ 9.00 or $5.49 ea. WITH CARD")
    assert p.unit_price == Decimal("4.50")
    assert any("5.49" in c for c in p.caveats)


def test_multibuy_does_not_also_emit_a_sale_price():
    """Regression: `2/ 9.00` must not be read as a $2 unit price as well."""
    promos = parse_item({"price_text": "2/ 9.00", "post_price_text": "WITH CARD"})
    assert [p.kind for p in promos] == [PromoKind.N_FOR_M]


def test_standalone_percent_off():
    p = one("25% OFF WITH CARD")
    assert p.kind is PromoKind.SAVE_PERCENT
    assert p.discount_percent == Decimal("25")


# ---------------------------------------------------------------------------
# several offers in one string -- the CVS-specific case
# ---------------------------------------------------------------------------


def test_coupon_and_extrabucks_both_come_out():
    promos = parse_text(
        "$5.00 on 3 Digital mfr coupon + Buy 2 get $4 Extrabucks Rewards®⧫ WITH CARD"
    )
    assert [p.kind for p in promos] == [PromoKind.SAVE_FLAT, PromoKind.EXTRABUCKS]
    assert promos[0].discount_amount == Decimal("5.00")
    assert promos[1].reward_amount == Decimal("4")


def test_price_coupon_and_extrabucks_all_three():
    promos = parse_item(
        {
            "price_text": "5.99",
            "post_price_text": "WITH CARD",
            "sale_story": "Also get savings with $5.00 on 2 Digital mfr coupon "
            "+ Buy 2 get $6 ExtraBucks Rewards®⧫",
        }
    )
    assert [p.kind for p in promos] == [
        PromoKind.SALE_PRICE,
        PromoKind.SAVE_FLAT,
        PromoKind.EXTRABUCKS,
    ]
    assert promos[0].unit_price == Decimal("5.99")
    assert promos[2].reward_amount == Decimal("6")


def test_bogo_stacks_with_a_coupon_and_a_reward():
    promos = parse_text(
        "Buy 1 get 1 40% OFF* Also get savings with $3.00 on 2 Digital mfr coupon "
        "+ Spend $25 get $8 ExtraBucks Rewards®◆ WITH CARD"
    )
    kinds = [p.kind for p in promos]
    assert kinds == [PromoKind.BOGO, PromoKind.SAVE_FLAT, PromoKind.EXTRABUCKS]


def test_offers_come_out_in_the_order_the_ad_states_them():
    """A plan reads back to the shopper in ad order, so order is part of the API."""
    promos = parse_text(
        "$4.00 Digital mfr coupon + Spend $20 get $8 ExtraBucks Rewards WITH CARD"
    )
    assert [p.source for p in promos] == [
        PromoSource.MFR_COUPON,
        PromoSource.LOYALTY_REWARD,
    ]


def test_every_cvs_offer_is_marked_stackable():
    """CVS allows a sale, a coupon and a reward on one item; the real limit is
    one offer per issuer, which `source` expresses."""
    promos = parse_text(
        "12.99 WITH CARD Also get savings with $5.00 on 2 Digital mfr coupon "
        "+ Buy 2 get $4 ExtraBucks Rewards®◆",
    )
    assert promos and all(p.stackable for p in promos)


# ---------------------------------------------------------------------------
# rows that are not offers
# ---------------------------------------------------------------------------


def test_category_header_rows_yield_nothing():
    """82 of the 293 rows are page furniture -- section headers with no price."""
    for name in ("Makeup & Nails", "Hair Care", "Extra Big Deals", "Oral Care"):
        assert parse_item({"name": name}) == []


def test_non_offer_copy_yields_nothing():
    assert parse_text("Get your flu shot today") == []
    assert parse_text("WITH CARD") == []
    assert parse_text("") == []


# ---------------------------------------------------------------------------
# whole-flyer invariants
# ---------------------------------------------------------------------------


def test_flyer_size(flyer_items):
    assert len(flyer_items) == 293


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
        "extrabucks": 98,
        "bogo": 79,
        "save_flat": 62,
        "sale_price": 59,
        "n_for_m": 11,
        "save_percent": 6,
    }


def test_multi_offer_strings_are_the_norm_not_the_exception(flyer_items):
    """If this ever drops to ~1.0, the scanner has stopped finding the
    secondary offers and every plan will understate the savings."""
    parsed = [parse_item(i) for i in flyer_items]
    with_offers = [p for p in parsed if p]
    total = sum(len(p) for p in with_offers)
    assert len(with_offers) == 211
    assert total / len(with_offers) > 1.4


def test_all_money_is_decimal(flyer_items):
    """Floats in a pricing engine are a correctness bug waiting to happen."""
    for item in flyer_items:
        for p in parse_item(item):
            for value in (p.discount_amount, p.unit_price, p.min_spend, p.reward_amount):
                assert value is None or isinstance(value, Decimal)


def test_no_promotion_carries_both_a_discount_and_a_reward(flyer_items):
    """They are different currencies. A single Promotion holding both would let
    an engine add them together."""
    for item in flyer_items:
        for p in parse_item(item):
            assert not (p.discount_amount and p.reward_amount)


def test_raw_copy_is_always_retained(flyer_items):
    """Ad footnotes carry exclusions the parser does not model, so a plan must
    always be able to show the user the original wording."""
    for item in flyer_items:
        for p in parse_item(item):
            assert p.raw.strip()
