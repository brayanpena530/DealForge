"""Tests for the H-E-B deal optimizer (offline, hand-built assignments)."""

from __future__ import annotations

from decimal import Decimal

from dealforge.models import Promotion, PromoKind, PromoSource
from dealforge.providers.heb.deals import (
    DealAssignment,
    DealMatch,
    DealOffer,
    COUPON,
)
from dealforge.providers.heb.location import parse_location
from dealforge.providers.heb.optimizer import optimize
from dealforge.providers.heb.route import LocatedItem
from dealforge.providers.heb.schemas import ProductResult


def make_item(
    query: str,
    name: str,
    price: str,
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


def flat_coupon(
    amount: str, headline: str = "coupon", min_quantity: int | None = None
) -> Promotion:
    return Promotion(
        kind=PromoKind.SAVE_FLAT,
        raw=headline,
        discount_amount=Decimal(amount),
        min_quantity=min_quantity,
        source=PromoSource.STORE_COUPON,
        requires_clip=True,
    )


def sale_price(amount: str) -> Promotion:
    return Promotion(
        kind=PromoKind.SALE_PRICE,
        raw=f"On Sale {amount}",
        unit_price=Decimal(amount),
    )


def basket_offer(
    amount: str, min_spend: str | None, headline: str = "basket coupon"
) -> DealOffer:
    return DealOffer(
        name=headline,
        source=COUPON,
        promotions=(
            Promotion(
                kind=PromoKind.BASKET_THRESHOLD,
                raw=headline,
                discount_amount=Decimal(amount),
                min_spend=Decimal(min_spend) if min_spend else None,
                basket_level=True,
                stackable=True,
                requires_clip=True,
            ),
        ),
    )


def coupon_offer(name: str, *promos: Promotion) -> DealOffer:
    return DealOffer(name=name, source=COUPON, promotions=promos)


def assign(pairs: list[tuple[LocatedItem, list[DealMatch]]], basket=()) -> DealAssignment:
    return DealAssignment(
        per_item=tuple((item, tuple(ms)) for item, ms in pairs),
        basket_offers=tuple(basket),
    )


def match(item: LocatedItem, offer: DealOffer, promo: Promotion) -> DealMatch:
    return DealMatch(
        item=item,
        offer=offer,
        promotion=promo,
        term=offer.name,
        score=1.0,
        matched=frozenset({"x"}),
    )


# ---------------------------------------------------------------------------
# one coupon per item
# ---------------------------------------------------------------------------


def test_no_stacking_picks_best_single_coupon():
    item = make_item("eggs", "H-E-B Large White Eggs 12 ct", "2.86")
    offer_a = coupon_offer("$1.00 OFF eggs", flat_coupon("1.00", "$1.00 OFF eggs"))
    offer_b = coupon_offer("SAVE 20% OFF eggs", Promotion(
        kind=PromoKind.SAVE_PERCENT,
        raw="SAVE 20% OFF eggs",
        discount_percent=Decimal("20"),
        source=PromoSource.STORE_COUPON,
        requires_clip=True,
    ))
    plan = optimize(
        assign([(item, [match(item, offer_a, offer_a.promotions[0]),
                       match(item, offer_b, offer_b.promotions[0])])])
    )
    (line,) = plan.lines
    # $1.00 beats 20% of $2.86 ($0.57): exactly one coupon applies.
    assert line.coupon_headline == "$1.00 OFF eggs"
    assert line.coupon_savings == Decimal("1.00")
    assert line.final_price == Decimal("1.86")
    assert line.line_savings == Decimal("1.00")


def test_sale_price_and_one_coupon_combine():
    item = make_item("milk", "H-E-B Whole Milk 1 gal", "3.97")
    ad = coupon_offer("ad", sale_price("3.47"))
    cpn = coupon_offer("$0.50 OFF milk", flat_coupon("0.50", "$0.50 OFF milk"))
    plan = optimize(
        assign([(item, [match(item, ad, ad.promotions[0]),
                       match(item, cpn, cpn.promotions[0])])])
    )
    (line,) = plan.lines
    assert line.ad_price == Decimal("3.47")
    assert line.coupon_savings == Decimal("0.50")
    assert line.final_price == Decimal("2.97")


def test_ad_price_never_raises_the_price():
    # Live search prices often already reflect the ad; re-applying a stale
    # (higher) ad price must not raise what the shopper pays.
    item = make_item("milk", "H-E-B Whole Milk 1 gal", "3.00")
    ad = coupon_offer("ad", sale_price("3.47"))
    plan = optimize(assign([(item, [match(item, ad, ad.promotions[0])])]))
    (line,) = plan.lines
    assert line.final_price == Decimal("3.00")
    assert line.ad_price is None


def test_coupon_clipped_at_item_price():
    # H-E-B clips a coupon's face value at the item price: no cash back.
    item = make_item("gum", "H-E-B Chewing Gum", "1.25")
    cpn = coupon_offer("$5 OFF gum", flat_coupon("5.00", "$5 OFF gum"))
    plan = optimize(assign([(item, [match(item, cpn, cpn.promotions[0])])]))
    (line,) = plan.lines
    assert line.coupon_savings == Decimal("1.25")
    assert line.final_price == Decimal("0.00")


def test_min_quantity_coupon_valued_per_unit_with_qualifier():
    item = make_item(
        "chips", "H-E-B Casa Magnifica Tortilla Chips 11 oz", "2.24"
    )
    cpn = coupon_offer(
        "SAVE $1.50 OFF 2 chips", flat_coupon("1.50", "SAVE $1.50 OFF 2 chips", 2)
    )
    plan = optimize(assign([(item, [match(item, cpn, cpn.promotions[0])])]))
    (line,) = plan.lines
    assert line.coupon_savings == Decimal("0.75")
    assert line.coupon_qualifier == "buy 2"
    assert line.final_price == Decimal("1.49")


def test_bogo_ad_mechanic_beats_sale_price():
    item = make_item("ice cream", "H-E-B Creamy Creations Ice Cream", "4.48")
    bogo = Promotion(
        kind=PromoKind.BOGO,
        raw="Buy 2, get 1 free",
        min_quantity=2,
        get_quantity=1,
        discount_percent=Decimal(100),
        source=PromoSource.AD_SALE,
    )
    ad = coupon_offer("ad", bogo, sale_price("3.48"))
    plan = optimize(
        assign([(item, [match(item, ad, bogo), match(item, ad, ad.promotions[1])])])
    )
    (line,) = plan.lines
    # Buy-2-get-1 at $4.48 ($2.99/unit) beats the $3.48 sale price.
    assert line.ad_price == Decimal("2.99")
    assert line.final_price == Decimal("2.99")


def test_unvaluable_coupon_skipped_with_note():
    item = make_item("chicken", "H-E-B Chicken Breasts", "2.97")
    per_lb = Promotion(
        kind=PromoKind.SAVE_PER_LB,
        raw="SAVE up to $2 per lb.",
        discount_amount=Decimal("2"),
        source=PromoSource.STORE_COUPON,
        requires_clip=True,
    )
    offer = coupon_offer("per-lb coupon", per_lb)
    plan = optimize(assign([(item, [match(item, offer, per_lb)])]))
    (line,) = plan.lines
    assert line.coupon is None
    assert line.final_price == Decimal("2.97")
    assert any("Could not value" in n for n in line.notes)


def test_bundle_offer_surfaced_as_note_not_savings():
    item = make_item("soda", "H-E-B Soda 12 pk", "5.98")
    bundle = Promotion(
        kind=PromoKind.BUNDLE,
        raw="Combo Loco -- Buy this, get that free",
        get_quantity=1,
        source=PromoSource.AD_SALE,
    )
    offer = coupon_offer("combo", bundle)
    plan = optimize(assign([(item, [match(item, offer, bundle)])]))
    (line,) = plan.lines
    assert line.final_price == Decimal("5.98")
    assert any("Bundle available" in n for n in line.notes)


# ---------------------------------------------------------------------------
# basket layer
# ---------------------------------------------------------------------------


def _two_item_basket():
    a = make_item("eggs", "H-E-B Large White Eggs 12 ct", "2.86", "In Dairy, A24")
    b = make_item("bread", "H-E-B Bakery Bread 20 oz", "1.88", "In Bakery")
    return a, b


def test_basket_threshold_applies_when_qualified():
    a, b = _two_item_basket()
    plan = optimize(
        assign([(a, []), (b, [])], basket=[basket_offer("5.00", "4.00")])
    )
    assert plan.basket_savings == Decimal("5.00")
    assert len(plan.basket_promos) == 1
    # Subtotal 4.74 - 5.00 floors the register total at zero.
    assert plan.total_final == Decimal("0.00")
    assert plan.basket_notes == ()


def test_basket_threshold_not_applied_below_minimum():
    a, b = _two_item_basket()
    plan = optimize(
        assign([(a, []), (b, [])], basket=[basket_offer("5.00", "25.00")])
    )
    assert plan.basket_savings == Decimal("0.00")
    assert plan.basket_promos == ()
    assert plan.total_final == Decimal("4.74")


def test_near_threshold_gap_reported():
    a, b = _two_item_basket()
    plan = optimize(
        assign([(a, []), (b, [])], basket=[basket_offer("10.00", "8.00")])
    )
    assert plan.basket_notes == ("$3.26 away from $10.00 off (spend $8.00)",)


def test_far_threshold_silent():
    a, b = _two_item_basket()
    plan = optimize(
        assign([(a, []), (b, [])], basket=[basket_offer("10.00", "40.00")])
    )
    assert plan.basket_notes == ()


def test_basket_coupon_requiring_clip_listed_to_clip():
    a, b = _two_item_basket()
    plan = optimize(
        assign([(a, []), (b, [])], basket=[basket_offer("5.00", "4.00")])
    )
    assert "basket coupon" in plan.coupons_to_clip


# ---------------------------------------------------------------------------
# debit rebate
# ---------------------------------------------------------------------------


def test_debit_rebate_only_on_private_label():
    heb = make_item("milk", "H-E-B Whole Milk 1 gal", "3.97")
    dole = make_item("bananas", "Dole Bananas", "0.58")
    plan = optimize(assign([(heb, []), (dole, [])]), debit_card=True)
    by_query = {line.query: line for line in plan.lines}
    assert by_query["milk"].debit_rebate == Decimal("0.20")  # 5% of 3.97
    assert by_query["bananas"].debit_rebate == Decimal("0")
    assert plan.debit_rebate_total == Decimal("0.20")
    # The rebate never changes the register total.
    assert plan.total_final == Decimal("4.55")
    assert plan.total_net == Decimal("4.35")


def test_no_rebate_without_debit_card():
    heb = make_item("milk", "H-E-B Whole Milk 1 gal", "3.97")
    plan = optimize(assign([(heb, [])]))
    (line,) = plan.lines
    assert line.debit_rebate == Decimal("0")
    assert plan.debit_rebate_total == Decimal("0")
    assert plan.total_net == plan.total_final


# ---------------------------------------------------------------------------
# plan shape
# ---------------------------------------------------------------------------


def test_lines_follow_walk_order_and_totals_add_up():
    dairy = make_item("eggs", "H-E-B Large White Eggs 12 ct", "2.86", "In Dairy, A24")
    produce = make_item("bananas", "Dole Bananas", "0.58", "In Produce, A3")
    cpn = coupon_offer("$1.00 OFF eggs", flat_coupon("1.00", "$1.00 OFF eggs"))
    plan = optimize(
        assign(
            [
                (dairy, [match(dairy, cpn, cpn.promotions[0])]),
                (produce, []),
            ]
        ),
        store_number="737",
    )
    assert [line.query for line in plan.lines] == ["bananas", "eggs"]
    assert plan.total_list == Decimal("3.44")
    assert plan.total_final == Decimal("2.44")
    assert plan.total_savings == Decimal("1.00")
    assert plan.coupons_to_clip == ("$1.00 OFF eggs",)


def test_unplaced_item_stays_in_plan_with_unknown_price():
    from dealforge.providers.heb.schemas import ProductResult

    item = LocatedItem(query="unicorn horns", product=None, location=None, unplaced=True)
    plan = optimize(assign([(item, [])]))
    (line,) = plan.lines
    assert line.unplaced is True
    assert line.final_price is None
    assert line.line_savings == Decimal("0")
    assert plan.total_final == Decimal("0.00")
