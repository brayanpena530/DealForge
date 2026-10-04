"""Pick the best legal combination of H-E-B deals for a located list.

The H-E-B rules encoded here come from the official coupon policy (see
``docs/research/heb-data-sources.md`` and ``docs/research/heb-deal-optimization.md``):

- **Exactly one item-level coupon per item**, regardless of origin
  (digital or paper). Given several candidates, the single max-savings one
  wins. Greedy per-item choice is optimal: items are independent, so there
  is no cross-item game to solve -- the only coupling is the basket
  threshold, evaluated afterwards.
- **Weekly-ad shelf offers always apply** underneath the coupon: a sale
  price, or an ad BOGO / multi-buy mechanic. Where the ad states several
  mechanics for one item they are alternatives, so the best one wins --
  H-E-B does not stack two shelf offers on the same item.
- **Piggyback exception**: a cents-off coupon may apply to the "buy" item
  of an ad BOGO. That falls out naturally: the ad BOGO is valued in the
  sale layer, the coupon in the coupon layer.
- **Basket-level ``$X OFF YOUR BASKET`` promos stack on top** of everything
  when their own qualifier is met. All qualifying basket promos apply.
- **5% debit-card rebate on H-E-B private-label items** is payment-side: it
  does not change the register total, so it is reported separately and only
  when ``debit_card=True``.

Money is :class:`decimal.Decimal`, quantized to cents per line with
``ROUND_HALF_UP`` (the register rounds per line, not per basket).

What Phase 3 does *not* model (documented, not silently wrong):

- Quantities are per single unit purchased. A ``$8 OFF ANY TWO (2)``
  coupon is valued at $4.00/unit and the "buy 2" requirement is surfaced
  as a qualifier note -- the plan does not force you to buy two.
- ``SAVE_PER_LB`` coupons cannot be valued without a weighed quantity and
  are skipped (noted on the line).
- BUNDLE / Combo Loco "get that free" offers name a free item whose price
  is unknown, so they are surfaced as notes, not savings.
- A manufacturer coupon whose face value exceeds the item price spills the
  excess onto the basket per H-E-B policy; the optimizer clips the coupon
  at the item price and ignores the spill (rare, and basket attribution
  needs quantities).
- Basket-threshold qualification is measured against the post-item-deal
  subtotal. H-E-B qualifies on pre-coupon spend in some cases; the plan
  may therefore understate qualification slightly.
- A full ILP solver for multi-unit / cross-item bundle games is future
  work. The greedy choice is optimal for the one-coupon-per-item rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from dealforge.models import Promotion, PromoKind, PromoSource
from dealforge.providers.heb.deals import DealAssignment, DealMatch, DealOffer
from dealforge.providers.heb.route import LocatedItem, plan_route

__all__ = [
    "PlanLine",
    "ShoppingPlan",
    "optimize",
]

_CENT = Decimal("0.01")

#: The H-E-B debit card pays 5% back on private-label items.
_DEBIT_REBATE_RATE = Decimal("0.05")

#: A basket threshold missed by no more than this gets a "you're $X away"
#: note. Heuristic, not policy -- see the module docstring.
_NEAR_MISS_BAND = Decimal("10.00")

#: Name fragments identifying H-E-B private-label brands. ProductResult
#: carries no brand object in the browser adapter, so this is a heuristic
#: until ``brand.isOwnBrand`` is wired through.
_PRIVATE_LABEL_MARKERS = (
    "h-e-b",
    "hill country fare",
    "central market",
    "higher harvest",
    "mi tienda",
)

#: Sources that make a promotion a *coupon* (one-per-item) rather than a
#: shelf offer.
_COUPON_SOURCES = frozenset({PromoSource.STORE_COUPON, PromoSource.MFR_COUPON})

#: Coupon kinds the optimizer can value per unit. SALE_PRICE lives in the
#: sale layer; BUNDLE names an unpriced free item; UNKNOWN/EXTRABUCKS are
#: not H-E-B register discounts.
_VALUABLE_COUPON_KINDS = frozenset(
    {
        PromoKind.SAVE_FLAT,
        PromoKind.SAVE_PERCENT,
        PromoKind.SAVE_PER_LB,
        PromoKind.N_FOR_M,
        PromoKind.BOGO,
    }
)


def _cents(value: Decimal) -> Decimal:
    """Quantize to cents, the way a register rounds a line."""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _is_coupon(promo: Promotion) -> bool:
    """An item-level coupon: exactly one may apply per item at H-E-B."""
    return (
        not promo.basket_level
        and promo.kind in _VALUABLE_COUPON_KINDS
        and (promo.requires_clip or promo.source in _COUPON_SOURCES)
    )


def _is_private_label(name: str | None) -> bool:
    """Heuristic: H-E-B's own brands carry the name in the product title."""
    if not name:
        return False
    lowered = name.lower()
    return any(marker in lowered for marker in _PRIVATE_LABEL_MARKERS)


