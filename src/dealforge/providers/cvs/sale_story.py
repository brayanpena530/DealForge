"""Parse CVS weekly-ad promo copy into :class:`Promotion` rules.

Structurally different from the H-E-B parser, for one reason: **a CVS string
routinely encodes several independent offers that all apply at once.**

    5.99 WITH CARD Also get savings with $5.00 on 2 Digital mfr coupon
    + Buy 2 get $6 ExtraBucks Rewards

That is three rules -- a sale price, a manufacturer coupon, and a loyalty
reward -- and CVS lets you take all three on the same purchase. So instead of
H-E-B's ordered "first rule wins and returns", this scans the blob for every
known offer shape and emits one :class:`Promotion` per match.

Overlapping matches are the hazard that creates. ``Buy 1 get 1 50% OFF``
contains ``50% OFF``, which the standalone percent rule matches too. Matches are
therefore taken in priority order and each one *consumes its span*; a later,
lower-priority rule matching inside a consumed span is discarded. See
:func:`_scan`.

Two CVS facts are encoded here rather than left to the optimizer:

1. **ExtraBucks are not a discount.** They land in `reward_amount`, never
   `discount_amount`, so an engine cannot subtract them from today's cash. They
   are future currency and belong in the objective as ``λ × reward_amount``.
2. **CVS stacks, H-E-B does not.** Every CVS promotion is ``stackable=True``.
   The real constraint is at most one offer per (item, `source`), which the
   `source` field now makes expressible.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable

from dealforge.models import Promotion, PromoKind, PromoSource

__all__ = ["parse_item", "parse_text"]

_FIELDS = ("pre_price_text", "price_text", "post_price_text", "sale_story")

_MONEY = r"\$?\s*(\d+(?:\.\d{1,2})?)"

# "ExtraBucks", "Extrabucks", "ExtraBucks Rewards", "ExtraBucks®◆ Rewards" --
# the ad is inconsistent about case, the registered mark and footnote glyphs.
_EB = r"extra\s?bucks"

# --- ExtraBucks: the two triggers CVS uses -------------------------------
# "Spend $20 get $5 ExtraBucks Rewards®◆"
_EB_SPEND = re.compile(rf"spend\s+{_MONEY}\s+get\s+{_MONEY}\s*[^\w$]*{_EB}", re.I)
# "Buy 2 get $6 ExtraBucks Rewards®⧫"
_EB_QTY = re.compile(rf"buy\s+(\d+)\s+get\s+{_MONEY}\s*[^\w$]*{_EB}", re.I)

# --- Buy-one-get-one, in all the spellings the ad uses --------------------
# "Buy 1 get 1 50% OFF*", "Buy 2 get 2 FREE*", "Buy 2 get 3rd FREE**"
#
# The ordinal suffix changes the meaning: "get 2 FREE" is two free items,
# "get 3rd FREE" is one free item that happens to be the third. Group 3 captures
# the suffix so the two can be told apart.
_BOGO_N = re.compile(
    r"buy\s+(\d+)\s+get\s+(\d+)(st|nd|rd|th)?\s+(?:(\d+)\s*%\s*off|free)",
    re.I,
)
# "BOGO FREE*", "BOGO 50% off*", "Mix & Match BOGO 50% Off"
_BOGO_SHORT = re.compile(r"\bbogo\s+(?:(\d+)\s*%\s*off|free)", re.I)

# --- Coupons --------------------------------------------------------------
# "$5.00 on 3 Digital mfr coupon", "$2.00 Digital mfr coupon", "Digital mfr coupon"
_MFR = re.compile(
    rf"(?:{_MONEY}\s*(?:on\s+(\d+)\s*)?)?digital\s+mfr\.?\s+coupon", re.I
)
# "Get a $3 off 2 ExtraCare coupon^ in the CVS app", "$2 off ExtraCare coupon^"
_EXTRACARE = re.compile(
    rf"{_MONEY}\s*off\s*(\d+)?\s*extra\s?care\s+coupon", re.I
)

# --- Price shapes ---------------------------------------------------------
# "2/ 9.00", optionally followed by "or $5.49 ea." / "or reg retail ea."
_N_FOR_M = re.compile(rf"\b(\d+)\s*/\s*{_MONEY}", re.I)
_FALLBACK_EA = re.compile(rf"or\s+{_MONEY}\s*ea\b", re.I)
# Standalone "25% OFF", not the tail of a BOGO (handled by span consumption).
_PERCENT = re.compile(r"(\d+)\s*%\s*off", re.I)

# A bare leading price: "1.99", "14.99 WITH CARD", "2.99 ✧ WITH CARD +CRV".
_PRICE_TEXT = re.compile(r"^\s*(\d+(?:\.\d{1,2})?)\s*(?:ea\b|each\b)?")

_CARD = re.compile(r"with\s+card", re.I)
# Text that reads as an offer even when no rule matched it.
_PROMO_ISH = re.compile(r"save|free|buy\b|coupon|%|spend|extra\s?bucks|bogo", re.I)
# Rows that are page furniture, not products.
_NOT_AN_OFFER = re.compile(r"^\s*(with\s+card|\+?crv|reg\.?\s+retail)\s*$", re.I)


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

    Returns None for the many rows where the field holds an offer rather than a
    price -- ``"Buy 1 get 1 50% OFF*"``, ``"Spend $20 get $5 ExtraBucks"`` --
    and for ``"2/ 9.00"``, which is priced as N_FOR_M instead.
    """
    if not price_text:
        return None
    text = price_text.strip()
    if re.match(r"^\s*(buy|spend|get|save|bogo|mix|all|\$|\d+\s*/)", text, re.I):
        return None
    match = _PRICE_TEXT.match(text)
    return _money(match.group(1)) if match else None


