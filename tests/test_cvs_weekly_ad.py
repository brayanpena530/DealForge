from copy import deepcopy
from decimal import Decimal

import pytest

from dealforge.providers.cvs.weekly_ad import search_weekly_ad

NOW = "2026-10-10T12:00:00-05:00"
DATES = {"valid_from": "2026-10-04T01:00:00-04:00", "valid_to": "2026-10-11T00:59:59-04:00"}


def fixture():
    flyers = {"flyers": [{"merchant": "CVS Pharmacy", "id": 123, "postal_code": "00000", **DATES},
        {"merchant": "CVS Pharmacy", "id": 124, "postal_code": "00000", **DATES,
         "valid_from": "2026-10-11T01:00:00-04:00", "valid_to": "2026-10-18T00:59:59-04:00"},
        {"merchant": "Other store", "id": 125, "postal_code": "00000", **DATES}]}
    item = {"id": 999, "flyer_id": 123, "merchant_name": "CVS Pharmacy", "name": "Select cereal",
        "current_price": Decimal("2.49"), "post_price_text": "WITH CARD",
        "sale_story": "Buy 2 get $5 ExtraBucks Rewards WITH CARD", **DATES}
    return flyers, {"items": [item], "ecom_items": [{"name": "Ignore", "current_price": 1.99}],
                    "coupons": [1234]}


def run(payloads=None, **kwargs):
    listing, payload = payloads or fixture()
    calls = []
    def fetch(url):
        calls.append(url)
        return listing if "flyers?" in url else payload
    return search_weekly_ad("cereal", as_of=NOW, fetch=fetch, **kwargs), calls


def test_current_zip_ad_only_and_no_sku_inference():
    result, calls = run()
    assert len(calls) == 2 and "flyer_ids=123" in calls[1]
    assert result["flyers_checked"] == 1 and len(result["offers"]) == 1
    offer = result["offers"][0]
    assert offer["advertised_price_text"] == "2.49"
    assert offer["sku_id"] is None and offer["product_id"] is None and offer["store_id"] is None
    assert [p["kind"] for p in offer["promotions"]] == ["sale_price", "extrabucks"]
    assert offer["promotions"][1]["reward_amount"] == "5"
    assert all(p["stackable"] is None and not p["stacking_verified"] for p in offer["promotions"])
    assert not offer["stacking_verified"] and not result["offers_complete"]


def test_skip_wrong_merchant_flyer_expired_item_and_duplicate():
    listing, payload = fixture()
    item = payload["items"][0]
    payload["items"] += [deepcopy(item), dict(item, id=1000, merchant_name="Other"),
        dict(item, id=1001, flyer_id=777), dict(item, id=1002, valid_to="2026-10-09T00:00:00-04:00")]
    assert len(run((listing, payload))[0]["offers"]) == 1


def test_missing_dates_zip_or_future_flyer_never_active():
    listing, payload = fixture()
    listing["flyers"][0]["valid_to"] = None
    assert run((listing, payload))[0]["offers"] == []
    listing, payload = fixture()
    listing["flyers"][0]["postal_code"] = "99999"
    assert run((listing, payload))[0]["offers"] == []


def test_later_source_failure_preserves_partial_candidates():
    listing, payload = fixture()
    listing["flyers"].append({"merchant": "CVS Pharmacy", "id": 126, "postal_code": "00000", **DATES})
    def fetch(url):
        if "flyers?" in url:
            return listing
        if "flyer_ids=126" in url:
            raise OSError("synthetic outage")
        return payload
    result = search_weekly_ad("cereal", as_of=NOW, fetch=fetch)
    assert len(result["offers"]) == 1 and result["errors"][0]["flyer_id"] == "126"


def test_bound_limit_and_query_encoded():
    listing, payload = fixture()
    payload["items"] *= 2
    result, calls = run((listing, payload), limit=1)
    assert len(result["offers"]) == 1
    for options in ({"zip_code": "bad"}, {"limit": True}, {"limit": 26}):
        with pytest.raises(ValueError):
            run(**options)


def test_unknown_or_multibuy_price_remains_ad_candidate():
    listing, payload = fixture()
    payload["items"][0].update(current_price=None, price_text="2/5.00", sale_story="")
    offer = run((listing, payload))[0]["offers"][0]
    assert offer["advertised_price_text"] == "2/5.00" and not offer["sku_membership_verified"]


def test_async_source_failures_are_not_reported_as_bad_user_input(monkeypatch):
    import asyncio
    import dealforge.providers.cvs.weekly_ad as app
    def unavailable(*args, **kwargs):
        raise ValueError("synthetic response schema changed")
    monkeypatch.setattr(app, "search_weekly_ad", unavailable)
    assert asyncio.run(app.fetch_weekly_ad("cereal"))["status"] == "source_unavailable"
    assert asyncio.run(app.fetch_weekly_ad("", limit=99))["status"] == "invalid_input"
