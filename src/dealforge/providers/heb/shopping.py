"""Resolve a shopper's list against H-E-B search results.

For each query this module searches H-E-B, ranks the candidates with the
existing deterministic token-overlap matcher (:func:`catalog.match_query`),
and takes the best hit. The in-store location comes from the search result
card; only when the card carries none do we open the product page as a
fallback. Pacing, read-only behavior, and bot-wall aborts are inherited from
:class:`HEBBrowserClient` -- this module adds no network behavior of its own.

Queries with no acceptable match become :class:`LocatedItem` entries with
``product=None`` and ``unplaced=True`` rather than raising, so a route plan
can show what it could not place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Sequence

from dealforge.providers.heb import catalog
from dealforge.providers.heb.location import StoreLocation, parse_location
from dealforge.providers.heb.route import LocatedItem
from dealforge.providers.heb.schemas import ProductResult

if TYPE_CHECKING:  # pragma: no cover -- typing only, avoids importing playwright
    from dealforge.providers.heb.browser import HEBBrowserClient

__all__ = ["resolve_list", "best_match"]


def _as_offers(
    results: Sequence[ProductResult],
) -> tuple[list[catalog.FlyerOffer], list[ProductResult]]:
    """Adapt search results to the catalog matcher's offer shape.

    Each result becomes a one-row "flyer" so ``match_query`` can score it
    with the same token-overlap logic used for weekly-ad rows.
    """
    offers: list[catalog.FlyerOffer] = []
    kept: list[ProductResult] = []
    for result in results:
        terms = catalog._terms(result.name)
        if not terms:
            continue
        offers.append(
            catalog.FlyerOffer(
                flyer_item_id=None,
                name=result.name,
                promotions=(),
                terms=terms,
                is_brand_family=False,
            )
        )
        kept.append(result)
    return offers, kept


def best_match(
    query: str,
    results: Sequence[ProductResult],
    *,
    min_score: float = 0.5,
) -> ProductResult | None:
    """Best search result for a query, or None below ``min_score``.

    Deterministic: same token-overlap ranking as the flyer matcher, so the
    choice is reproducible and auditable. Ties keep search-result order --
    H-E-B already ranks by relevance.
    """
    offers, kept = _as_offers(results)
    if not offers:
        return None
    matches = catalog.match_query(query, offers, min_score=min_score)
    if not matches:
        return None
    # match_query sorts best-first; map the winning offer back to its result.
    winner = matches[0].offer
    for offer, result in zip(offers, kept):
        if offer is winner:
            return result
    return None  # pragma: no cover -- defensive; winner always in offers


def resolve_list(
    queries: Iterable[str],
    client: "HEBBrowserClient",
    *,
    min_score: float = 0.5,
    search_limit: int = 10,
) -> list[LocatedItem]:
    """Resolve each query to a product + store location.

    ``client`` is a started :class:`HEBBrowserClient`; its pacing applies.
    The product page is opened only when the search card had no location.
    """
    items: list[LocatedItem] = []
    for query in queries:
        results = client.search(query, limit=search_limit)
        product = best_match(query, results, min_score=min_score)
        if product is None:
            items.append(
                LocatedItem(query=query, product=None, location=None, unplaced=True)
            )
            continue
        raw_location = product.location
        if not raw_location:
            detail = client.get_product_by_url(product.url)
            raw_location = detail.location
        location: StoreLocation | None = parse_location(raw_location)
        items.append(
            LocatedItem(
                query=query,
                product=product,
                location=location,
                unplaced=location is None,
            )
        )
    return items
