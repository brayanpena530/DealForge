"""Browser-driven H-E-B adapter.

H-E-B's storefront sits behind Imperva/Incapsula: plain HTTP clients are
walled off, but a real Chromium session loads the site normally (verified
2026-10-04, no login, no CAPTCHA). This client drives that real browser and
hands page HTML to :mod:`dealforge.providers.heb.parse`, which turns it into
typed results.

Hard rules, no exceptions:

- **Read-only.** No login, no account creation, no cart, no coupon clipping,
  no personal information entered anywhere.
- **Human pacing.** 2-5s jittered delays between page actions; one retry on a
  transient failure, then raise.
- **Bot walls abort.** If a CAPTCHA / Incapsula challenge appears, raise
  :class:`BotWallError` immediately. Never attempt to solve it.
- **Selectors are centralized** in :mod:`dealforge.providers.heb.parse`
  (:data:`SELECTORS`) -- re-verify them against the live site before a bulk
  run; H-E-B renames things without notice.

Do not run bulk scrapes from a datacenter IP: repeated non-browser traffic
gets flagged (502 + injected sensor script). This client is for interactive,
user-initiated use.
"""

from __future__ import annotations

import random
import time
from typing import Any

from dealforge.providers.heb.parse import (
    has_next_page,
    parse_coupons_page,
    parse_product_page,
    parse_search_results,
    parse_weekly_ad,
)
from dealforge.providers.heb.schemas import (
    DEFAULT_STORE,
    CouponOffer,
    ProductDetail,
    ProductResult,
    Store,
    WeeklyAdItem,
)

__all__ = [
    "BotWallError",
    "HEBBrowserClient",
    "COUPONS_URL",
    "WEEKLY_AD_URL",
]

BASE_URL = "https://www.heb.com"
COUPONS_URL = f"{BASE_URL}/digital-coupon/coupon-selection/all-coupons"
WEEKLY_AD_URL = f"{BASE_URL}/collections/weekly-ad"

#: Page text that means we hit the bot wall. Checked case-insensitively.
_BOT_WALL_MARKERS = (
    "incapsula",
    "additional security check",
    "hcaptcha",
    "request unsuccessful",
    "access denied",
    "_incapsula_resource",
)


class BotWallError(RuntimeError):
    """Raised when H-E-B serves a bot challenge. Never solve it; abort."""


