"""Tests for the H-E-B browser parsing layer.

Fixtures are small representative pages authored from the 2026-10-04 recon
notes -- they mirror the documented page structures but were not scraped from
the live site. If the live selectors drift, update SELECTORS in
dealforge.providers.heb.parse and refresh these fixtures.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from dealforge.providers.heb.parse import (
    parse_coupons_page,
    parse_product_page,
    parse_search_results,
    parse_weekly_ad,
)

FIXTURES = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# search results
# ---------------------------------------------------------------------------


def test_search_results_parse_cards():
    results = parse_search_results(read("heb_search_eggs.html"))
    assert len(results) == 3

    first = results[0]
    assert first.item_id == "8271501"
    assert first.name == "H-E-B Grade AA Cage Free Extra Large Brown Eggs 18 ct"
    assert first.price == Decimal("4.79")
    assert first.price_unit == "each"
    assert first.unit_price_text == "($0.27/ct)"
    assert first.location == "In Dairy on the Left Wall, A25 at The Heights H-E-B"
    assert first.snap_eligible is True
    assert first.url == "https://www.heb.com/product-detail/h-e-b-grade-aa-cage-free-extra-large-brown-eggs-18-ct/8271501"


def test_search_results_location_verbatim_and_limit():
    results = parse_search_results(read("heb_search_eggs.html"), limit=2)
    assert len(results) == 2
    assert results[1].location == "In Dairy on the Left Wall, A24"
    assert results[1].price == Decimal("2.86")


def test_search_results_missing_fields_do_not_crash():
    results = parse_search_results(read("heb_search_eggs.html"))
    third = results[2]
    assert third.location == "Aisle 10"
    assert third.unit_price_text is None
    assert third.snap_eligible is False


# ---------------------------------------------------------------------------
# product detail
# ---------------------------------------------------------------------------


def test_product_page_fields():
    url = "https://www.heb.com/product-detail/h-e-b-grade-aa-cage-free-extra-large-brown-eggs-18-ct/8271501"
    detail = parse_product_page(read("heb_product_8271501.html"), url)
    assert detail.item_id == "8271501"
    assert detail.price == Decimal("4.79")
    assert detail.location == "In Dairy on the Left Wall, A25 at The Heights H-E-B"
    assert detail.deal_badges == ("Coupon",)
    assert detail.more_ways_to_save == (
        "Earn 5% cash back on this item with the H-E-B Visa Signature Credit Card",
    )
    assert "SNAP EBT eligible" in detail.highlights
    assert detail.snap_eligible is True


# ---------------------------------------------------------------------------
# coupons
# ---------------------------------------------------------------------------


def test_coupons_page_parses_headlines():
    offers = parse_coupons_page(read("heb_coupons.html"))
    assert len(offers) == 3
    assert offers[0].headline.startswith("$8.00 OFF ANY TWO (2)")
    assert offers[0].expiry == "Expires 10/17/2026"
    assert offers[0].limit == "Limit 1 per customer"
    assert offers[0].redeem_channel == "Redeem in store"
    assert offers[1].department == "Grocery"


def test_coupon_to_promotion_forces_clip_and_store_source():
    from dealforge.models import PromoKind, PromoSource

    offers = parse_coupons_page(read("heb_coupons.html"))
    promos = offers[0].to_promotion()
    assert len(promos) == 1
    p = promos[0]
    assert p.kind is PromoKind.SAVE_FLAT
    assert p.discount_amount == Decimal("8.00")
    assert p.min_quantity == 2
    assert p.requires_clip is True
    assert p.source is PromoSource.STORE_COUPON


# ---------------------------------------------------------------------------
# weekly ad
# ---------------------------------------------------------------------------


def test_weekly_ad_items():
    items = parse_weekly_ad(read("heb_weekly_ad.html"))
    assert len(items) == 3

    avo = items[0]
    assert avo.name == "Hass Avocados"
    assert avo.sale_price == Decimal("2.97")
    assert avo.regular_price == Decimal("3.57")
    assert avo.location == "In Produce, B4"
    assert avo.coupon_tags == ("Coupon available",)
    assert avo.expiry == "Expires Tuesday"

    promo = avo.to_promotion()[0]
    assert promo.unit_price == Decimal("2.97")
    assert promo.discount_amount == Decimal("0.60")


def test_weekly_ad_deal_mechanics_parse():
    from dealforge.models import PromoKind

    items = parse_weekly_ad(read("heb_weekly_ad.html"))

    combo = items[1]
    assert combo.deal_text == "Combo Loco — Buy this, get that free"
    promos = combo.to_promotion()
    assert promos and promos[0].kind is PromoKind.BUNDLE

    chicken = items[2]
    promos = chicken.to_promotion()
    assert promos and promos[0].kind is PromoKind.BOGO
    assert promos[0].min_quantity == 2
    assert promos[0].get_quantity == 1
