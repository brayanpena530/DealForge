"""Collect H-E-B deals and match them to located shopping-list items.

Deals come from two browser sources: the weekly ad (:class:`WeeklyAdItem`)
and the digital-coupon listing (:class:`CouponOffer`). Each row is normalized
to the shared :class:`Promotion` model via ``to_promotion()`` -- but a
promotion alone is not enough to match on. A weekly-ad sale promotion's
``raw`` text is just ``"On Sale 2.99"``; the product name lives on the row,
not the rule. So collection keeps the named rows (:class:`DealOffer`), and
matching scores row names against the item's resolved product name with the
same deterministic token-overlap matcher the catalog uses for flyer rows.

Two Phase 1 gaps are repaired here, deterministically:

1. :meth:`WeeklyAdItem.to_promotion` drops the stated sale price whenever
   ``deal_text`` is present (the deal-mechanics branch returns before the
   price branch). Collection re-attaches a ``SALE_PRICE`` rule carrying the
   sale price whenever the parsed rules don't already have one.
2. Basket-level ``$X OFF YOUR BASKET`` offers name no product, so they can
   never match an item. They are set aside as :attr:`DealAssignment.basket_offers`
   and applied to the whole basket by the optimizer.

Nothing here prices anything or clips anything: the plan only *lists* what
to clip. Read-only, no login, no cart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Sequence

from dealforge.models import Promotion, PromoKind
from dealforge.providers.heb import catalog
from dealforge.providers.heb.route import LocatedItem
from dealforge.providers.heb.schemas import CouponOffer, WeeklyAdItem

if TYPE_CHECKING:  # pragma: no cover -- typing only, avoids importing playwright
    from dealforge.providers.heb.browser import HEBBrowserClient

__all__ = [
    "DealAssignment",
    "DealMatch",
    "DealOffer",
    "collect_deals",
    "match_deals_to_items",
]

#: Where each offer row came from. Kept as a plain string so fixtures and
#: future sources (Flipp flyer rows) can add values without an enum change.
WEEKLY_AD = "weekly_ad"
COUPON = "coupon"


@dataclass(frozen=True)
class DealOffer:
    """One deal row: a name plus the promotion rules parsed from it.

    ``name`` is the weekly-ad item name or the coupon headline -- the text
    the matcher scores against. ``promotions`` are the parsed rules; a row
    usually carries exactly one.
    """

    name: str
    source: str
    promotions: tuple[Promotion, ...]

    @property
    def is_basket_level(self) -> bool:
        """True when every rule on this row applies to the basket, not an item."""
        return bool(self.promotions) and all(
            p.basket_level for p in self.promotions
        )


@dataclass(frozen=True)
class DealMatch:
    """One promotion proposed for one list item, with the reasoning attached."""

    item: LocatedItem
    offer: DealOffer
    promotion: Promotion
    #: The row term that matched best (a coupon headline or ad item name).
    term: str
    #: Fraction of the product name's words found in the row. 1.0 = all.
    score: float
    matched: frozenset[str]


@dataclass(frozen=True)
class DealAssignment:
    """Deals partitioned for the optimizer.

    ``per_item`` pairs each located item with its candidate promotions, in
    the input list's order. (A plain dict keyed by :class:`LocatedItem` is
    not possible: the resolved product carries an unhashable ``raw`` dict.)
    ``basket_offers`` are the ``$X OFF YOUR BASKET`` rows, which name no
    product and are evaluated against the whole basket instead.
    """

    per_item: tuple[tuple[LocatedItem, tuple[DealMatch, ...]], ...]
    basket_offers: tuple[DealOffer, ...]


def _offer_from_weekly_ad(item: WeeklyAdItem) -> DealOffer | None:
    """A weekly-ad row as a named offer, re-attaching a dropped sale price."""
    promos = list(item.to_promotion())
    if item.sale_price is not None and not any(
        p.kind is PromoKind.SALE_PRICE and p.unit_price == item.sale_price
        for p in promos
    ):
        discount = None
        if item.regular_price is not None and item.regular_price > item.sale_price:
            discount = item.regular_price - item.sale_price
        promos.append(
            Promotion(
                kind=PromoKind.SALE_PRICE,
                raw=f"On Sale {item.sale_price}",
                unit_price=item.sale_price,
                discount_amount=discount,
            )
        )
    if not promos:
        return None
    return DealOffer(name=item.name, source=WEEKLY_AD, promotions=tuple(promos))


def _offer_from_coupon(coupon: CouponOffer) -> DealOffer | None:
    """A digital-coupon row as a named offer. The headline is the match text."""
    promos = tuple(coupon.to_promotion())
    if not promos:
        return None
    return DealOffer(name=coupon.headline, source=COUPON, promotions=promos)


def collect_deals(client: "HEBBrowserClient") -> list[DealOffer]:
    """Pull this week's deals from heb.com and normalize them.

    ``client`` is a started :class:`HEBBrowserClient`; its pacing applies.
    Returns the named offer rows. A caller that only wants the rules can
    flatten with ``[p for o in offers for p in o.promotions]``.

    Rows that parse to no promotion are dropped -- there is nothing to
    optimize about them.
    """
    offers: list[DealOffer] = []
    for item in client.weekly_ad():
        offer = _offer_from_weekly_ad(item)
        if offer is not None:
            offers.append(offer)
    for coupon in client.list_coupons():
        offer = _offer_from_coupon(coupon)
        if offer is not None:
            offers.append(offer)
    return offers


def _as_flyer_offers(
    offers: Sequence[DealOffer],
) -> tuple[list[catalog.FlyerOffer], list[DealOffer]]:
    """Adapt deal rows to the catalog matcher's offer shape.

    Same trick as :func:`shopping.best_match`: each row becomes a one-row
    "flyer" so ``match_query`` scores it with the standard token-overlap
    logic, and the winner maps back to its deal row by identity.
    """
    flyer: list[catalog.FlyerOffer] = []
    kept: list[DealOffer] = []
    for offer in offers:
        terms = catalog._terms(offer.name)
        if not terms:
            continue
        flyer.append(
            catalog.FlyerOffer(
                flyer_item_id=None,
                name=offer.name,
                promotions=offer.promotions,
                terms=terms,
                is_brand_family=False,
            )
        )
        kept.append(offer)
    return flyer, kept


def _match_target(item: LocatedItem) -> str:
    """What a deal row is scored against: the resolved product name.

    The product name is canonical -- the shopper's query may be looser
    ("eggs") than what H-E-B actually sells ("H-E-B Large White Eggs 12 ct"),
    and the coupon names the SKU-level product. Falls back to the query for
    items that never resolved.
    """
    if item.product is not None:
        return item.product.name
    return item.query


def match_deals_to_items(
    items: Iterable[LocatedItem],
    offers: Sequence[DealOffer],
    *,
    min_score: float = 0.5,
) -> DealAssignment:
    """Attach applicable promotions to each located item.

    Item-level rows are scored against the item's resolved product name with
    the catalog's token-overlap matcher (head-noun required, best first).
    Every promotion on a matched row becomes a :class:`DealMatch` -- the
    optimizer, not the matcher, decides which single coupon wins.

    Basket-level rows are partitioned into ``basket_offers``: they name no
    product and are evaluated against the whole basket.

    Deterministic: same inputs, same assignment, with the score and the
    matched term kept on each match for audit.
    """
    item_offers = [o for o in offers if not o.is_basket_level]
    basket_offers = tuple(o for o in offers if o.is_basket_level)

    flyer, kept = _as_flyer_offers(item_offers)
    by_flyer = {id(f): o for f, o in zip(flyer, kept)}

    per_item: list[tuple[LocatedItem, tuple[DealMatch, ...]]] = []
    for item in items:
        target = _match_target(item)
        matches: list[DealMatch] = []
        if target:
            for m in catalog.match_query(target, flyer, min_score=min_score):
                offer = by_flyer[id(m.offer)]
                for promo in offer.promotions:
                    matches.append(
                        DealMatch(
                            item=item,
                            offer=offer,
                            promotion=promo,
                            term=m.term,
                            score=m.score,
                            matched=m.matched,
                        )
                    )
        # Best row first; within a row keep promotion order.
        matches.sort(key=lambda dm: (-dm.score, dm.offer.name, dm.promotion.raw))
        per_item.append((item, tuple(matches)))
    return DealAssignment(per_item=tuple(per_item), basket_offers=basket_offers)
