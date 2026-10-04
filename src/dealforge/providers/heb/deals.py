"""Collect H-E-B deals and match them to located shopping-list items.

Deals come from two browser sources: the weekly ad (:class:`WeeklyAdItem`)
and the digital-coupon listing (:class:`CouponOffer`). Each row is normalized
to the shared :class:`Promotion` model via ``to_promotion()`` -- but a
promotion alone is not enough to match on. A weekly-ad sale promotion's
``raw`` text is just ``"On Sale 2.99"``; the product name lives on the row,
not the rule. So collection keeps the named rows (:class:`DealOffer`), and
matching scores row names against the item's resolved product name with the
same deterministic token-overlap matcher the catalog uses for flyer rows --
then applies four strictness gates (see :func:`veto_reason`) so a row only
attaches when the product plausibly *is* the row's product: conflicting
brands veto, the row's product noun must appear in the product, conflicting
line qualifiers veto, and quantity-gated rows ("buy 2") must name the
product exactly.

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

import re
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
    "veto_reason",
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


# ---------------------------------------------------------------------------
# Match strictness gates.
#
# Token overlap alone over-matches: it counts shared words but is blind to
# the words that make two products different. A coupon for "Whole Milk
# Plain Greek Yogurt" shares every word of "H-E-B Whole Milk" yet names a
# different product; an ad for "Seasoned Chicken Breast for Fajitas" shares
# "chicken breast" with "Natural Boneless Chicken Breast, Thin Sliced" yet
# is a different product. The four gates below veto such rows
# deterministically (no LLM), each with a human-readable reason. They run
# after the catalog's overlap score, so the score still decides *candidacy*
# and the gates decide *eligibility*.
#
# When a new false positive appears, the fix is one of: a new entry in
# _BRANDS (a brand the row names), a new pair in _CONFLICTS (two qualifier
# groups that never describe the same product), a new word in
# _TRAILING_MODIFIERS (a flavor/heat word that dangles at the end of a
# name), or a new pattern in _QUANTITY_RE (a "buy N" phrasing). Add the
# entry, then add the (offer, product, expect) pair to the adversarial
# matrix in tests/test_heb_deal_matrix.py.
# ---------------------------------------------------------------------------

#: Brand token-groups, singularized the way catalog.tokenize folds them
#: ("Sunups" -> "sunup", "Pete and Gerry's" -> "pete gerry"). A row vetoes
#: only on *conflicting* brands: a branded row may still match an unbranded
#: product, but never a product carrying a different brand.
_BRANDS: tuple[tuple[str, ...], ...] = (
    ("h-e-b",),
    ("hill", "country", "fare"),
    ("central", "market"),
    ("higher", "harvest"),
    ("mi", "tienda"),
    ("pete", "gerry"),
    ("sunup",),
    ("dole",),
    ("bush",),
    ("blue", "bell"),
    ("nature",),
    ("simply",),
    ("fairlife",),
    ("silk",),
    ("oatly",),
    ("planet", "oat"),
    ("tyson",),
    ("sabra",),
    ("frito", "lay"),
    ("thomas",),
    ("mateo",),
    ("herdez",),
    ("johnsonville",),
    ("old", "el", "paso"),
    ("la", "banderita"),
    ("guerrero",),
    ("siete",),
    ("bimbo",),
    ("marinela",),
    ("kerrygold",),
    ("schar",),
    ("bonduelle",),
    ("luby",),
    ("jimmy", "dean"),
    ("delimex",),
    ("cacique",),
    ("core", "power"),
    ("muscle", "milk"),
    ("naked",),
    ("pom",),
    ("welch",),
    ("capri", "sun"),
    ("vive",),
    ("trip",),
    ("good", "sense"),
    ("camellia",),
    ("westbrae",),
    ("green", "valley"),
    ("wolf",),
    ("goya",),
    ("ducal",),
    ("stella",),
    ("haleon",),
    ("dove",),
    ("vaseline",),
    ("degree",),
    ("il", "critter"),
    ("vitafusion",),
    ("romansana",),
    ("chicken", "sea"),
)

#: Mutually exclusive qualifier groups. Each pair is (group_a, group_b);
#: when one side of the match carries all of group_a and the other side
#: carries all of group_b (either direction), the row cannot be the
#: product. "Round Top" and "Grain & Glory" are different bread lines;
#: "seasoned" and "natural" never describe the same chicken.
_CONFLICTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("round", "top"), ("grain", "glory")),
    (("round", "top"), ("split", "top")),
    (("grain", "glory"), ("split", "top")),
    (("seasoned",), ("natural",)),
    (("white",), ("whole", "wheat")),
)

#: Flavor/heat words that dangle at the end of a name without naming the
#: product ("Italian Sausage - Mild" is a sausage). Dropped when finding
#: the head token so the product noun is compared, not the modifier.
_TRAILING_MODIFIERS = frozenset({"mild", "medium", "hot", "spicy"})

#: "Buy 2", "$1 off 2", "2 for $5", "when you buy 2". A quantity-gated row
#: is a commitment device: it may only attach when it names the product at
#: least as specifically as the product names itself (overlap score 1.0).
#: The optimizer then values it per unit with a "buy N" qualifier note.
_QUANTITY_RE = re.compile(
    r"\bbuy\s+(one|two|three|four|five|\d+)\b"
    r"|\boff\s+(one|two|three|four|five|\d+)\b"
    r"|\b(one|two|three|four|five|\d+)\s+for\s*\$"
    r"|\bwhen\s+you\s+buy\s+(one|two|three|\d+)\b",
    re.I,
)
_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}

#: Tokens that are never the product noun: counts, prices, "4-7".
_NUMBERISH = re.compile(r"^[\d$¢%\-/.,]+$")


def _brands_in(text: str) -> list[tuple[str, ...]]:
    """Brand token-groups named by a row or product name."""
    tokens = catalog.tokenize(text)
    return [b for b in _BRANDS if set(b) <= tokens]


def _head_token(text: str) -> str | None:
    """The product noun: last substantive token of a name.

    Sizes, counts, filler and trailing flavor modifiers are skipped, so
    "Italian Sausage - Mild" yields "sausage" and "Whole Milk Plain Greek
    Yogurt, 32 oz" yields "yogurt".
    """
    toks = [
        t for t in catalog._ordered_tokens(text) if not _NUMBERISH.match(t)
    ]
    while toks and toks[-1] in _TRAILING_MODIFIERS:
        toks.pop()
    return toks[-1] if toks else None


def _quantity_required(text: str) -> int | None:
    """Units the row requires buying ("buy 2", "$1 off 2", "2 for $5")."""
    m = _QUANTITY_RE.search(text)
    if not m:
        return None
    word = next(g for g in m.groups() if g is not None)
    return _WORDNUM.get(word.lower(), int(word) if word.isdigit() else 1)


def veto_reason(
    term: str,
    *,
    offer_name: str,
    product_name: str,
    score: float,
) -> str | None:
    """Why this deal row cannot apply to this product, or None if it may.

    ``term`` is the row fragment that scored best (what the head gate
    checks); ``offer_name`` is the whole row (what brand, conflict and
    quantity gates check); ``product_name`` is the resolved product name;
    ``score`` is the catalog overlap score. The first failing gate wins and
    its reason is returned for audit.
    """
    offer_tokens = catalog.tokenize(offer_name)
    product_tokens = catalog.tokenize(product_name)

    # 1. Quantity-gated rows must name the product exactly. A "buy 2" row
    #    that only shares some words is reaching for a neighboring product.
    qty = _quantity_required(offer_name)
    if qty is not None and qty >= 2 and score < 1.0:
        return (
            f"quantity-gated (buy {qty}) without full product coverage "
            f"(score {score:.2f})"
        )

    # 2. Conflicting brands veto. A branded row may match an unbranded
    #    product, but never a product carrying a different brand.
    offer_brands = set(_brands_in(offer_name))
    product_brands = set(_brands_in(product_name))
    if offer_brands and product_brands and offer_brands.isdisjoint(product_brands):
        ob = "/".join(" ".join(b) for b in sorted(offer_brands))
        pb = "/".join(" ".join(b) for b in sorted(product_brands))
        return f"brand conflict: offer names {ob}, product names {pb}"

    # 3. The row's product noun must appear in the product. The row is
    #    *about* its head noun ("yogurt", "fajita", "sausage"); if the
    #    product does not contain it, the shared words are modifiers.
    head = _head_token(term)
    if head is not None and head not in product_tokens:
        return f"offer head {head!r} not in product"

    # 4. Conflicting line qualifiers veto, either direction.
    for group_a, group_b in _CONFLICTS:
        a, b = set(group_a), set(group_b)
        if (a <= offer_tokens and b <= product_tokens) or (
            b <= offer_tokens and a <= product_tokens
        ):
            return (
                f"conflicting qualifiers: {' '.join(group_a)} "
                f"vs {' '.join(group_b)}"
            )

    return None


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
                # The score decides candidacy; the strictness gates decide
                # eligibility. A vetoed row is dropped, not scored lower.
                veto = veto_reason(
                    m.term,
                    offer_name=offer.name,
                    product_name=target,
                    score=m.score,
                )
                if veto is not None:
                    continue
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
