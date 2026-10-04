# H-E-B Store Routing — how locations become a walking route

Phase 2 turns a shopping list into an ordered walk through the store:
each query is resolved to a product, the product's in-store location is
parsed, and the list is sorted into a sensible visiting order.

## Where locations come from

HEB displays an in-store location on search result cards and product pages,
verbatim, e.g. `"In Dairy on the Left Wall, A25 at The Heights H-E-B"`.
`HEBBrowserClient.search()` captures this string; `shopping.resolve_list()`
falls back to the product page only when the card has none. No login is
needed for any of this.

## Parsing (`location.py`)

`parse_location()` is regex-based and deterministic -- no LLM, so a route
is reproducible and auditable. It extracts:

- **zone**: normalized department (`"Dairy"`, `"Meat Market"`, ...) via a
  canonical map; unknown departments are title-cased, never dropped.
- **aisle code**: `"A24"` -> letter `A`, number `24`. A letter code wins over
  a bare `"Aisle 11"` when both appear (`"Aisle 11, A12"` -> `A12`).
- **position hint**: `"Left Wall"`, `"Back Wall"`, `"Front Wall"`,
  `"Right Wall"`, `"Left Edge"` -- from "on the X Wall/Edge" phrasing.
- **raw**: the verbatim original, always retained for audit.

It returns `None` (never raises) for `None`, empty, or unrecognized input.
Items that don't parse are routed last and flagged `unplaced` so the UI can
say so honestly.

## Walk order (`route.py`)

`STORE_WALK_ORDERS` maps store number -> ordered zone list. The default
(and the entry for `737`, The Heights) models a perimeter-first walk:

Produce -> Seafood -> Deli -> Bakery -> Meat Market -> Dairy -> Frozen ->
Market -> plain grocery aisles -> Checkout

`plan_route()` sorts by (zone rank, aisle letter, aisle number). Zones not
in the walk order visit after the known zones; unplaced items go last in
their original input order.

## Tuning per store

The walk order is a heuristic, not surveyed truth. To tune for a store:

1. Open the store's page on heb.com and find the "View store layout" link
   (a per-store guide PDF, e.g. `guide-houston-737.pdf`).
2. Read the department placement off the floor plan.
3. Add an entry to `STORE_WALK_ORDERS` keyed by the store number.

Zone names in the walk order must match `normalize_zone()` output
(`location.py`) exactly, or items fall into the unknown-zone bucket.

## Limitations

- HEB publishes a flat floor-plan image, not coordinates, so routing is
  zone-and-aisle ordered -- not turn-by-turn navigation.
- Location strings are per store; switching stores re-resolves locations.
- "Near Checkout 1" style spots and zone-only strings ("In Bakery") have
  no aisle code, so they sort at the head of their zone.
- Selectors for the location elements live in `parse.SELECTORS` and were
  verified live 2026-10-04; re-verify if locations start coming back empty.
