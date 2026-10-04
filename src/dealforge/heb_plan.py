"""Demo: build an optimized H-E-B shopping plan (deals + route + totals).

Fixture mode (default): resolves queries against a bundled sample catalog
and matches them against bundled coupon/weekly-ad fixtures -- no browser,
no network. This is how the pipeline is exercised in CI and in this demo.

Live mode (--live): drives heb.com through HEBBrowserClient. Read-only, no
login, never clips anything: the plan only *lists* what to clip. Human-paced.
Run it yourself, not from a datacenter box.

--debit applies the 5% H-E-B debit-card rebate on private-label items. It is
payment-side: it changes the net total, not the register total.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTS_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "heb_route_items.json"
DEALS_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "heb_plan_deals.json"


class _FixtureClient:
    """Test double for HEBBrowserClient backed by the JSON fixtures."""

    def __init__(self, products: list[dict], deals: dict) -> None:
        self._by_query = {e["query"]: e for e in products}
        self._deals = deals

    def _product(self, cand: dict):
        from dealforge.providers.heb.schemas import ProductResult

        price = cand.get("price")
        return ProductResult(
            item_id=cand["item_id"],
            name=cand["name"],
            url=cand["url"],
            price=Decimal(price) if price is not None else None,
            price_unit=cand.get("price_unit"),
            unit_price_text=cand.get("unit_price_text"),
            location=cand.get("location"),
        )

    def search(self, query: str, limit: int = 10):
        entry = self._by_query.get(query)
        if not entry:
            return []
        return [self._product(c) for c in entry["candidates"][:limit]]

    def get_product_by_url(self, url: str):
        from dealforge.providers.heb.schemas import ProductDetail

        for entry in self._by_query.values():
            for cand in entry["candidates"]:
                if cand["url"] == url:
                    return ProductDetail(
                        item_id=cand["item_id"],
                        name=cand["name"],
                        url=cand["url"],
                        location=cand.get("detail_location"),
                    )
        return ProductDetail(item_id="?", name="?", url=url, location=None)

    def weekly_ad(self):
        from dealforge.providers.heb.schemas import WeeklyAdItem

        items = []
        for row in self._deals.get("weekly_ad", []):
            sale = row.get("sale_price")
            regular = row.get("regular_price")
            items.append(
                WeeklyAdItem(
                    name=row["name"],
                    url=row.get("url", ""),
                    sale_price=Decimal(sale) if sale else None,
                    regular_price=Decimal(regular) if regular else None,
                    deal_text=row.get("deal_text"),
                )
            )
        return items

    def list_coupons(self, department=None, *, max_pages: int = 20):
        from dealforge.providers.heb.schemas import CouponOffer

        return [
            CouponOffer(
                headline=row["headline"],
                expiry=row.get("expiry"),
                limit=row.get("limit"),
            )
            for row in self._deals.get("coupons", [])
        ]


def _money(value: Decimal | None) -> str:
    return f"${value:.2f}" if value is not None else "n/a"


def _print_plan(plan, *, store_number: str, live: bool) -> None:
    mode = "live heb.com" if live else "fixture data (no live lookup)"
    print(f"Shopping plan (store #{store_number})  [{mode}]\n")

    last_zone = object()
    n = 0
    for line in plan.lines:
        n += 1
        if line.unplaced or line.location_text is None:
            zone = "Unplaced"
        else:
            # Group header from the location's zone; reuse the route module's
            # notion of zones via a light parse of the raw location string.
            zone = _zone_of(line)
        if zone != last_zone:
            print(f"{zone}")
            last_zone = zone
        name = line.product_name or "no match found"
        print(f"  {n}. {line.query} -> {name}")
        if line.location_text:
            print(f"     [{line.location_text}]")
        if line.final_price is None:
            print("     price unknown")
            continue
        parts = [f"list {_money(line.list_price)}"]
        if line.ad_price is not None and line.ad_price < (line.list_price or line.ad_price):
            parts.append(f"ad {_money(line.ad_price)}")
        if line.coupon is not None:
            coupon_bit = f'coupon -{_money(line.coupon_savings)} "{line.coupon_headline}"'
            if line.coupon_qualifier:
                coupon_bit += f" ({line.coupon_qualifier})"
            parts.append(coupon_bit)
        parts.append(f"= {_money(line.final_price)}")
        print("     " + "  ".join(parts))
        for note in line.notes:
            print(f"     note: {note}")

    if plan.coupons_to_clip:
        print(f"\nCoupons to clip ({len(plan.coupons_to_clip)}):")
        for headline in plan.coupons_to_clip:
            print(f"  - {headline}")

    if plan.basket_promos or plan.basket_notes:
        print("\nBasket:")
        for promo in plan.basket_promos:
            print(f'  -{_money(promo.discount_amount)} "{promo.raw}" (qualified)')
        for note in plan.basket_notes:
            print(f"  ~ {note}")

    print("\nTotals:")
    print(f"  List:      {_money(plan.total_list)}")
    print(f"  Register:  {_money(plan.total_final)}")
    print(f"  You save:  {_money(plan.total_savings)}")
    if plan.debit_card:
        print(f"  Debit rebate (5% private label): -{_money(plan.debit_rebate_total)}")
        print(f"  Net:       {_money(plan.total_net)}")


def _zone_of(line) -> str:
    """Best-effort zone header from a plan line's raw location string."""
    from dealforge.providers.heb.location import parse_location

    loc = parse_location(line.location_text)
    if loc is not None and loc.zone:
        return loc.zone
    if loc is not None and loc.aisle_number is not None:
        return "Aisles"
    return "Unplaced"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build an optimized H-E-B shopping plan."
    )
    parser.add_argument("items", nargs="*", help="shopping list queries")
    parser.add_argument("--zip", default="77008", help="store ZIP (default: 77008)")
    parser.add_argument("--store", default="737", help="store number (default: 737)")
    parser.add_argument(
        "--live",
        action="store_true",
        help="hit heb.com through a real Chromium session (read-only)",
    )
    parser.add_argument(
        "--debit",
        action="store_true",
        help="apply the 5% H-E-B debit-card rebate on private-label items",
    )
    args = parser.parse_args(argv)

    from dealforge.providers.heb.deals import collect_deals, match_deals_to_items
    from dealforge.providers.heb.optimizer import optimize
    from dealforge.providers.heb.shopping import resolve_list

    if args.live:
        from dealforge.providers.heb.browser import HEBBrowserClient

        queries = args.items or [
            "eggs",
            "milk",
            "tortilla chips",
            "ice cream",
            "bread",
        ]
        with HEBBrowserClient(store_zip=args.zip) as heb:
            heb.set_store(args.zip)
            items = resolve_list(queries, heb)
            offers = collect_deals(heb)
    else:
        if not PRODUCTS_FIXTURE.exists():
            print(f"fixture not found: {PRODUCTS_FIXTURE}", file=sys.stderr)
            return 1
        if not DEALS_FIXTURE.exists():
            print(f"fixture not found: {DEALS_FIXTURE}", file=sys.stderr)
            return 1
        products = json.loads(PRODUCTS_FIXTURE.read_text(encoding="utf-8"))
        deals = json.loads(DEALS_FIXTURE.read_text(encoding="utf-8"))
        client = _FixtureClient(products, deals)
        queries = args.items or [e["query"] for e in products]
        items = resolve_list(queries, client)
        offers = collect_deals(client)

    assignment = match_deals_to_items(items, offers)
    plan = optimize(assignment, store_number=args.store, debit_card=args.debit)
    _print_plan(plan, store_number=args.store, live=args.live)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