def _qualifier_text(promo: Promotion) -> str | None:
    """Human-readable purchase requirement, e.g. "buy 2" or "spend $40"."""
    if promo.min_quantity and promo.min_quantity > 1:
        return f"buy {promo.min_quantity}"
    if promo.min_spend is not None:
        return f"spend ${promo.min_spend}"
    return None


def _coupon_unit_savings(promo: Promotion, unit_price: Decimal) -> Decimal | None:
    """Per-unit saving a coupon yields against ``unit_price``, or None.

    None means "cannot be valued from the rule alone" -- never "worth
    nothing". Unvaluable coupons are skipped, not treated as zero.
    """
    if promo.kind is PromoKind.SAVE_FLAT:
        if promo.discount_amount is None:
            return None
        qty = promo.min_quantity if promo.min_quantity and promo.min_quantity > 0 else 1
        return promo.discount_amount / qty
    if promo.kind is PromoKind.SAVE_PERCENT:
        if promo.discount_percent is None:
            return None
        return unit_price * promo.discount_percent / Decimal(100)
    if promo.kind is PromoKind.SAVE_PER_LB:
        # Needs a weighed quantity the list never states.
        return None
    if promo.kind is PromoKind.N_FOR_M:
        if promo.unit_price is None or promo.unit_price >= unit_price:
            return None
        return unit_price - promo.unit_price
    if promo.kind is PromoKind.BOGO:
        buy = promo.min_quantity or 1
        get = promo.get_quantity or 1
        if buy + get <= 0:
            return None
        return unit_price * Decimal(get) / Decimal(buy + get)
    return None


def _sale_unit_price(
    list_price: Decimal | None, promos: list[Promotion]
) -> tuple[Decimal | None, Promotion | None]:
    """Best shelf offer: sale price, ad BOGO, or multi-buy -- best one wins.

    A weekly-ad ``SALE_PRICE`` only applies when it *lowers* the resolved
    price. That keeps live runs honest: heb.com search prices usually
    already reflect the ad, and re-applying the ad price would double
    count. In fixture mode the resolved prices are list prices, so the ad
    price applies normally.
    """
    best_price = list_price
    best_promo: Promotion | None = None

    def consider(price: Decimal | None, promo: Promotion) -> None:
        nonlocal best_price, best_promo
        if price is None:
            return
        if best_price is None or price < best_price:
            best_price = price
            best_promo = promo

    for promo in promos:
        if promo.basket_level or _is_coupon(promo):
            continue
        if promo.kind is PromoKind.SALE_PRICE and promo.unit_price is not None:
            consider(promo.unit_price, promo)
        elif promo.kind is PromoKind.BOGO and best_price is not None:
            saving = _coupon_unit_savings(promo, best_price)
            if saving is not None:
                consider(best_price - saving, promo)
        elif promo.kind is PromoKind.N_FOR_M and promo.unit_price is not None:
            consider(promo.unit_price, promo)
    return best_price, best_promo


