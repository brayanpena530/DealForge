"""Typed results from the H-E-B browser adapter.

Every dataclass carries the raw strings the page showed alongside the parsed
values, so a plan built on these can always be audited back to what H-E-B
displayed. Money is :class:`decimal.Decimal`, matching the rest of DealForge.

``to_promotion()`` normalizes each result into the shared :class:`Promotion`
model in :mod:`dealforge.models`, so browser-sourced data flows into the same
optimizer as the weekly-ad pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from dealforge.models import Promotion
from dealforge.shopper_settings import preferences, shopping_store

__all__ = [
    "DEFAULT_STORE",
    "CouponOffer",
    "ProductDetail",
    "ProductResult",
    "Store",
    "WeeklyAdItem",
]


@dataclass(frozen=True)
class Store:
    """An H-E-B storefront used as the pricing/location context."""

    store_number: int
    name: str
    address: str
    zip_code: str


#: Example H-E-B, Fictional example store; shopper defaults belong in local preferences.
DEFAULT_STORE = Store(
    store_number=0,
    name="Example H-E-B",
    address="Example store address",
    zip_code="",
)


def default_store():
    """Load private defaults; the public fallback has no geographic identity."""
    data = preferences("heb")
    return Store(store_number=int(shopping_store("heb") or 0),
        name=data.get("name", "Unconfigured H-E-B store"),
        address=data.get("address", ""), zip_code=data.get("zip_code", ""))


@dataclass(frozen=True)
class ProductResult:
    """One card from H-E-B search results."""

    item_id: str
    name: str
    url: str
    price: Decimal | None = None
    price_unit: str | None = None  #: "each", "lb", ...
    unit_price_text: str | None = None  #: raw "($0.24/ct)"
    #: Raw in-store location, verbatim: "In Dairy on the Left Wall, A24".
    location: str | None = None
    snap_eligible: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProductDetail:
    """A full H-E-B product detail page."""

    item_id: str
    name: str
    url: str
    price: Decimal | None = None
    price_unit: str | None = None
    unit_price_text: str | None = None
    location: str | None = None
    snap_eligible: bool = False
    #: Verbatim deal badges, e.g. "yellow coupon", "Combo Loco".
    deal_badges: tuple[str, ...] = ()
    #: Verbatim "More ways to save" lines, e.g. the 5% Visa perk.
    more_ways_to_save: tuple[str, ...] = ()
    highlights: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    def to_promotion(self) -> list[Promotion]:
        """Deal badges parsed into Promotion rules; empty when none apply."""
        from dealforge.providers.heb.sale_story import parse_text

        promos: list[Promotion] = []
        for badge in self.deal_badges:
            promos.extend(parse_text(badge))
        return promos


@dataclass(frozen=True)
class CouponOffer:
    """One digital coupon from the all-coupons page."""

    headline: str
    expiry: str | None = None  #: raw "Expires 10/17/2026"
    limit: str | None = None  #: raw "Limit 1 per customer"
    redeem_channel: str | None = None  #: raw "Redeem in store"
    department: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_promotion(self) -> list[Promotion]:
        """Headline parsed as an H-E-B-issued, clip-required coupon."""
        from dealforge.providers.heb.sale_story import parse_coupon_headline

        return parse_coupon_headline(self.headline)


@dataclass(frozen=True)
class WeeklyAdItem:
    """One item from the weekly-ad collection page."""

    name: str
    url: str
    sale_price: Decimal | None = None
    regular_price: Decimal | None = None
    unit_price_text: str | None = None
    location: str | None = None
    #: Verbatim "Coupon available" tags.
    coupon_tags: tuple[str, ...] = ()
    #: Verbatim deal mechanics: "Combo Loco — Buy this, get that free", etc.
    deal_text: str | None = None
    expiry: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_promotion(self) -> list[Promotion]:
        """Deal mechanics parsed; falls back to a plain sale price."""
        from dealforge.providers.heb.sale_story import parse_text
        from dealforge.models import PromoKind

        if self.deal_text:
            return parse_text(self.deal_text, unit_price=self.sale_price)
        if self.sale_price is None:
            return []
        discount = None
        if self.regular_price is not None and self.regular_price > self.sale_price:
            discount = self.regular_price - self.sale_price
        return [
            Promotion(
                kind=PromoKind.SALE_PRICE,
                raw=f"On Sale {self.sale_price}",
                unit_price=self.sale_price,
                discount_amount=discount,
            )
        ]
