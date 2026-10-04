"""Order a shopping list into a walking route through an H-E-B store.

The model is deliberately simple and legible: each store has a *walk order*,
a list of zones in the sequence a shopper would sensibly visit them, and
items are sorted by (zone position, aisle number, aisle letter). Plain
numbered grocery aisles (no department zone) are their own walk step. Items
with no parseable location go last, in their original input order, flagged
``unplaced`` so the UI can say so honestly instead of guessing.

Walk orders are plain data in :data:`STORE_WALK_ORDERS`, keyed by store
number as a string. They are a starting heuristic, not surveyed truth --
tune per store from the store's floor-plan PDF (see
``docs/research/heb-store-routing.md``). Unknown stores fall back to the
default walk order.
"""

from __future__ import annotations

from dataclasses import dataclass

from dealforge.providers.heb.location import StoreLocation
from dealforge.providers.heb.schemas import ProductResult

__all__ = [
    "DEFAULT_WALK_ORDER",
    "STORE_WALK_ORDERS",
    "LocatedItem",
    "plan_route",
    "walk_order_for",
]

#: Sensible H-E-B walk: perimeter fresh departments first (produce along the
#: front/right, seafood/deli/bakery/meat around the back), then dairy on the
#: back wall, frozen, the center grocery aisles in code order, and out through
#: the checkstands. Modeled on The Heights #737 floor plan; tune per store.
#: ``None`` is the walk step for plain numbered grocery aisles -- items whose
#: location parsed to an aisle number but no department zone
#: ("Aisle 10", "Aisle 11, A12").
DEFAULT_WALK_ORDER: list[str | None] = [
    "Produce",
    "Seafood",
    "Deli",
    "Bakery",
    "Meat Market",
    "Dairy",
    "Frozen",
    "Market",
    None,  # plain grocery aisles, e.g. "Aisle 10"
    "Checkout",
]

#: Store number (str) -> walk order. Add entries as floor plans are surveyed.
STORE_WALK_ORDERS: dict[str, list[str | None]] = {
    "737": list(DEFAULT_WALK_ORDER),  # The Heights H-E-B, 2300 N. Shepherd Dr.
    "default": list(DEFAULT_WALK_ORDER),
}


def walk_order_for(store_number: str | int | None) -> list[str | None]:
    """Walk order for a store, falling back to the default."""
    if store_number is None:
        return STORE_WALK_ORDERS["default"]
    return STORE_WALK_ORDERS.get(str(store_number), STORE_WALK_ORDERS["default"])


@dataclass(frozen=True)
class LocatedItem:
    """One shopping-list query resolved to a product and a store location."""

    query: str
    product: ProductResult | None
    location: StoreLocation | None
    #: True when the item has no parseable location -- it is routed last and
    #: the UI should say so rather than silently dropping it.
    unplaced: bool = False


def _sort_key(
    item: LocatedItem, index: int, zone_rank: dict[str | None, int]
) -> tuple:
    """(zone position, aisle number, aisle letter, input order).

    Aisles sort numerically ("Aisle 10" before "A12"); the letter breaks
    ties ("A3" before "B3"). Unplaced items sort after everything placed;
    among themselves they keep input order via the trailing index.
    """
    if item.unplaced or item.location is None:
        return (1, len(zone_rank), 0, "", index)
    loc = item.location
    # Zones not in the walk order (e.g. "Pharmacy") visit after the known
    # zones but before the unplaced tail, in aisle order.
    rank = zone_rank.get(loc.zone, len(zone_rank))
    return (
        0,
        rank,
        loc.aisle_number if loc.aisle_number is not None else 0,
        loc.aisle_letter or "",
        index,
    )


def plan_route(
    items: list[LocatedItem], store_number: str | int | None = "737"
) -> list[LocatedItem]:
    """Order items into a walking route for a store.

    Sorts by (walk-order zone, aisle number, aisle letter); items with no
    parseable location go last in their original input order. The input list
    is not modified.
    """
    order = walk_order_for(store_number)
    zone_rank = {zone: i for i, zone in enumerate(order)}
    indexed = list(enumerate(items))
    indexed.sort(key=lambda pair: _sort_key(pair[1], pair[0], zone_rank))
    return [item for _, item in indexed]
