"""Property-style checks over a snapshot of real heb.com data.

The snapshot (``tests/fixtures/heb_live_*.json``) is the Oct 4, 2026 scrape
of the Heights store (#737) used for the first live end-to-end run: 10
queries, 44 product candidates, 96 coupons, 43 weekly-ad rows. These tests
assert invariants that must hold for EVERY pair, so a future loosening of
the matcher -- or a new deal grammar that slips past the gates -- fails
fast here instead of requiring another live run to notice.

The adversarial matrix (``test_heb_deal_matrix.py``) pins the *judgments*
on named hard cases; these properties pin the *wiring* across real data.
"""

from __future__ import annotations

import json
from pathlib import Path

from dealforge.heb_plan import _FixtureClient
from dealforge.providers.heb import catalog as catalog_mod
from dealforge.providers.heb.deals import (
    _brands_in,
    _head_token,
    collect_deals,
    match_deals_to_items,
    veto_reason,
)
from dealforge.providers.heb.shopping import resolve_list

FIXTURES = Path(__file__).parent / "fixtures"


def _snapshot_assignment():
    """The real pipeline over the live snapshot: resolve, collect, match."""
    products = json.loads((FIXTURES / "heb_live_products.json").read_text())
    deals = json.loads((FIXTURES / "heb_live_deals.json").read_text())
    client = _FixtureClient(products, deals)
    queries = [e["query"] for e in products]
    items = resolve_list(queries, client)
    assignment = match_deals_to_items(items, collect_deals(client))
    return assignment


def _iter_matches(assignment):
    for item, matches in assignment.per_item:
        target = item.product.name if item.product is not None else item.query
        for dm in matches:
            yield target, dm


def test_property_no_vetoed_match_emitted() -> None:
    """No emitted match may violate any strictness gate.

    If a future change bypasses the gates (reorders the pipeline, adds a
    second match path), this fails on real data.
    """
    for target, dm in _iter_matches(_snapshot_assignment()):
        reason = veto_reason(
            dm.term,
            offer_name=dm.offer.name,
            product_name=target,
            score=dm.score,
        )
        assert reason is None, (
            f"vetoed match emitted ({reason}): {dm.offer.name!r} vs {target!r}"
        )


def test_property_brand_conflict_never_matches() -> None:
    """A row naming a brand never attaches to a product with another brand."""
    for target, dm in _iter_matches(_snapshot_assignment()):
        offer_brands = set(_brands_in(dm.offer.name))
        product_brands = set(_brands_in(target))
        assert not (
            offer_brands and product_brands and offer_brands.isdisjoint(product_brands)
        ), (
            f"brand conflict matched: {dm.offer.name!r} "
            f"(brands {sorted(' '.join(b) for b in offer_brands)}) vs "
            f"{target!r} "
            f"(brands {sorted(' '.join(b) for b in product_brands)})"
        )


def test_property_offer_head_in_product() -> None:
    """Every matched row's product noun appears in the product name."""
    for target, dm in _iter_matches(_snapshot_assignment()):
        head = _head_token(dm.term)
        assert head is None or head in catalog_mod.tokenize(target), (
            f"offer head {head!r} not in product: {dm.offer.name!r} vs {target!r}"
        )


def test_property_pipeline_is_deterministic() -> None:
    """Same snapshot in, same assignment out -- the plan must be auditable."""

    def _signature(assignment):
        return tuple(
            (target, tuple((dm.offer.name, dm.promotion.raw) for dm in matches))
            for target, matches in (
                (
                    item.product.name if item.product is not None else item.query,
                    matches,
                )
                for item, matches in assignment.per_item
            )
        )

    assert _signature(_snapshot_assignment()) == _signature(_snapshot_assignment())