@dataclass(frozen=True)
class PlanLine:
    """One shopping-list item with its optimized pricing."""

    query: str
    product_name: str | None
    #: Verbatim in-store location, or None when unplaced.
    location_text: str | None
    list_price: Decimal | None
    #: Price after the weekly-ad shelf offer, before any coupon.
    ad_price: Decimal | None
    #: The single winning coupon (H-E-B: exactly one per item).
    coupon: Promotion | None
    #: Headline to clip, e.g. "$1.00 OFF ANY ONE (1) H-E-B Large White Eggs".
    coupon_headline: str | None
    #: Purchase requirement on the coupon, e.g. "buy 2".
    coupon_qualifier: str | None
    coupon_savings: Decimal
    #: What the register shows for this line.
    final_price: Decimal | None
    #: list_price - final_price. Zero when the price is unknown.
    line_savings: Decimal
    #: 5% debit rebate on private-label items; does not change the register.
    debit_rebate: Decimal
    notes: tuple[str, ...] = ()
    unplaced: bool = False


@dataclass(frozen=True)
class ShoppingPlan:
    """The optimized plan: lines in walk order plus basket-level results."""

    lines: tuple[PlanLine, ...]
    #: Headlines to clip, deduped, in walk order. The plan never clips.
    coupons_to_clip: tuple[str, ...]
    #: Basket promos that qualified and were applied.
    basket_promos: tuple[Promotion, ...]
    basket_savings: Decimal
    #: Near-miss notes, e.g. "$3.20 away from $10 off".
    basket_notes: tuple[str, ...]
    total_list: Decimal
    #: What the register shows: lines minus basket savings, floored at zero.
    total_final: Decimal
    total_savings: Decimal
    debit_rebate_total: Decimal
    #: Register total minus the payment-side rebate.
    total_net: Decimal
    debit_card: bool = False


def _build_line(
    item: LocatedItem,
    matches: tuple[DealMatch, ...],
    *,
    debit_card: bool,
) -> PlanLine:
    """Price one item: sale layer, then the single best coupon."""
    promos = [m.promotion for m in matches]
    list_price = item.product.price if item.product is not None else None

    ad_price, sale_promo = _sale_unit_price(list_price, promos)
    base = ad_price if ad_price is not None else list_price

    # The one coupon: max per-unit saving wins; ties break on headline for
    # determinism.
    best: DealMatch | None = None
    best_saving: Decimal | None = None
    skipped: list[str] = []
    for m in matches:
        promo = m.promotion
        if not _is_coupon(promo):
            continue
        if base is None:
            skipped.append(m.offer.name)
            continue
        saving = _coupon_unit_savings(promo, base)
        if saving is None:
            skipped.append(m.offer.name)
            continue
        if best_saving is None or saving > best_saving:
            best, best_saving = m, saving

    notes: list[str] = []
    for m in matches:
        promo = m.promotion
        if promo.kind is PromoKind.BUNDLE and not promo.basket_level:
            label = promo.get_free_item or "a free item"
            notes.append(f"Bundle available: {promo.raw} (get {label})")
    for name in skipped:
        notes.append(f"Could not value coupon (needs quantity/price): {name}")

    coupon_savings = Decimal("0")
    coupon = coupon_headline = coupon_qualifier = None
    final_price = base
    if best is not None and best_saving is not None and base is not None:
        # H-E-B clips a coupon's face value at the item price: no cash back.
        coupon_savings = _cents(min(best_saving, base))
        final_price = _cents(base - coupon_savings)
        coupon = best.promotion
        coupon_headline = best.offer.name
        coupon_qualifier = _qualifier_text(best.promotion)
    elif base is not None:
        final_price = _cents(base)

    line_savings = (
        _cents(list_price - final_price)
        if list_price is not None and final_price is not None
        else Decimal("0")
    )

    rebate = Decimal("0")
    product_name = item.product.name if item.product is not None else None
    if debit_card and final_price is not None and _is_private_label(product_name):
        rebate = _cents(final_price * _DEBIT_REBATE_RATE)

    return PlanLine(
        query=item.query,
        product_name=product_name,
        location_text=item.location.raw if item.location is not None else None,
        list_price=list_price,
        # ad_price is only set when a shelf offer actually lowered the price;
        # otherwise the field would misleadingly echo the list price.
        ad_price=_cents(ad_price) if sale_promo is not None and ad_price is not None else None,
        coupon=coupon,
        coupon_headline=coupon_headline,
        coupon_qualifier=coupon_qualifier,
        coupon_savings=coupon_savings,
        final_price=final_price,
        line_savings=line_savings,
        debit_rebate=rebate,
        notes=tuple(notes),
        unplaced=item.unplaced,
    )