def _percent(raw: str | None) -> tuple[Decimal | None, tuple[str, ...], float]:
    """Validate an advertised percentage.

    The live flyer contains ``Buy 1 get 1 140% OFF*`` -- a typo in CVS's own ad
    copy. A parser that passes 140 through would hand the optimizer a promotion
    that pays the shopper to take the item. Flag it instead of inventing a value.
    """
    pct = _money(raw)
    if pct is None or pct <= 100:
        return pct, (), 1.0
    return pct, (f"ad states {pct}% off, which is not a valid discount",), 0.0


class _Scan:
    """Accumulates promotions while tracking which spans of text are spoken for."""

    def __init__(self, blob: str) -> None:
        self.blob = blob
        self.found: list[tuple[int, Promotion]] = []
        self._taken: list[tuple[int, int]] = []

    def _overlaps(self, span: tuple[int, int]) -> bool:
        return any(span[0] < end and start < span[1] for start, end in self._taken)

    def take(self, pattern: re.Pattern[str], build: Callable[[re.Match[str]], Promotion | None]) -> None:
        for match in pattern.finditer(self.blob):
            if self._overlaps(match.span()):
                continue
            promo = build(match)
            if promo is None:
                continue
            self._taken.append(match.span())
            self.found.append((match.start(), promo))

    def results(self) -> list[Promotion]:
        """In the order the offers appear in the ad, so a plan reads naturally."""
        return [promo for _, promo in sorted(self.found, key=lambda pair: pair[0])]


