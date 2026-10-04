"""Demo: search H-E-B and print products with price + aisle location.

Fixture mode (default): parses the bundled sample search page -- no browser,
no network. This is how the pipeline is exercised in CI and in this demo.

Live mode (--live): drives heb.com through HEBBrowserClient. Read-only, no
login; human-paced. Run it yourself, not from a datacenter box.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "heb_search_eggs.html"


def _print_results(query: str, results, *, live: bool) -> None:
    mode = "live heb.com" if live else "fixture data (no live lookup)"
    print(f"query: {query}  [{mode}]")
    print(f"{'#':<3} {'item':<58} {'price':<12} location")
    print("-" * 110)
    for i, r in enumerate(results, 1):
        price = f"${r.price} {r.price_unit or ''}".strip() if r.price else "?"
        unit = r.unit_price_text or ""
        if unit and not (unit.startswith("(") and unit.endswith(")")):
            unit = f"({unit})"
        print(f"{i:<3} {r.name[:58]:<58} {price + ' ' + unit:<12} {r.location or '?'}" .rstrip())
    print(f"\n{len(results)} result(s)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Search H-E-B, show price + aisle.")
    parser.add_argument("query", nargs="?", default="eggs", help="product to search")
    parser.add_argument("--zip", default="77008", help="store ZIP (default: 77008)")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--live",
        action="store_true",
        help="hit heb.com through a real Chromium session (read-only)",
    )
    args = parser.parse_args(argv)

    if args.live:
        from dealforge.providers.heb.browser import HEBBrowserClient

        with HEBBrowserClient(store_zip=args.zip) as heb:
            heb.set_store(args.zip)
            results = heb.search(args.query, limit=args.limit)
        _print_results(args.query, results, live=True)
        return 0

    if not FIXTURE.exists():
        print(f"fixture not found: {FIXTURE}", file=sys.stderr)
        return 1
    from dealforge.providers.heb.parse import parse_search_results

    results = parse_search_results(FIXTURE.read_text(encoding="utf-8"), limit=args.limit)
    _print_results(args.query, results, live=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
