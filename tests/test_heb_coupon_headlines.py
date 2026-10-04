"""Tests for digital-coupon headline grammar in the H-E-B parser.

Coupon headlines use a different grammar than flyer copy: they never say SAVE
("$8.00 OFF ANY TWO (2) ..."), and weekly-ad deal mechanics add shapes like
"Buy 2, get 1 free". Every string here mirrors real H-E-B copy shapes observed
2026-10-04.
"""

from __future__ import annotations

from decimal import Decimal

from dealforge.models import PromoKind, PromoSource
from dealforge.providers.heb.sale_story import parse_coupon_headline, parse_text


def one(blob: str, **kwargs):
    promos = parse_text(blob, **kwargs)
    assert len(promos) == 1, f"expected 1 promotion, got {len(promos)}: {promos}"
    return promos[0]


def test_coupon_dollars_off_any_two():
    p = one("$8.00 OFF ANY TWO (2) L'OREAL PARIS Superior Preference Hair Color Products")
    assert p.kind is PromoKind.SAVE_FLAT
    assert p.discount_amount == Decimal("8.00")
    assert p.min_quantity == 2


def test_coupon_dollars_off_two_lowercase():
    p = one("$1 off 2 H-E-B Tortilla Chips, Salsa or Hill Country Fare Salsa Con Queso, 11 - 16 oz., assorted varieties (located on the chips aisle)")
    assert p.kind is PromoKind.SAVE_FLAT
    assert p.discount_amount == Decimal("1")
    assert p.min_quantity == 2


def test_coupon_headline_forces_clip_and_store_coupon():
    promos = parse_coupon_headline("$1 off 2 H-E-B Tortilla Chips")
    assert len(promos) == 1
    assert promos[0].requires_clip is True
    assert promos[0].source is PromoSource.STORE_COUPON


def test_flyer_save_off_any_two_keeps_quantity():
    """The SAVE form also carries the trailing quantity now."""
    p = one("SAVE $8 OFF ANY TWO (2) select shampoo")
    assert p.kind is PromoKind.SAVE_FLAT
    assert p.discount_amount == Decimal("8")
    assert p.min_quantity == 2


def test_buy_n_get_m_free():
    p = one("Buy 2, get 1 free")
    assert p.kind is PromoKind.BOGO
    assert p.min_quantity == 2
    assert p.get_quantity == 1
    assert p.discount_percent == Decimal(100)


def test_combo_loco_buy_this_get_that():
    p = one("Combo Loco — Buy this, get that free")
    assert p.kind is PromoKind.BUNDLE
    assert p.get_quantity == 1
    assert p.confidence < 1.0
    assert p.get_free_item is None


def test_free_with_purchase():
    p = one("FREE! with the purchase of any two participating items")
    assert p.kind is PromoKind.BUNDLE
    assert p.confidence < 1.0


def test_basket_offer_still_wins_over_coupon_flat():
    p = one("WHEN YOU BUY $40 OF Huggies SAVE $10 OFF YOUR BASKET")
    assert p.kind is PromoKind.BASKET_THRESHOLD
    assert p.basket_level is True


def test_existing_flyer_grammar_unchanged():
    p = one("SAVE $6 with yellow coupon in-store or online WHEN YOU BUY 2")
    assert p.kind is PromoKind.SAVE_FLAT
    assert p.discount_amount == Decimal("6")
    assert p.min_quantity == 2
    assert p.requires_clip is True
