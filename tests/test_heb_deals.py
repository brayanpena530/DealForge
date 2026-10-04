"""Tests for deal collection and item matching (offline, fake client)."""

from __future__ import annotations

from decimal import Decimal

from dealforge.models import PromoKind
from dealforge.providers.heb.deals import (
    collect_deals,
    match_deals_to_items,
)
from dealforge.providers.heb.location import parse_location
from dealforge.providers.heb.route import LocatedItem
from dealforge.providers.heb.schemas import (
    CouponOffer,
    ProductResult,
    WeeklyAdItem,
)


def make_item(
    query: str,
    name: str,
    price: str = "2.00",
    location: str | None = "Aisle 10",
) -> LocatedItem:
    slug = name.lower().replace(" ", "-")
    return LocatedItem(
        query=query,
        product=ProductResult(
            item_id="1",
            name=name,
            url=f"https://www.heb.com/product-detail/{slug}/1",
            price=Decimal(price),
            location=location,
        ),
        location=parse_location(location),
        unplaced=location is None,
    )


class FakeDealsClient:
    """Minimal HEBBrowserClient double for collect_deals."""

    def __init__(
        self,
        weekly_ad: list[WeeklyAdItem] | None = None,
        coupons: list[CouponOffer] | None = None,
    ) -> None:
        self._weekly_ad = weekly_ad or []
        self._coupons = coupons or []

    def weekly_ad(self) -> list[WeeklyAdItem]:
        return self._weekly_ad

    def list_coupons(self, department=None, *, max_pages: int = 20):
        return self._coupons


def coupon(headline: str) -> CouponOffer:
    return CouponOffer(headline=headline)


# ---------------------------------------------------------------------------
# collect_deals
# ---------------------------------------------------------------------------


def test_collect_deals_weekly_ad_sale_price():
    client = FakeDealsClient(
        weekly_ad=[
            WeeklyAdItem(
                name="H-E-B Whole Milk 1 gal",
                url="",
                sale_price=Decimal("3.47"),
                regular_price=Decimal("3.97"),
            )
        ]
    )
    offers = collect_deals(client)
    assert len(offers) == 1
    assert offers[0].source == "weekly_ad"
    assert offers[0].name == "H-E-B Whole Milk 1 gal"
    kinds = {p.kind for p in offers[0].promotions}
    assert PromoKind.SALE_PRICE in kinds
    sale = next(p for p in offers[0].promotions if p.kind is PromoKind.SALE_PRICE)
    assert sale.unit_price == Decimal("3.47")
    assert sale.discount_amount == Decimal("0.50")


def test_collect_deals_reattaches_sale_price_beside_deal_mechanics():
    # Phase 1's to_promotion() drops the sale price when deal_text is set;
    # collection must put it back so the optimizer can price the ad.
    client = FakeDealsClient(
        weekly_ad=[
            WeeklyAdItem(
                name="H-E-B Creamy Creations Ice Cream",
                url="",
                sale_price=Decimal("3.48"),
                regular_price=Decimal("4.48"),
                deal_text="Buy 2, get 1 free",
            )
        ]
    )
    offers = collect_deals(client)
    assert len(offers) == 1
    kinds = {p.kind for p in offers[0].promotions}
    assert PromoKind.BOGO in kinds
    assert PromoKind.SALE_PRICE in kinds


def test_collect_deals_coupon_forces_clip_and_store_source():
    from dealforge.models import PromoSource

    client = FakeDealsClient(
        coupons=[coupon("$1.00 OFF ANY ONE (1) H-E-B Large White Eggs 12 ct")]
    )
    offers = collect_deals(client)
    assert len(offers) == 1
    assert offers[0].source == "coupon"
    promo = offers[0].promotions[0]
    assert promo.requires_clip is True
    assert promo.source is PromoSource.STORE_COUPON
    assert promo.kind is PromoKind.SAVE_FLAT
    assert promo.discount_amount == Decimal("1.00")


def test_collect_deals_drops_rows_with_no_promotion():
    client = FakeDealsClient(
        weekly_ad=[WeeklyAdItem(name="Plain Old Bread", url="")],
        coupons=[coupon("some text with no offer in it at all xyzzy")],
    )
    assert collect_deals(client) == []


