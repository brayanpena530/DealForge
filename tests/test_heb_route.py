"""Tests for the H-E-B store route planner."""

from __future__ import annotations

from decimal import Decimal

from dealforge.providers.heb.location import parse_location
from dealforge.providers.heb.route import (
    LocatedItem,
    plan_route,
    walk_order_for,
)
from dealforge.providers.heb.schemas import ProductResult


def product(name: str, location: str | None = None) -> ProductResult:
    return ProductResult(
        item_id="1",
        name=name,
        url="https://www.heb.com/product-detail/x/1",
        price=Decimal("1.00"),
        location=location,
    )


def placed(query: str, raw_location: str) -> LocatedItem:
    return LocatedItem(
        query=query,
        product=product(query),
        location=parse_location(raw_location),
        unplaced=False,
    )


def unplaced(query: str) -> LocatedItem:
    return LocatedItem(query=query, product=None, location=None, unplaced=True)


# ---------------------------------------------------------------------------


def test_walk_order_default_and_unknown_store():
    assert walk_order_for("737")[0] == "Produce"
    assert walk_order_for("737")[-1] == "Checkout"
    assert walk_order_for("999999") == walk_order_for(None)


def test_route_orders_zones_by_walk_order():
    items = [
        placed("ice cream", "In Frozen Foods, B12"),
        placed("bananas", "In Produce, A3"),
        placed("eggs", "In Dairy on the Left Wall, A25"),
        placed("bread", "In Bakery"),
    ]
    assert [i.query for i in plan_route(items)] == [
        "bananas",
        "bread",
        "eggs",
        "ice cream",
    ]


def test_same_zone_orders_by_aisle_number_then_letter():
    items = [
        placed("chips", "In Produce, B4"),
        placed("bananas", "In Produce, A3"),
        placed("salsa", "In Produce, A10"),
    ]
    assert [i.query for i in plan_route(items)] == ["bananas", "chips", "salsa"]


def test_plain_aisles_visit_in_number_order():
    items = [
        placed("towels", "Aisle 11, A12"),
        placed("chips", "Aisle 10"),
        placed("soap", "Aisle 3, A15"),
    ]
    assert [i.query for i in plan_route(items)] == ["chips", "towels", "soap"]


def test_unplaced_go_last_in_input_order():
    items = [
        unplaced("unicorn horns"),
        placed("bananas", "In Produce, A3"),
        unplaced("dragon fruit"),
    ]
    assert [i.query for i in plan_route(items)] == [
        "bananas",
        "unicorn horns",
        "dragon fruit",
    ]


def test_unknown_zone_after_known_zones_before_unplaced():
    items = [
        unplaced("mystery"),
        placed("vitamins", "In Pharmacy"),
        placed("bananas", "In Produce, A3"),
    ]
    assert [i.query for i in plan_route(items)] == [
        "bananas",
        "vitamins",
        "mystery",
    ]


def test_input_list_is_not_modified():
    items = [
        placed("ice cream", "In Frozen Foods, B12"),
        placed("bananas", "In Produce, A3"),
    ]
    plan_route(items)
    assert [i.query for i in items] == ["ice cream", "bananas"]