class HEBBrowserClient:
    """Drives a real Chromium session against heb.com.

    Use as a context manager::

        with HEBBrowserClient() as heb:
            heb.set_store("77008")
            for product in heb.search("eggs"):
                print(product.name, product.price, product.location)
    """

    def __init__(
        self,
        *,
        store_zip: str = DEFAULT_STORE.zip_code,
        profile_dir: str | None = None,
        headless: bool = True,
        min_delay: float = 2.0,
        max_delay: float = 5.0,
    ) -> None:
        self.store_zip = store_zip
        self.profile_dir = profile_dir
        self.headless = headless
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    # -- lifecycle ------------------------------------------------------

    def _require_playwright(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "playwright is not installed. Run: pip install playwright "
                "&& playwright install chromium"
            ) from exc
        return sync_playwright

    def start(self) -> "HEBBrowserClient":
        """Launch Chromium (persistent profile so store choice sticks)."""
        sync_playwright = self._require_playwright()
        self._playwright = sync_playwright().start()
        if self.profile_dir:
            self._context = self._playwright.chromium.launch_persistent_context(
                self.profile_dir, headless=self.headless
            )
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        else:
            self._browser = self._playwright.chromium.launch(headless=self.headless)
            self._context = self._browser.new_context()
            self._page = self._context.new_page()
        return self

    def close(self) -> None:
        for handle in (self._context, self._browser, self._playwright):
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
        self._page = self._context = self._browser = self._playwright = None

    def __enter__(self) -> "HEBBrowserClient":
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- pacing + guards ------------------------------------------------

    def _pace(self) -> None:
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    def _guard_bot_wall(self, html: str, url: str) -> None:
        lowered = html.lower()
        for marker in _BOT_WALL_MARKERS:
            if marker in lowered:
                raise BotWallError(
                    f"bot challenge detected at {url} (marker: {marker!r}); "
                    "aborting -- never attempt to solve it"
                )

    def _get_html(self, url: str, *, retries: int = 1) -> str:
        """Load a URL with pacing and bot-wall detection. One retry, then raise."""
        if self._page is None:
            raise RuntimeError("client not started; use 'with HEBBrowserClient():'")
        attempt = 0
        while True:
            self._pace()
            self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
            self._pace()
            html = self._page.content()
            try:
                self._guard_bot_wall(html, url)
            except BotWallError:
                raise
            if len(html) > 10_000 or attempt >= retries:
                return html
            attempt += 1  # suspiciously small page; one retry before giving up

    # -- store ----------------------------------------------------------

    def set_store(self, zip_code: str) -> Store:
        """Select the shopping store by ZIP via the store-selector modal.

        Selector assumptions (re-verify live): the header shows a
        "Shopping at ..." button; the modal has a ZIP textbox and result rows.
        """
        page = self._page
        self._get_html(BASE_URL + "/")
        self._pace()
        page.get_by_role("button", name=re_compile("shopping at")).click()
        self._pace()
        page.get_by_role("textbox", name=re_compile("zip")).fill(zip_code)
        self._pace()
        # First result row; the Heights store is top result for 77008.
        page.get_by_role("option").first.click()
        self._pace()
        html = page.content()
        self._guard_bot_wall(html, BASE_URL)
        self.store_zip = zip_code
        return Store(
            store_number=DEFAULT_STORE.store_number,
            name=DEFAULT_STORE.name,
            address=DEFAULT_STORE.address,
            zip_code=zip_code,
        )

    # -- catalog --------------------------------------------------------

    def search(self, query: str, limit: int = 20) -> list[ProductResult]:
        """Search H-E-B and return product cards with price + aisle location."""
        from urllib.parse import quote_plus

        html = self._get_html(f"{BASE_URL}/search?q={quote_plus(query)}")
        return parse_search_results(html, limit=limit)

    def get_product(self, item_id: str, slug: str = "-") -> ProductDetail:
        """Open a product detail page by numeric item ID.

        H-E-B URLs are /product-detail/{slug}/{itemId}; the page resolves by
        item ID and the slug is cosmetic, but that assumption still needs live
        verification -- prefer :meth:`get_product_by_url` with the real URL
        from :class:`ProductResult` when you have it.
        """
        return self.get_product_by_url(f"{BASE_URL}/product-detail/{slug}/{item_id}")

    def get_product_by_url(self, url: str) -> ProductDetail:
        html = self._get_html(url)
        return parse_product_page(html, url)

    # -- deals ----------------------------------------------------------

    def list_coupons(
        self, department: str | None = None, *, max_pages: int = 20
    ) -> list[CouponOffer]:
        """All digital coupons, paginated. Read-only: never clicks Clip."""
        offers: list[CouponOffer] = []
        page = self._page
        self._get_html(COUPONS_URL)
        pages = 0
        while pages < max_pages:
            html = page.content()
            self._guard_bot_wall(html, COUPONS_URL)
            offers.extend(parse_coupons_page(html))
            pages += 1
            if not has_next_page(html):
                break
            self._pace()
            # Next-page / load-more control; selector in parse.SELECTORS.
            from dealforge.providers.heb.parse import SELECTORS

            clicked = False
            for selector in SELECTORS["coupon_next_page"]:
                control = page.query_selector(selector)
                if control:
                    control.click()
                    clicked = True
                    break
            if not clicked:
                break
            self._pace()
        return offers

    def weekly_ad(self) -> list[WeeklyAdItem]:
        """This week's ad items with sale prices, locations and deal copy."""
        html = self._get_html(WEEKLY_AD_URL)
        return parse_weekly_ad(html)


def re_compile(pattern: str):
    """Case-insensitive regex for Playwright role/text matching."""
    import re

    return re.compile(pattern, re.I)
