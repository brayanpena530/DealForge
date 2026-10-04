"""Parse H-E-B weekly-ad promo copy into :class:`Promotion` rules.

Input is a Flipp flyerkit product record. The promo text is split across four
fields (`pre_price_text`, `price_text`, `post_price_text`, `sale_story`) with no
consistent rule about which holds what -- the same offer may read
`price_text="5.96 ea. with yellow coupon"` + `sale_story="SAVE $1"` on one item
and put the whole thing in `sale_story` on the next. So the fields are joined
and parsed as one blob, with `price_text` additionally parsed on its own for a
concrete price.

Two H-E-B rules from the official coupon policy are encoded here rather than
left to the optimizer:

1. Item-level coupons never stack -- one coupon per qualifying item.
2. `$ off YOUR BASKET` offers *do* piggyback on top of item-level offers.

So item-level promotions get ``stackable=False`` and basket-level ones get
``stackable=True``.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from dealforge.models import Promotion, PromoKind, PromoSource

__all__ = ["parse_coupon_headline", "parse_item", "parse_text"]

_FIELDS = ("pre_price_text", "price_text", "post_price_text", "sale_story")

# --------------------------------------------------------------------------
# patterns
# --------------------------------------------------------------------------

_MONEY = r"\$?\s*(\d+(?:\.\d{1,2})?)"

# A basket-level offer. Checked first: "SAVE $10 OFF YOUR BASKET" would
# otherwise match the plain "SAVE $X" rule and be applied to a line item.
_BASKET = re.compile(rf"save\s+{_MONEY}\s*off\s+your\s+basket", re.I)

# "BUY one (1) X GET 2ND FREE" -- same product, so BOGO rather than BUNDLE.
_BOGO = re.compile(r"get\s+2nd\s+free", re.I)

# "GET FREE! <other product>" / "get Free!" / "get 4 items free!"
_BUNDLE = re.compile(r"get\s+(?:(\d+)\s+items?\s+)?free\s*!", re.I)

# Multi-buy pricing: "2 for 7.00", "4 for 10.00".
_N_FOR_M = re.compile(rf"\b(\d+)\s+for\s+{_MONEY}", re.I)

# "SAVE up to $2 per lb." -- the ceiling IS the discount here, unlike a bare
# "SAVE up to $X" trailing a bundle, which only values the free item.
_PER_LB = re.compile(rf"save\s+up\s+to\s+{_MONEY}\s*(?:per\s+)?lb", re.I)

_PERCENT = re.compile(r"(?:save\s+)?(\d+)\s*%\s*off|save\s+(\d+)\s*%", re.I)

_FLAT = re.compile(rf"save\s+{_MONEY}(?!\s*(?:off\s+your\s+basket|%))", re.I)

# "SAVE up to $2.87!" trailing a bundle -- an advertised ceiling, not a rule.
_MAX_VALUE = re.compile(rf"save\s+up\s+to\s+{_MONEY}", re.I)

# Thresholds. Quantity and spend are distinguished by the dollar sign, so the
# spend form is tried first.
_MIN_SPEND = re.compile(r"(?:when\s+you\s+)?buy\s+\$\s*(\d+(?:\.\d{1,2})?)", re.I)
_MIN_QTY_DIGIT = re.compile(r"when\s+you\s+buy\s+(\d+)\b(?!\s*(?:oz|ct|lb|pk|l\b))", re.I)
# "BUY one (1) ..." / "BUY two (2) ..." -- take the parenthesized digit.
_MIN_QTY_PAREN = re.compile(r"\bbuy\s+\w+\s*\((\d+)\)", re.I)

_CLIP = re.compile(r"yellow\s+coupons?", re.I)

# A bare leading price in `price_text`: "7.97 lb.", "3.99 ea.", "1.97".
_PRICE_TEXT = re.compile(r"^\s*(\d+(?:\.\d{1,2})?)\s*(lb|ea|each)?\b", re.I)

# Text that reads as promotional even when no rule matched.
_PROMO_ISH = re.compile(r"save|free|buy|coupon|off\b|deal", re.I)

# --------------------------------------------------------------------------
# digital-coupon headline grammar
# --------------------------------------------------------------------------
# Unlike flyer copy, coupon headlines never say SAVE: "$8.00 OFF ANY TWO (2)
# L'OREAL ..." / "$1 off 2 H-E-B Tortilla Chips ...". The quantity trails the
# amount instead of living in a "WHEN YOU BUY N" clause.
_COUPON_FLAT = re.compile(rf"{_MONEY}\s*off\b(?!\s*your\s+basket)", re.I)
_COUPON_QTY_TWO = re.compile(r"any\s+two\s*\(\s*2\s*\)", re.I)
_COUPON_QTY_N = re.compile(r"\boff\s+(\d+)\b", re.I)

# Weekly-ad deal mechanics: "Buy 2, get 1 free".
_BOGO_BUY_GET = re.compile(r"\bbuy\s+(\d+)\s*,\s*get\s+(\d+)\s+free\b", re.I)

# The free item is a different product the ad never names:
# "Combo Loco -- Buy this, get that free" / "FREE! with the purchase of ...".
_BUNDLE_THIS_THAT = re.compile(r"\bbuy\s+this\s*,\s*get\s+that\s+free\b", re.I)
_BUNDLE_FREE_WITH = re.compile(r"\bfree\s*!\s*with\s+the\s+purchase\s+of\b", re.I)


def _coupon_quantity(blob: str) -> int | None:
    """Quantity from coupon-headline phrasing: 'ANY TWO (2)' or 'off 2'."""
    if _COUPON_QTY_TWO.search(blob):
        return 2
    if m := _COUPON_QTY_N.search(blob):
        return int(m.group(1))
    return None


def _money(raw: str | None) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(raw.strip())
    except (InvalidOperation, AttributeError):
        return None


def _join(item: dict[str, Any]) -> str:
    parts: Iterable[str] = (str(item.get(f) or "").strip() for f in _FIELDS)
    return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()


def _parse_price_text(price_text: str | None) -> Decimal | None:
    """Pull a concrete unit price out of `price_text`, if it states one.

    Returns None for fragments like ``"WHEN YOU BUY $40 OF"`` or
    ``"SAVE up to $3.48!"``, which live in the same field but are not prices.
    """
    if not price_text:
        return None
    text = price_text.strip()
    if re.match(r"^\s*(save|when|buy|get)\b", text, re.I):
        return None
    if _N_FOR_M.match(text):  # handled as N_FOR_M, not a unit price
        return None
    match = _PRICE_TEXT.match(text)
    return _money(match.group(1)) if match else None


def _free_item_text(blob: str) -> str | None:
    """Best-effort name of the item you receive on a bundle offer.

    Takes what follows ``GET FREE!`` up to the advertised-value tail. Ad copy
    interleaves the free item with coupon boilerplate, so this is a label for
    display, never something to match a SKU against.
    """
    match = _BUNDLE.search(blob)
    if not match:
        return None
    tail = blob[match.end():]
    tail = re.split(_MAX_VALUE, tail)[0]
    tail = re.sub(r"with\s+yellow\s+coupons?\s+(?:in-store\s+or\s+online)?", " ", tail, flags=re.I)
    tail = re.sub(r"\s+", " ", tail).strip(" .,!-")
    return tail or None


def parse_text(blob: str, *, unit_price: Decimal | None = None) -> list[Promotion]:
    """Parse one joined promo blob into zero or more rules.

    Exposed separately from :func:`parse_item` so tests and other providers can
    feed it bare strings.
    """
    blob = re.sub(r"\s+", " ", blob or "").strip()
    if not blob:
        return []

    requires_clip = bool(_CLIP.search(blob))
    min_spend = _money(m.group(1)) if (m := _MIN_SPEND.search(blob)) else None
    min_qty = None
    if m := _MIN_QTY_DIGIT.search(blob):
        min_qty = int(m.group(1))
    elif m := _MIN_QTY_PAREN.search(blob):
        min_qty = int(m.group(1))

    def build(kind: PromoKind, **kwargs: Any) -> Promotion:
        kwargs.setdefault("requires_clip", requires_clip)
        # Yellow coupons are H-E-B's own, not a manufacturer's. That matters
        # because H-E-B's one-coupon-per-item rule applies regardless of origin,
        # so the solver needs the origin recorded even where it cannot stack.
        kwargs.setdefault(
            "source", PromoSource.STORE_COUPON if requires_clip else PromoSource.AD_SALE
        )
        return Promotion(kind=kind, raw=blob, **kwargs)

    # 1. Basket-level. Self-contained and the only stackable shape at H-E-B.
    if m := _BASKET.search(blob):
        caveats: tuple[str, ...] = ()
        confidence = 1.0
        if min_spend is None and min_qty is None:
            caveats = ("basket offer states no qualifying threshold",)
            confidence = 0.6
        return [
            build(
                PromoKind.BASKET_THRESHOLD,
                discount_amount=_money(m.group(1)),
                min_spend=min_spend,
                min_quantity=min_qty,
                basket_level=True,
                stackable=True,
                confidence=confidence,
                caveats=caveats,
            )
        ]

    max_value = _money(m.group(1)) if (m := _MAX_VALUE.search(blob)) else None
    per_lb = _PER_LB.search(blob)

    # 2. Buy-and-get offers, before the plain SAVE rules -- their trailing
    #    "SAVE up to $X" values the free item and is not a discount.
    if _BOGO.search(blob):
        return [
            build(
                PromoKind.BOGO,
                min_quantity=min_qty or 1,
                get_quantity=1,
                discount_percent=Decimal(100),  # "GET 2ND FREE" -- the second is 100% off
                max_value=max_value,
            )
        ]

    # "Buy 2, get 1 free" -- weekly-ad deal mechanics.
    if m := _BOGO_BUY_GET.search(blob):
        return [
            build(
                PromoKind.BOGO,
                min_quantity=int(m.group(1)),
                get_quantity=int(m.group(2)),
                discount_percent=Decimal(100),
            )
        ]

    if m := _BUNDLE.search(blob):
        free_item = _free_item_text(blob)
        return [
            build(
                PromoKind.BUNDLE,
                min_quantity=min_qty,
                min_spend=min_spend,
                get_quantity=int(m.group(1)) if m.group(1) else 1,
                get_free_item=free_item,
                max_value=max_value,
                confidence=0.8 if free_item else 0.5,
                caveats=() if free_item else ("free item not identifiable from ad copy",),
            )
        ]

    # A different item is free but the ad never names it: Combo Loco
    # "Buy this, get that free", or "FREE! with the purchase of ...".
    if _BUNDLE_THIS_THAT.search(blob) or _BUNDLE_FREE_WITH.search(blob):
        return [
            build(
                PromoKind.BUNDLE,
                min_quantity=min_qty,
                min_spend=min_spend,
                get_quantity=1,
                confidence=0.5,
                caveats=("free item not identifiable from ad copy",),
            )
        ]

    # 3. Multi-buy pricing.
    if m := _N_FOR_M.search(blob):
        qty, total = int(m.group(1)), _money(m.group(2))
        return [
            build(
                PromoKind.N_FOR_M,
                bundle_quantity=qty,
                unit_price=(total / qty).quantize(Decimal("0.01")) if total else None,
                discount_amount=total,
                min_quantity=min_qty or qty,
            )
        ]

    # 4. Per-pound savings, before the generic percent/flat rules.
    if per_lb:
        return [
            build(
                PromoKind.SAVE_PER_LB,
                discount_amount=_money(per_lb.group(1)),
                unit_price=unit_price,
                max_value=_money(per_lb.group(1)),
                confidence=0.9,
                caveats=('advertised as "up to" -- actual saving may be lower',),
            )
        ]

    if m := _PERCENT.search(blob):
        return [
            build(
                PromoKind.SAVE_PERCENT,
                discount_percent=Decimal(m.group(1) or m.group(2)),
                min_quantity=min_qty,
                min_spend=min_spend,
            )
        ]

    # 5. Flat dollars off. When the ad also states a price, that price is the
    #    post-discount one: emit SALE_PRICE carrying the stated saving, so the
    #    engine cannot subtract the discount a second time.
    if m := _FLAT.search(blob):
        amount = _money(m.group(1))
        qty = min_qty or _coupon_quantity(blob)
        if unit_price is not None:
            return [
                build(
                    PromoKind.SALE_PRICE,
                    unit_price=unit_price,
                    discount_amount=amount,
                    min_quantity=qty,
                )
            ]
        return [
            build(
                PromoKind.SAVE_FLAT,
                discount_amount=amount,
                min_quantity=qty,
                min_spend=min_spend,
            )
        ]

    # 5b. Coupon-headline flat: "$8.00 OFF ANY TWO (2) ..." -- no SAVE verb.
    if m := _COUPON_FLAT.search(blob):
        return [
            build(
                PromoKind.SAVE_FLAT,
                discount_amount=_money(m.group(1)),
                min_quantity=_coupon_quantity(blob),
                min_spend=min_spend,
            )
        ]

    # 6. Price with no offer attached.
    if unit_price is not None:
        return [build(PromoKind.SALE_PRICE, unit_price=unit_price, min_quantity=min_qty)]

    if _PROMO_ISH.search(blob):
        return [
            build(
                PromoKind.UNKNOWN,
                confidence=0.0,
                caveats=("promotional text not matched by any rule",),
            )
        ]

    return []


def parse_coupon_headline(headline: str) -> list[Promotion]:
    """Parse one digital-coupon headline into Promotion rules.

    Digital coupons always require clipping and are H-E-B-issued, unlike flyer
    rows where clipping is inferred from "yellow coupon" text -- so the parsed
    rules are forced to ``requires_clip=True`` and ``source=STORE_COUPON``.
    """
    import dataclasses

    promos = parse_text(headline)
    return [
        dataclasses.replace(p, requires_clip=True, source=PromoSource.STORE_COUPON)
        for p in promos
    ]


def parse_item(item: dict[str, Any]) -> list[Promotion]:
    """Parse one Flipp flyerkit product record."""
    unit_price = _parse_price_text(item.get("price_text"))
    return parse_text(_join(item), unit_price=unit_price)
