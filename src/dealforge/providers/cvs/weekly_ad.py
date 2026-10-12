"""Public ZIP-scoped ad candidates. Ad group identifiers are never product SKUs."""

import asyncio
from dataclasses import asdict
from decimal import Decimal
from enum import Enum
import json
import re
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from dealforge.providers.cvs.pricing import comparison_time, valid_window
from dealforge.providers.cvs.sale_story import parse_item
from dealforge.shopper_settings import shopping_zip

BASE = "https://backflipp.wishabi.com/flipp/"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch(url):
    if not url.startswith(BASE) or urlsplit(url).hostname != "backflipp.wishabi.com":
        raise ValueError("Only public Flipp ad URLs are supported")
    with build_opener(_NoRedirect).open(Request(url, headers={"Accept": "application/json"}), timeout=15) as response:
        raw = response.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError("Ad response too large")
    return json.loads(raw, parse_float=Decimal)


def _json(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(v) for v in value]
    return value


def _options(query, zip_code, limit):
    if (not isinstance(query, str) or not query.strip() or len(query) > 100
            or any(ord(c) < 32 for c in query) or not isinstance(zip_code, str)
            or not re.fullmatch(r"\d{5}", zip_code) or type(limit) is not int or not 1 <= limit <= 25):
        raise ValueError("Use a query, five-digit ZIP and limit 1–25")


def search_weekly_ad(query, *, zip_code=None, limit=10, as_of=None, fetch=_fetch):
    zip_code = shopping_zip("cvs", zip_code)
    _options(query, zip_code, limit)
    now = comparison_time(as_of)
    listing_url = BASE + "flyers?" + urlencode({"postal_code": zip_code, "locale": "en-us"})
    listing = fetch(listing_url)
    if not isinstance(listing, dict) or not isinstance(listing.get("flyers"), list):
        raise ValueError("Unexpected public flyer listing")
    flyers = []
    for f in listing["flyers"]:
        if not isinstance(f, dict) or f.get("merchant") != "CVS Pharmacy":
            continue
        if f.get("postal_code") != zip_code or not re.fullmatch(r"\d{1,12}", str(f.get("id"))):
            continue
        if valid_window(f, now) is None:
            flyers.append(f)
    # Preserve partial candidates from successful responses if a later flyer fails.
    result, errors, seen = [], [], set()
    for f in flyers[:4]:
        url = BASE + "items/search?" + urlencode({"postal_code": zip_code, "flyer_ids": f["id"], "q": query.strip()})
        try:
            payload = fetch(url)
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise ValueError("Unexpected ad item response")
            # Deliberately ignore ecom_items, coupons and unrelated recommendations.
            for item in payload["items"]:
                if (not isinstance(item, dict) or item.get("merchant_name") != "CVS Pharmacy"
                        or str(item.get("flyer_id")) != str(f["id"]) or valid_window(item, now) is not None
                        or not re.fullmatch(r"\d{1,15}", str(item.get("id")))):
                    continue
                key = (str(f["id"]), str(item["id"]))
                if key in seen:
                    continue
                seen.add(key)
                text_fields = {k: item.get(k) for k in ("name", "pre_price_text", "price_text", "post_price_text", "sale_story", "disclaimer_text")}
                if any(v is not None and not isinstance(v, str) for v in text_fields.values()):
                    continue
                if not text_fields["name"]:
                    continue
                raw_price = item.get("current_price")
                price = str(raw_price) if raw_price is not None else text_fields["price_text"]
                # A simple numeric ad price can enrich parser candidates; range,
                # multibuy and BOGO strings remain raw and require resolution.
                parsed_input = {**text_fields, "price_text": price}
                result.append({"ad_id": f"flipp-{key[0]}-{key[1]}", "flyer_id": key[0], "ad_item_id": key[1],
                    **text_fields, "advertised_price_text": price,
                    "valid_from": item["valid_from"], "valid_to": item["valid_to"],
                    "flyer_valid_from": f["valid_from"], "flyer_valid_to": f["valid_to"],
                    "observed_at": now.isoformat(), "search_zip_code": zip_code,
                    "source_url": url, "store_id": None, "product_id": None, "sku_id": None,
                    "promotions": [{**_json(asdict(p)), "stackable": None, "stacking_verified": False}
                                   for p in parse_item(parsed_input)],
                    "sku_membership_verified": False, "store_applicability_verified": False,
                    "stacking_verified": False, "checkout_verified": False})
        except (OSError, ValueError, TypeError, KeyError):
            errors.append({"flyer_id": str(f["id"]), "reason": "public_ad_response_unavailable_or_changed"})
    return {"status": "partial", "query": query.strip(), "zip_code": zip_code, "observed_at": now.isoformat(),
        "flyers_checked": len(flyers[:4]), "offers": result[:limit], "offers_complete": False, "errors": errors,
        "review": ["ad_identifiers_are_not_skus", "ad_footnotes_and_store_applicability_need_verification",
                   "ad_candidates_are_not_complete_checkout_rules"], "source_url": listing_url}


async def fetch_weekly_ad(query, **kwargs):
    try:
        kwargs["zip_code"] = shopping_zip("cvs", kwargs.get("zip_code"))
        _options(query, kwargs["zip_code"], kwargs.get("limit", 10))
    except ValueError:
        return {"status": "invalid_input", "reason": "invalid_ad_options", "offers": []}
    try:
        return await asyncio.to_thread(search_weekly_ad, query, **kwargs)
    except (OSError, ValueError, TypeError, KeyError):
        return {"status": "source_unavailable", "reason": "public_ad_source_unavailable_or_changed", "offers": []}