def test_collect_deals_basket_offer_flagged():
    client = FakeDealsClient(
        coupons=[coupon("SAVE $5 OFF YOUR BASKET WHEN YOU BUY $25")]
    )
    offers = collect_deals(client)
    assert len(offers) == 1
    assert offers[0].is_basket_level
    promo = offers[0].promotions[0]
    assert promo.basket_level is True
    assert promo.min_spend == Decimal("25")


# ---------------------------------------------------------------------------
# match_deals_to_items
# ---------------------------------------------------------------------------


def _offers(client: FakeDealsClient):
    return collect_deals(client)


def test_match_attaches_coupon_to_item():
    offers = _offers(
        FakeDealsClient(
            coupons=[coupon("$1.00 OFF ANY ONE (1) H-E-B Large White Eggs 12 ct")]
        )
    )
    item = make_item("eggs", "H-E-B Large White Eggs 12 ct", price="2.86")
    assignment = match_deals_to_items([item], offers)
    ((got_item, matches),) = assignment.per_item
    assert got_item is item
    assert len(matches) == 1
    assert matches[0].promotion.kind is PromoKind.SAVE_FLAT
    assert matches[0].score == 1.0
    assert assignment.basket_offers == ()


def test_match_uses_product_name_not_query():
    # The query is loose ("chips"); the coupon names the SKU-level product.
    offers = _offers(
        FakeDealsClient(
            coupons=[
                coupon(
                    "SAVE $1.50 OFF 2 H-E-B Casa Magnifica Yellow Corn Tortilla Chips"
                )
            ]
        )
    )
    item = make_item(
        "chips", "H-E-B Casa Magnifica Yellow Corn Tortilla Chips 11 oz", price="2.24"
    )
    assignment = match_deals_to_items([item], offers)
    ((_, matches),) = assignment.per_item
    assert len(matches) == 1
    assert matches[0].promotion.min_quantity == 2


def test_match_partitions_basket_offers():
    offers = _offers(
        FakeDealsClient(
            coupons=[
                coupon("$1.00 OFF ANY ONE (1) H-E-B Large White Eggs 12 ct"),
                coupon("SAVE $5 OFF YOUR BASKET WHEN YOU BUY $25"),
            ]
        )
    )
    item = make_item("eggs", "H-E-B Large White Eggs 12 ct", price="2.86")
    assignment = match_deals_to_items([item], offers)
    ((_, matches),) = assignment.per_item
    assert len(matches) == 1  # the basket row never matches an item
    assert len(assignment.basket_offers) == 1
    assert assignment.basket_offers[0].name == "SAVE $5 OFF YOUR BASKET WHEN YOU BUY $25"


def test_match_no_deal_is_empty_not_an_error():
    offers = _offers(
        FakeDealsClient(
            coupons=[coupon("$1.00 OFF ANY ONE (1) H-E-B Large White Eggs 12 ct")]
        )
    )
    item = make_item("bananas", "Dole Bananas", price="0.58")
    assignment = match_deals_to_items([item], offers)
    ((_, matches),) = assignment.per_item
    assert matches == ()


def test_match_head_noun_required():
    # "frozen pizza" must not match a coupon for frozen meatballs: the shared
    # word is the adjective, not the product.
    offers = _offers(
        FakeDealsClient(
            coupons=[coupon("$1.00 OFF ANY ONE (1) H-E-B Frozen Fully Cooked Meatballs")]
        )
    )
    item = make_item("frozen pizza", "Red Baron Frozen Pizza", price="5.99")
    assignment = match_deals_to_items([item], offers)
    ((_, matches),) = assignment.per_item
    assert matches == ()


def test_match_unresolved_item_falls_back_to_query():
    offers = _offers(
        FakeDealsClient(
            coupons=[coupon("$1.00 OFF ANY ONE (1) H-E-B Large White Eggs 12 ct")]
        )
    )
    item = LocatedItem(query="eggs", product=None, location=None, unplaced=True)
    assignment = match_deals_to_items([item], offers)
    ((_, matches),) = assignment.per_item
    # The bare query still carries the product word, so it matches.
    assert len(matches) == 1
