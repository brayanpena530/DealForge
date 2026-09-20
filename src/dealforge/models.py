"""Normalized promotion model, shared across retailer providers.

Deliberately retailer-agnostic: a CVS `Spend $25 get $8 ExtraBucks` and an H-E-B
`WHEN YOU BUY $40 ... SAVE $10 OFF YOUR BASKET` both land here as
`BASKET_THRESHOLD`. The optimizer reasons over these, never over ad copy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class PromoSource(str, Enum):
    """Who issued the offer.

    Load-bearing for stacking. CVS lets a store sale, a manufacturer coupon, a
    store coupon and a loyalty reward all apply to the same item; H-E-B allows
    exactly one item-level coupon regardless of origin. The solver decides using
    this field, not by knowing which retailer it is looking at.
    """

    #: Shelf/circular price set by the retailer.
    AD_SALE = "ad_sale"
    #: Manufacturer coupon, clipped digitally or paper.
    MFR_COUPON = "mfr_coupon"
    #: Retailer-issued coupon (CVS ExtraCare coupon, H-E-B yellow coupon).
    STORE_COUPON = "store_coupon"
    #: Loyalty currency earned, not money saved now (CVS ExtraBucks).
    LOYALTY_REWARD = "loyalty_reward"


class PromoKind(str, Enum):
    """How a promotion changes what the shopper pays."""

    #: Shelf/sale price is stated outright. `unit_price` is authoritative.
    SALE_PRICE = "sale_price"
    #: Fixed dollars off an item, e.g. `SAVE $2`.
    SAVE_FLAT = "save_flat"
    #: Percentage off an item, e.g. `SAVE 20%`.
    SAVE_PERCENT = "save_percent"
    #: Dollars off per pound, for items sold by weight.
    SAVE_PER_LB = "save_per_lb"
    #: Multi-buy pricing, e.g. `2 for 7.00`.
    N_FOR_M = "n_for_m"
    #: Buy N of a thing, get M of the same thing free.
    BOGO = "bogo"
    #: Buy a qualifying item (or spend), get a *different* item free.
    BUNDLE = "bundle"
    #: Spend a threshold across a set, get money off the whole basket.
    BASKET_THRESHOLD = "basket_threshold"
    #: Earn loyalty currency (CVS ExtraBucks) by spending or buying a quantity.
    #: Does NOT reduce today's bill — see `reward_amount`.
    EXTRABUCKS = "extrabucks"
    #: Text recognized as promotional but not parsed into a rule.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Promotion:
    """One parsed promotional rule.

    Money is `Decimal`. Never floats — this feeds a pricing engine.

    `raw` is always retained. Ad copy carries footnote markers and exclusions
    that no parser fully captures, so every plan shown to a user should be able
    to fall back to quoting the source text.
    """

    kind: PromoKind
    raw: str

    #: Dollars off. On `SALE_PRICE` this is the *stated savings vs regular*,
    #: not an additional discount to apply on top of `unit_price`.
    discount_amount: Decimal | None = None
    #: Percent off. On `BOGO` this is the percent off the *received* item, so a
    #: plain buy-one-get-one-free carries 100.
    discount_percent: Decimal | None = None

    #: Loyalty currency earned. Deliberately a separate field from
    #: `discount_amount`: ExtraBucks are not money off today's bill. An engine
    #: that subtracts them from cash is wrong. Value them as λ × reward_amount.
    reward_amount: Decimal | None = None

    #: Resulting price per unit, when the ad states one.
    unit_price: Decimal | None = None
    #: The N in `N for $M`.
    bundle_quantity: int | None = None

    #: Qualifying thresholds. Quantity and spend are mutually exclusive in
    #: practice but both are modeled, since some ads state each.
    min_quantity: int | None = None
    min_spend: Decimal | None = None

    #: What you receive on BOGO/BUNDLE.
    get_quantity: int | None = None
    get_free_item: str | None = None

    #: Advertised ceiling, from `SAVE up to $X`. Upper bound, not a guarantee.
    max_value: Decimal | None = None

    #: Who issued it. Drives which offers may combine.
    source: PromoSource = PromoSource.AD_SALE

    #: Applies to the order total rather than a line item.
    basket_level: bool = False
    #: Requires clipping/activating a coupon first.
    requires_clip: bool = False
    #: Requires the retailer's loyalty card be presented (CVS `WITH CARD`).
    requires_card: bool = False
    #: May combine with another offer on the same item. H-E-B forbids this for
    #: item-level coupons but permits it for basket-level ones.
    stackable: bool = False

    #: 1.0 = every field came from an explicit match. Lower when the parser
    #: inferred, or when a threshold was referenced but never stated.
    confidence: float = 1.0
    #: Human-readable reasons confidence is below 1.0.
    caveats: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence out of range: {self.confidence}")

    @property
    def reduces_cash_today(self) -> bool:
        """False for loyalty currency, which is earned rather than saved."""
        return self.kind is not PromoKind.EXTRABUCKS

    @property
    def is_certain(self) -> bool:
        return self.confidence >= 1.0
