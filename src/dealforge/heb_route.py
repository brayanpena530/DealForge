"""Demo: resolve a shopping list into an ordered H-E-B store route.

Fixture mode (default): resolves queries against a bundled sample catalog --
no browser, no network. This is how the pipeline is exercised in CI and in
this demo.

Live mode (--live): drives heb.com through HEBBrowserClient. Read-only, no
login; human-paced. Run it yourself, not from a datacenter box.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "heb_route_items.json"


class _FixtureClient:
    """Test double for HEBBrowserClient backed by the JSON fixture."""

    def __init__(self, entries: list[dict]) -> None:
        self._by_query = {e["query"]: e for e in entries}

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


def _print_route(items, *, store_number: str, live: bool) -> None:
    from dealforge.providers.heb.route import plan_route

    ordered = plan_route(items, store_number=store_number)
    placed = [i for i in ordered if not i.unplaced]
    unplaced = [i for i in ordered if i.unplaced]
    mode = "live heb.com" if live else "fixture data (no live lookup)"
    print(f"Store route (store #{store_number})  [{mode}]")
    print(f"{len(placed)} placed, {len(unplaced)} unplaced\n")

    last_zone = object()
    n = 0
    for item in placed:
        zone = item.location.zone if item.location and item.location.zone else "Aisles"
        if zone != last_zone:
            print(f"{zone}")
            last_zone = zone
        n += 1
        name = item.product.name if item.product else item.query
        aisle = ""
        if item.location:
            if item.location.aisle_code:
                aisle = f" [{item.location.aisle_code}]"
            elif item.location.aisle_number is not None:
                aisle = f" [Aisle {item.location.aisle_number}]"
        price = f" ${item.product.price}" if item.product and item.product.price else ""
        print(f"  {n}. {item.query} -> {name}{price}{aisle}")

    if unplaced:
        print("\nUnplaced")
        for item in unplaced:
            reason = "no match" if item.product is None else "no location found"
            print(f"  {n + 1}. {item.query} ({reason})")
            n += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve a shopping list into an H-E-B store route."
    )
    parser.add_argument("items", nargs="*", help="shopping list queries")
    parser.add_argument("--zip", default="77008", help="store ZIP (default: 77008)")
    parser.add_argument("--store", default="737", help="store number (default: 737)")
    parser.add_argument(
        "--live",
        action="store_true",
        help="hit heb.com through a real Chromium session (read-only)",
    )
    args = parser.parse_args(argv)

    from dealforge.providers.heb.shopping import resolve_list

    if args.live:
        from dealforge.providers.heb.browser import HEBBrowserClient

        queries = args.items or ["eggs", "bananas", "milk"]
        with HEBBrowserClient(store_zip=args.zip) as heb:
            heb.set_store(args.zip)
            items = resolve_list(queries, heb)
        _print_route(items, store_number=args.store, live=True)
        return 0

    if not FIXTURE.exists():
        print(f"fixture not found: {FIXTURE}", file=sys.stderr)
        return 1
    entries = json.loads(FIXTURE.read_text(encoding="utf-8"))
    client = _FixtureClient(entries)
    queries = args.items or [e["query"] for e in entries]
    items = resolve_list(queries, client)
    _print_route(items, store_number=args.store, live=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