def _apply_basket(
    offers: tuple[DealOffer, ...], subtotal: Decimal
) -> tuple[tuple[Promotion, ...], Decimal, tuple[str, ...]]:
    """Apply qualifying basket promos; note near-misses.

    Qualification is measured against the post-item-deal subtotal (see the
    module docstring for the approximation). Every qualifying basket promo
    applies -- basket-level offers stack on top of item offers at H-E-B.
    """
    applied: list[Promotion] = []
    savings = Decimal("0")
    notes: list[str] = []
    for offer in offers:
        for promo in offer.promotions:
            if not promo.basket_level or promo.kind is not PromoKind.BASKET_THRESHOLD:
                continue
            if promo.discount_amount is None:
                notes.append(f"Basket offer has no stated value: {offer.name}")
                continue
            threshold = promo.min_spend
            if threshold is None:
                # No stated qualifier: applies outright, with the parser's
                # caveat retained on the promotion itself.
                applied.append(promo)
                savings += promo.discount_amount
                continue
            if subtotal >= threshold:
                applied.append(promo)
                savings += promo.discount_amount
            else:
                gap = threshold - subtotal
                if gap <= _NEAR_MISS_BAND:
                    notes.append(
                        f"${gap:.2f} away from ${promo.discount_amount:.2f} off "
                        f"(spend ${threshold:.2f})"
                    )
    return tuple(applied), _cents(savings), tuple(notes)


def optimize(
    assignment: DealAssignment,
    *,
    store_number: str | int | None = "737",
    debit_card: bool = False,
) -> ShoppingPlan:
    """Turn a deal assignment into a priced, route-ordered shopping plan.

    Lines follow :func:`plan_route` walk order. Greedy per-item coupon
    choice is optimal under H-E-B's one-coupon-per-item rule; see the module
    docstring for the modeled rules and the known approximations.
    """
    ordered_items = plan_route(
        [item for item, _ in assignment.per_item], store_number=store_number
    )
    matches_by_item = {id(item): matches for item, matches in assignment.per_item}

    lines = tuple(
        _build_line(item, matches_by_item[id(item)], debit_card=debit_card)
        for item in ordered_items
    )

    subtotal = sum(
        (line.final_price for line in lines if line.final_price is not None),
        Decimal("0"),
    )
    basket_promos, basket_savings, basket_notes = _apply_basket(
        assignment.basket_offers, subtotal
    )

    total_list = _cents(
        sum(
            (line.list_price for line in lines if line.list_price is not None),
            Decimal("0"),
        )
    )
    total_final = _cents(max(Decimal("0"), subtotal - basket_savings))
    total_savings = _cents(total_list - total_final)
    rebate_total = _cents(sum((line.debit_rebate for line in lines), Decimal("0")))

    seen: set[str] = set()
    coupons_to_clip: list[str] = []
    for line in lines:
        if line.coupon_headline and line.coupon_headline not in seen:
            seen.add(line.coupon_headline)
            coupons_to_clip.append(line.coupon_headline)
    for promo in basket_promos:
        if promo.requires_clip and promo.raw not in seen:
            seen.add(promo.raw)
            coupons_to_clip.append(promo.raw)

    return ShoppingPlan(
        lines=lines,
        coupons_to_clip=tuple(coupons_to_clip),
        basket_promos=basket_promos,
        basket_savings=basket_savings,
        basket_notes=basket_notes,
        total_list=total_list,
        total_final=total_final,
        total_savings=total_savings,
        debit_rebate_total=rebate_total,
        total_net=_cents(total_final - rebate_total),
        debit_card=debit_card,
    )
