"""Pure HTML parsing for H-E-B pages.

These functions take page HTML and return the typed results in
:mod:`dealforge.providers.heb.schemas`. They never touch the network, so they
are unit-testable against saved fixtures.

All CSS selectors live in :data:`SELECTORS`. They were verified against
the live heb.com DOM on 2026-10-04 (no login, real Chromium): H-E-B relies on
``data-component`` / ``data-qe-id`` attributes, with ``data-testid`` used
sparsely. Re-verify if parsing starts returning ``None`` values after a site
redesign -- when a selector drifts, update it in :data:`SELECTORS`, since
every parse function reads from this dict.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin

from dealforge.providers.heb.schemas import (
    CouponOffer,
    ProductDetail,
    ProductResult,
    WeeklyAdItem,
)

__all__ = [
    "SELECTORS",
    "parse_coupons_page",
    "parse_product_page",
    "parse_search_results",
    "parse_weekly_ad",
]

BASE_URL = "https://www.heb.com"

# ---------------------------------------------------------------------------
# Selectors -- verified live against heb.com on 2026-10-04 (real Chromium,
# no login). H-E-B leans on data-component / data-qe-id attributes; data-testid
# is sparse, so those two are preferred. Hashed CSS-module class names
# (e.g. __HJiPb) are listed only as last-resort fallbacks -- they can rotate
# on any deploy. Each entry is tried in order; the first selector with a hit
# wins. Re-verify if parsing starts returning Nones after a site redesign.
# ---------------------------------------------------------------------------
SELECTORS: dict[str, tuple[str, ...]] = {
    # Search results page -----------------------------------------------
    "search_card": (
        '[data-component="product-card"]',
        '[data-qe-id="productCard"]',
        '[data-testid="productCardContainer"]',
    ),
    "search_name": (
        '[data-qe-id="productTitle"]',
        '[data-testid="product-name"]',
        "h2.product-name",
        "a.product-link",
    ),
    "search_link": (
        'a[href^="/product-detail/"]',
        'a[data-testid="product-link"]',
        "a.product-link",
    ),
    "search_price": (
        '[data-component="product-price-sale-price"]',
        '[data-testid="product-price"]',
        "span.price",
        "div.price",
    ),
    "search_unit_price": (
        '[data-component="product-price-price-per-unit"]',
        '[data-testid="unit-price"]',
        "span.unit-price",
    ),
    "search_location": (
        'p[data-testid="product-location"]',
        'button[data-testid="product-card-map-button"]',
        '[data-testid="store-location"]',
        "button.store-location",
        "span.aisle-info",
    ),
    "search_snap": (
        "p.SnapEbtLabel_snapEbtLabel__054ID",
        '[data-testid="snap-eligible"]',
        "span.snap-badge",
    ),
    # Product detail page -----------------------------------------------
    "detail_name": (
        "h1.ProductDetailTitle_titleText__n6zkq",
        '[data-testid="product-title"]',
        "h1.product-title",
    ),
    "detail_price": (
        '[data-component="product-price-sale-price"]',
        '[data-testid="product-price"]',
        "span.price",
    ),
    "detail_unit_price": (
        '[data-component="product-price-price-per-unit"]',
        '[data-testid="unit-price"]',
        "span.unit-price",
    ),
    "detail_location": (
        'button[data-qe-id="nearbyStoreMapButton"]',
        "button.ProductDetailStoreMapButton_button__TcWVB",
        '[data-testid="store-location"]',
        "button.store-location",
    ),
    "detail_deal_badge": (
        '[data-testid="deal-badge"]',
        "span.deal-badge",
        "div.promo-badge",
    ),
    "detail_savings_line": (
        'ul[data-testid="product-detail-promos-container"] li',
        '[data-testid="ways-to-save"] li',
        "div.more-ways-to-save li",
    ),
    "detail_highlight": (
        'div[data-qe-id="productHighlights"] button',
        '[data-testid="highlight"]',
        "ul.highlights li",
    ),
    "detail_snap": (
        '[data-testid="snap-eligible"]',
        "span.snap-badge",
    ),
    # Coupons page --------------------------------------------------------
    "coupon_card": (
        'div[data-testid="coupon-card-body"]',
        '[data-qe-id="couponCard"]',
        'div[data-testid="coupon-grid-column"]',
        '[data-testid="coupon-card"]',
        "article.coupon",
    ),
    "coupon_headline": (
        'p[data-qe-id="couponDescription"]',
        '[data-testid="coupon-headline"]',
        "h3.coupon-title",
    ),
    "coupon_expiry": (
        '[data-qe-id="couponExpiration"]',
        '[data-testid="coupon-expiry"]',
        "span.expiry",
    ),
    "coupon_limit": (
        '[data-qe-id="couponRedemptionLimit"]',
        '[data-testid="coupon-limit"]',
        "span.coupon-limit",
    ),
    "coupon_redeem": (
        '[data-testid="coupon-redeem"]',
        "span.redeem-channel",
    ),
    "coupon_department": (
        '[data-testid="coupon-department"]',
        "span.coupon-department",
    ),
    "coupon_next_page": (
        'a[data-qe-id="paginationNext"]',
        'nav[aria-label="Pagination Navigation"] a[data-qe-id="paginationListNum"]',
        '[data-testid="pagination-next"]',
        "a.pagination-next",
        "button.load-more",
    ),
    # Weekly ad page ------------------------------------------------------
    "ad_card": (
        '[data-component="product-card"]',
        '[data-qe-id="productCard"]',
        '[data-testid="weekly-ad-item"]',
        "article.ad-item",
    ),
    "ad_name": (
        '[data-qe-id="productTitle"]',
        '[data-testid="ad-item-name"]',
        "h3.ad-item-name",
    ),
    "ad_link": (
        'a[href^="/product-detail/"]',
        'a[data-testid="ad-item-link"]',
        "a.ad-item-link",
    ),
    "ad_sale_price": (
        '[data-component="product-price-sale-price"]',
        '[data-testid="ad-sale-price"]',
        "span.sale-price",
    ),
    "ad_regular_price": (
        'p[data-testid="strike-through-price"]',
        '[data-component="product-price-strike-through-price"]',
        '[data-testid="ad-regular-price"]',
        "span.regular-price",
    ),
    "ad_unit_price": (
        '[data-component="product-price-price-per-unit"]',
        '[data-testid="ad-unit-price"]',
        "span.unit-price",
    ),
    "ad_location": (
        'p[data-testid="product-location"]',
        'button[data-testid="product-card-map-button"]',
        '[data-testid="ad-location"]',
        "span.aisle-info",
    ),
    "ad_coupon_tag": (
        '[data-qe-id="couponLabel"]',
        '[data-testid="ad-coupon-tag"]',
        "span.coupon-tag",
    ),
    "ad_deal_text": (
        '[data-qe-id="snipe"]',
        'p[data-qe-id="couponDescription"]',
        '[data-testid="ad-deal-text"]',
        "div.deal-mechanics",
    ),
    "ad_expiry": (
        '[data-testid="ad-expiry"]',
        "span.ad-expiry",
    ),
}

_MONEY = re.compile(r"\$\s*(\d+(?:\.\d{1,2})?)")
_ITEM_ID = re.compile(r"/(\d{4,})(?:[/?#]|$)")


def _soup(html: str):
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser")


def _select(card, key: str):
    """First matching element for a selector key, or None."""
    for selector in SELECTORS[key]:
        found = card.select_one(selector)
        if found is not None:
            return found
    return None


def _text(element) -> str | None:
    if element is None:
        return None
    text = element.get_text(" ", strip=True)
    return text or None


def _all_texts(root, key: str) -> list[str]:
    """Non-empty texts for a selector key, deduplicated.

    An element matching several fallback selectors is reported once.
    """
    seen: set[int] = set()
    texts: list[str] = []
    for selector in SELECTORS[key]:
        for element in root.select(selector):
            if id(element) in seen:
                continue
            seen.add(id(element))
            if text := _text(element):
                texts.append(text)
    return texts


def _money(text: str | None) -> Decimal | None:
    if not text:
        return None
    match = _MONEY.search(text)
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None


def _price_unit(text: str | None) -> str | None:
    """'each' from '$4.79 each'; 'lb' from '$1.97 lb.'."""
    if not text:
        return None
    match = re.search(r"\b(each|ea|lb|lbs|oz|ct)\b\.?", text, re.I)
    return match.group(1).lower() if match else None


def _item_id(url: str | None) -> str | None:
    if not url:
        return None
    match = _ITEM_ID.search(url)
    return match.group(1) if match else None


def _abs_url(href: str | None) -> str | None:
    if not href:
        return None
    return urljoin(BASE_URL, href)


# ---------------------------------------------------------------------------
# page parsers
# ---------------------------------------------------------------------------


def parse_search_results(html: str, *, limit: int = 20) -> list[ProductResult]:
    """Parse an H-E-B search results page into product cards."""
    soup = _soup(html)
    cards = []
    for selector in SELECTORS["search_card"]:
        cards = soup.select(selector)
        if cards:
            break
    results: list[ProductResult] = []
    for card in cards[:limit]:
        link = _select(card, "search_link")
        href = link.get("href") if link else None
        url = _abs_url(href)
        price_text = _text(_select(card, "search_price"))
        location_text = _text(_select(card, "search_location"))
        results.append(
            ProductResult(
                item_id=_item_id(url) or "",
                name=_text(_select(card, "search_name")) or "",
                url=url or "",
                price=_money(price_text),
                price_unit=_price_unit(price_text),
                unit_price_text=_text(_select(card, "search_unit_price")),
                location=location_text,
                snap_eligible=_select(card, "search_snap") is not None,
                raw={"price_text": price_text, "location_text": location_text},
            )
        )
    return results


def parse_product_page(html: str, url: str) -> ProductDetail:
    """Parse an H-E-B product detail page."""
    soup = _soup(html)
    price_text = _text(_select(soup, "detail_price"))
    badges = _all_texts(soup, "detail_deal_badge")
    savings = _all_texts(soup, "detail_savings_line")
    highlights = _all_texts(soup, "detail_highlight")
    location_text = _text(_select(soup, "detail_location"))
    return ProductDetail(
        item_id=_item_id(url) or "",
        name=_text(_select(soup, "detail_name")) or "",
        url=url,
        price=_money(price_text),
        price_unit=_price_unit(price_text),
        unit_price_text=_text(_select(soup, "detail_unit_price")),
        location=location_text,
        snap_eligible=_select(soup, "detail_snap") is not None,
        deal_badges=tuple(badges),
        more_ways_to_save=tuple(savings),
        highlights=tuple(highlights),
        raw={"price_text": price_text, "location_text": location_text},
    )


def parse_coupons_page(html: str) -> list[CouponOffer]:
    """Parse one page of the all-coupons listing."""
    soup = _soup(html)
    cards = []
    for selector in SELECTORS["coupon_card"]:
        cards = soup.select(selector)
        if cards:
            break
    offers: list[CouponOffer] = []
    for card in cards:
        headline = _text(_select(card, "coupon_headline"))
        if not headline:
            continue
        offers.append(
            CouponOffer(
                headline=headline,
                expiry=_text(_select(card, "coupon_expiry")),
                limit=_text(_select(card, "coupon_limit")),
                redeem_channel=_text(_select(card, "coupon_redeem")),
                department=_text(_select(card, "coupon_department")),
                raw={"card_html": str(card)[:2000]},
            )
        )
    return offers


def parse_weekly_ad(html: str) -> list[WeeklyAdItem]:
    """Parse the weekly-ad collection page."""
    soup = _soup(html)
    cards = []
    for selector in SELECTORS["ad_card"]:
        cards = soup.select(selector)
        if cards:
            break
    items: list[WeeklyAdItem] = []
    for card in cards:
        link = _select(card, "ad_link")
        url = _abs_url(link.get("href")) if link else None
        sale_text = _text(_select(card, "ad_sale_price"))
        regular_text = _text(_select(card, "ad_regular_price"))
        tags = _all_texts(card, "ad_coupon_tag")
        items.append(
            WeeklyAdItem(
                name=_text(_select(card, "ad_name")) or "",
                url=url or "",
                sale_price=_money(sale_text),
                regular_price=_money(regular_text),
                unit_price_text=_text(_select(card, "ad_unit_price")),
                location=_text(_select(card, "ad_location")),
                coupon_tags=tuple(tags),
                deal_text=_text(_select(card, "ad_deal_text")),
                expiry=_text(_select(card, "ad_expiry")),
                raw={"sale_text": sale_text, "regular_text": regular_text},
            )
        )
    return items


def has_next_page(html: str) -> bool:
    """Whether the coupons/ad listing shows a next-page / load-more control."""
    soup = _soup(html)
    return _select(soup, "coupon_next_page") is not None
