"""End-to-end plan test on fixture data (offline, no browser)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from dealforge import heb_plan
from dealforge.providers.heb.deals import collect_deals, match_deals_to_items
from dealforge.providers.heb.optimizer import optimize
from dealforge.providers.heb.shopping import resolve_list

FIXTURES = Path(__file__).parent / "fixtures"


def _client():
    products = json.loads((FIXTURES / "heb_route_items.json").read_text())
    deals = json.loads((FIXTURES / "heb_plan_deals.json").read_text())
    return heb_plan._FixtureClient(products, deals)


def _plan(**kwargs):
    client = _client()
    queries = [e["query"] for e in json.loads((FIXTURES / "heb_route_items.json").read_text())]
    items = resolve_list(queries, client)
    assignment = match_deals_to_items(items, collect_deals(client))
    return optimize(assignment, store_number="737", **kwargs)


def test_full_plan_totals_on_fixtures():
    plan = _plan()
    assert plan.total_list == Decimal("31.42")
    assert plan.total_final == Decimal("22.18")
    assert plan.total_savings == Decimal("9.24")


def test_full_plan_picks_best_coupon_and_combines_ad_sale():
    plan = _plan()
    by_query = {line.query: line for line in plan.lines}
    # No stacking: the $1.00 coupon beats 20% off on eggs.
    assert by_query["eggs"].coupon_headline == "$1.00 OFF ANY ONE (1) H-E-B Large White Eggs 12 ct"
    assert by_query["eggs"].final_price == Decimal("1.86")
    # Ad sale price and the one coupon combine on milk.
    assert by_query["milk"].ad_price == Decimal("3.47")
    assert by_query["milk"].final_price == Decimal("2.97")
    # Multi-unit coupon valued per unit with its qualifier surfaced.
    assert by_query["tortilla chips"].coupon_qualifier == "buy 2"
    assert by_query["tortilla chips"].final_price == Decimal("1.49")


def test_full_plan_basket_and_clip_list():
    plan = _plan()
    assert plan.basket_savings == Decimal("5.00")
    assert len(plan.basket_promos) == 1
    assert plan.basket_notes == ("$7.82 away from $10.00 off (spend $35.00)",)
    assert plan.coupons_to_clip == (
        "$0.50 OFF ANY ONE (1) H-E-B Whole Milk 1 gal",
        "$1.00 OFF ANY ONE (1) H-E-B Large White Eggs 12 ct",
        "SAVE $1.50 OFF 2 H-E-B Casa Magnifica Yellow Corn Tortilla Chips",
        "SAVE $5 OFF YOUR BASKET WHEN YOU BUY $25",
    )


def test_full_plan_route_order_and_unplaced():
    plan = _plan()
    queries = [line.query for line in plan.lines]
    # Walk order: produce first, plain aisles after departments, unplaced last.
    assert queries.index("bananas") < queries.index("eggs")
    assert queries.index("eggs") < queries.index("tortilla chips")
    assert queries[-1] == "unicorn horns"
    assert plan.lines[-1].unplaced is True
    assert plan.lines[-1].final_price is None


def test_full_plan_debit_rebate():
    plan = _plan(debit_card=True)
    by_query = {line.query: line for line in plan.lines}
    # Dole bananas are not private label: no rebate.
    assert by_query["bananas"].debit_rebate == Decimal("0")
    assert by_query["milk"].debit_rebate > Decimal("0")
    assert plan.debit_rebate_total == Decimal("1.32")
    assert plan.total_final == Decimal("22.18")  # register unchanged
    assert plan.total_net == Decimal("20.86")


def test_cli_runs_on_fixtures(capsys):
    assert heb_plan.main([]) == 0
    out = capsys.readouterr().out
    assert "Shopping plan (store #737)" in out
    assert "You save:  $9.24" in out
    assert "Coupons to clip (4):" in out
