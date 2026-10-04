# H-E-B Browser Adapter — runbook

Phase 1 of the DealForge H-E-B integration: a browser-driven client that
searches heb.com, reads product pages, and lists digital coupons and weekly-ad
items. Read-only. No login, no cart, no coupon clipping.

## Why a browser

heb.com sits behind Imperva/Incapsula. Plain HTTP clients (curl, requests)
are walled off; a real Chromium session loads the site normally (verified
2026-10-04, no login, no CAPTCHA). So the adapter drives Chromium via
Playwright and parses the rendered HTML. Do not "optimize" this back to
requests -- the WAF flags repeated non-browser traffic from one IP with 502s
and an injected sensor script.

## Setup

```bash
pip install -e ".[dev]"        # or pip install -e .
playwright install chromium   # one-time browser download
```

## Usage

```python
from dealforge.providers.heb.browser import HEBBrowserClient

with HEBBrowserClient() as heb:          # persistent profile, headless
    heb.set_store("77008")               # The Heights H-E-B #737 (default)
    for p in heb.search("eggs", limit=10):
        print(p.name, p.price, p.location)
    detail = heb.get_product_by_url(p.url)
    coupons = heb.list_coupons()         # all pages, read-only
    ad = heb.weekly_ad()
```

Demo (fixture mode, no browser needed):

```bash
python -m dealforge.heb_demo eggs
python -m dealforge.heb_demo "chicken breast" --live --zip 77008
```

Every result has `to_promotion()` to normalize into the shared `Promotion`
model used by the optimizer.

## Etiquette (hard rules)

- **Read-only.** No login, no account creation, no cart, no clipping, no
  personal info. `list_coupons()` never clicks Clip.
- **Human pacing.** 2-5s jittered delays between page actions; one retry on a
  transient failure, then raise.
- **Bot walls abort.** A CAPTCHA / Incapsula challenge raises `BotWallError`
  immediately. Never attempt to solve it.
- **No datacenter bulk runs.** Interactive, user-initiated use only.
- H-E-B's `robots.txt` disallows `/graphql`; this client does not touch the
  GraphQL endpoint at all -- it only loads the same public pages a shopper
  sees.

## Selector maintenance

All CSS selectors live in `SELECTORS` in
`src/dealforge/providers/heb/parse.py`. They are best-effort values from the
2026-10-04 recon and **must be re-verified against the live site** before any
bulk run. The parsers try each selector in order and tolerate missing fields
(returning `None`), so drift degrades gracefully instead of crashing -- but
verify anyway.

Needs live verification (could not be confirmed without a live session):

1. Exact card/field selectors on search, product, coupons, and weekly-ad
   pages (see `SELECTORS`).
2. Store-selector modal flow in `set_store()` (button/textbox/option roles).
3. Coupons pagination control shape (next-page link vs. load-more button).
4. Whether the product-URL slug is cosmetic -- `get_product(item_id)` assumes
   the page resolves by item ID. `get_product_by_url()` with the real URL from
   search results is the safe path until confirmed.

## Testing

```bash
pytest tests/ -q
```

Parser tests run against authored fixtures in `tests/fixtures/heb_*.html`
(mirroring documented page structures, not scraped). The Playwright driver
itself is not unit-tested -- it needs a live browser.