def parse_text(blob: str, *, unit_price: Decimal | None = None) -> list[Promotion]:
    """Parse one joined promo blob into zero or more rules.

    Rules are taken in priority order, each consuming the text it matched:
    ExtraBucks, then BOGO, then coupons, then multi-buy, then a bare percent.
    """
    blob = re.sub(r"\s+", " ", blob or "").strip()
    if not blob or _NOT_AN_OFFER.match(blob):
        return []

    requires_card = bool(_CARD.search(blob))

    def build(kind: PromoKind, **kwargs: Any) -> Promotion:
        kwargs.setdefault("requires_card", requires_card)
        kwargs.setdefault("stackable", True)
        return Promotion(kind=kind, raw=blob, **kwargs)

    scan = _Scan(blob)

    # 1. ExtraBucks. First because "Buy 2 get $6 ExtraBucks" would otherwise be
    #    read as a buy-one-get-one, and because the reward must not become a
    #    discount: it goes to `reward_amount`, never `discount_amount`.
    scan.take(
        _EB_SPEND,
        lambda m: build(
            PromoKind.EXTRABUCKS,
            min_spend=_money(m.group(1)),
            reward_amount=_money(m.group(2)),
            source=PromoSource.LOYALTY_REWARD,
            basket_level=True,
        ),
    )
    scan.take(
        _EB_QTY,
        lambda m: build(
            PromoKind.EXTRABUCKS,
            min_quantity=int(m.group(1)),
            reward_amount=_money(m.group(2)),
            source=PromoSource.LOYALTY_REWARD,
        ),
    )

    # 2. Buy-one-get-one, before the bare percent rule -- "Buy 1 get 1 50% OFF"
    #    is 50% off the *second* item, not 50% off the item.
    def _bogo_n(m: re.Match[str]) -> Promotion:
        pct, caveats, confidence = _percent(m.group(4))
        return build(
            PromoKind.BOGO,
            min_quantity=int(m.group(1)),
            get_quantity=1 if m.group(3) else int(m.group(2)),
            discount_percent=pct if pct is not None else Decimal(100),
            caveats=caveats,
            confidence=confidence,
        )

    def _bogo_short(m: re.Match[str]) -> Promotion:
        pct, caveats, confidence = _percent(m.group(1))
        return build(
            PromoKind.BOGO,
            min_quantity=1,
            get_quantity=1,
            discount_percent=pct if pct is not None else Decimal(100),
            caveats=caveats,
            confidence=confidence,
        )

    scan.take(_BOGO_N, _bogo_n)
    scan.take(_BOGO_SHORT, _bogo_short)

    # 3. Coupons. A bare "Digital mfr coupon" with no stated value is real ad
    #    copy -- the value only shows in the app -- so it is kept with the
    #    amount unknown rather than dropped.
    def _mfr(m: re.Match[str]) -> Promotion:
        amount = _money(m.group(1))
        return build(
            PromoKind.SAVE_FLAT,
            discount_amount=amount,
            min_quantity=int(m.group(2)) if m.group(2) else None,
            source=PromoSource.MFR_COUPON,
            requires_clip=True,
            confidence=1.0 if amount else 0.4,
            caveats=() if amount else ("coupon value not stated in the ad",),
        )

    scan.take(_MFR, _mfr)
    scan.take(
        _EXTRACARE,
        lambda m: build(
            PromoKind.SAVE_FLAT,
            discount_amount=_money(m.group(1)),
            min_quantity=int(m.group(2)) if m.group(2) else None,
            source=PromoSource.STORE_COUPON,
            requires_clip=True,
        ),
    )

    # 4. Multi-buy. "2/ 9.00 or $5.49 ea." states a single-unit fallback the
    #    model has no field for; it is recorded as a caveat so a plan can still
    #    warn that buying one is not half the pair price.
    def _n_for_m(m: re.Match[str]) -> Promotion:
        qty, total = int(m.group(1)), _money(m.group(2))
        caveats: tuple[str, ...] = ()
        if fallback := _FALLBACK_EA.search(blob):
            caveats = (f"buying fewer than {qty} costs ${fallback.group(1)} each",)
        return build(
            PromoKind.N_FOR_M,
            bundle_quantity=qty,
            unit_price=(total / qty).quantize(Decimal("0.01")) if total else None,
            discount_amount=total,
            min_quantity=qty,
            caveats=caveats,
        )

    scan.take(_N_FOR_M, _n_for_m)

    # 5. A percent that survived BOGO consumption is a straight percent off.
    def _pct(m: re.Match[str]) -> Promotion | None:
        pct, caveats, confidence = _percent(m.group(1))
        if pct is None:
            return None
        return build(
            PromoKind.SAVE_PERCENT,
            discount_percent=pct,
            caveats=caveats,
            confidence=confidence,
        )

    scan.take(_PERCENT, _pct)

    promos = scan.results()

    # 6. A stated price stands alongside the offers above rather than replacing
    #    them -- at CVS the sale price is the base that coupons then come off.
    if unit_price is not None and not any(p.kind is PromoKind.N_FOR_M for p in promos):
        promos.insert(0, build(PromoKind.SALE_PRICE, unit_price=unit_price))

    if promos:
        return promos

    if _PROMO_ISH.search(blob):
        return [
            build(
                PromoKind.UNKNOWN,
                confidence=0.0,
                caveats=("promotional text not matched by any rule",),
            )
        ]

    return []


def parse_item(item: dict[str, Any]) -> list[Promotion]:
    """Parse one Flipp flyerkit product record.

    Category-header rows carry a name and nothing else; they return no
    promotions, which is correct -- they are not purchasable.
    """
    unit_price = _parse_price_text(item.get("price_text"))
    return parse_text(_join(item), unit_price=unit_price)
