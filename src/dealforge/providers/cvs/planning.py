"""Read-only shopping-list orchestration over the existing CVS capabilities."""

import asyncio
from datetime import datetime
import re
from zoneinfo import ZoneInfo
from dealforge.shopper_settings import shopping_zip, shopping_store, store_timezone as local_timezone

from dealforge.providers.cvs.catalog import product_identity
from dealforge.providers.cvs.account_sync import _time_limit
from dealforge.providers.cvs.offer_assessment import assess_offers
from dealforge.providers.cvs.optimizer import optimize_purchase
from dealforge.providers.cvs.pricing import identifier, resolve_prices


def validate_request(items, zip_code, store_id, channel, detail_limit, timeout_seconds):
    if (not isinstance(items, list) or not 1 <= len(items) <= 10
            or not isinstance(zip_code, str) or not re.fullmatch(r"\d{5}", zip_code)
            or store_id is not None and (not isinstance(store_id, str) or not re.fullmatch(r"\d{1,8}", store_id))
            or channel not in {"in_store", "pickup", "online"}
            or type(detail_limit) is not int or not 0 <= detail_limit <= 25
            or type(timeout_seconds) is not int or not 30 <= timeout_seconds <= 180):
        raise ValueError("Use 1-10 shopping items, a ZIP and bounded collection options")
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Shopping items must be objects")
        key = identifier(item.get("line_id"))
        if key in seen or type(item.get("quantity")) is not int or not 1 <= item["quantity"] <= 100:
            raise ValueError("Use unique line IDs and integer quantities 1-100")
        seen.add(key)
        for field in ("query", "brand", "size", "offer_query"):
            text = item.get(field, "")
            if not isinstance(text, str) or len(text) > 100 or any(ord(c) < 32 for c in text):
                raise ValueError("Use short printable product queries")
        if not item.get("query", "").strip() and not item.get("product_url"):
            raise ValueError("Each item needs a query or an exact CVS URL")
        if item.get("product_url") and not product_identity(item["product_url"]):
            raise ValueError("Use CVS product URLs")
        evidence = item.get("price_evidence", [])
        if not isinstance(evidence, list) or len(evidence) > 10 or any(not isinstance(e, dict) for e in evidence):
            raise ValueError("Use up to ten price evidence objects per item")


async def plan_trip(service, *, items, account_ref="cvs-personal", zip_code=None,
                    store_id=None, channel="in_store", confirmations=None, reward_offers=None,
                    profile=None, extra_care_ready=False, detail_limit=10,
                    store_timezone=None, max_candidates=1000,
                    timeout_seconds=180):
    """Refresh, collect choices, assess terms and optimize the complete exact list.

    Query searches produce choices; a caller-selected exact SKU URL is required
    to build a basket. No substitution, partial-basket recommendation, automatic
    clipping, credentials, cart or checkout. One total browser collection budget.
    """
    zip_code, store_id = shopping_zip("cvs", zip_code), shopping_store("cvs", store_id)
    store_timezone = local_timezone(store_timezone)
    validate_request(items, zip_code, store_id, channel, detail_limit, timeout_seconds)
    identifier(account_ref)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", account_ref) or re.search(r"\d{10,}", account_ref):
        raise ValueError("Use an opaque account label")
    ZoneInfo(store_timezone)
    if type(extra_care_ready) is not bool or type(max_candidates) is not int or not 4 <= max_candidates <= 10000:
        raise ValueError("Use explicit card readiness and a bounded optimizer search")
    result = {"schema_version": 1, "status": "needs_input", "account_ref": account_ref,
        "zip_code": zip_code, "store_id": store_id, "channel": channel,
        "items": [], "lines": [], "account": None, "eligibility": None,
        "price_resolution": None, "optimization": None, "recommended": None,
        "next_actions": [], "execution_ready": False, "checkout_verified": False,
        "review": ["exact_product_choices_required", "unknown_shelf_prices_remain_unknown",
                   "no_partial_basket_recommendations", "conditional_one_checkout_before_tax"]}
    deadline = asyncio.get_running_loop().time() + timeout_seconds

    def remaining():
        left = int(deadline - asyncio.get_running_loop().time())
        if left < 10:
            raise asyncio.TimeoutError
        return min(180, left)

    def account_view(sync):
        snapshot = sync.get("snapshot") or {}
        return {"status": sync.get("status"), "reason": sync.get("reason"),
            "observed_at": snapshot.get("observed_at"), "browser_backend": sync.get("browser_backend"),
            "binding": sync.get("account_binding"), "collection": sync.get("collection"),
            "definition_coverage": sync.get("definition_coverage"),
            "rewards": snapshot.get("rewards", []), "review": sync.get("review", [])}

    try:
        async with _time_limit(timeout_seconds):
            sync = await service.sync(account_ref=account_ref, timeout_seconds=remaining(), include_offer_rules=True)
            result["account"] = account_view(sync)
            if not sync.get("snapshot") or sync.get("status") not in {"partial", "ready"}:
                result["reason"] = sync.get("reason", "refresh_signed_in_account")
                result["next_actions"].append({"kind": "account", "reason": result["reason"], "next_action": sync.get("next_action")})
                return result
            if sync.get("account_identity_verified") is not True or (sync.get("account_binding") or {}).get("status") != "matched":
                result["reason"] = "account_continuity_not_matched"
                result["next_actions"].append({"kind": "account", "reason": result["reason"]})
                return result
            stores = await service.find_stores(zip_code=zip_code, timeout_seconds=remaining())
            selected = [s for s in stores.get("stores", []) if s.get("selected") and s.get("store_id")
                        and s.get("zip_code") == zip_code and (store_id is None or s["store_id"] == store_id)]
            if len(selected) != 1:
                result["stores"] = stores.get("stores", [])
                result["next_actions"].append({"kind": "store", "reason": stores.get("reason") or "requested_store_not_selected"})
                return result
            result["store_id"] = store_id = selected[0]["store_id"]
            for item in items:
                options = dict(zip_code=zip_code, store_id=store_id, timeout_seconds=remaining())
                if item.get("product_url"):
                    observed = await service.get_product(product_url=item["product_url"], **options)
                else:
                    observed = await service.search_products(query=item["query"], brand=item.get("brand", ""),
                        size=item.get("size", ""), limit=5, **options)
                products = observed.get("products", [])
                chosen = products[0] if item.get("product_url") and len(products) == 1 else None
                expected = product_identity(item.get("product_url", ""))
                exact = bool(chosen and expected and expected["sku_id"] and chosen.get("selected_sku_verified") is True
                    and all(chosen.get(k) == expected[k] for k in ("product_id", "sku_id"))
                    and (observed.get("store") or {}).get("store_id") == store_id
                    and "selected_sku_mismatch" not in (chosen.get("match") or {}).get("constraint_gaps", []))
                result["items"].append({"line_id": item["line_id"], "quantity": item["quantity"],
                    "status": "selected" if exact else "needs_selection", "candidates": products,
                    "reason": observed.get("reason")})
                if exact:
                    result["lines"].append({"line_id": item["line_id"], "quantity": item["quantity"],
                        "product": chosen, "price_evidence": item.get("price_evidence", [])})
                else:
                    result["next_actions"].append({"kind": "product", "line_id": item["line_id"],
                        "reason": observed.get("reason") or "choose_exact_sku_url"})
            queries = list(dict.fromkeys((i.get("offer_query") or i.get("brand") or i.get("query") or "").strip()
                                        for i in items))
            queries = [q for q in queries if q]
            detail_options = {"detail_limit": detail_limit, "detail_queries": queries} if detail_limit and queries else {}
            sync = await service.sync(account_ref=account_ref, timeout_seconds=remaining(),
                include_offer_rules=True, **detail_options)
            result["account"] = account_view(sync)
            if not sync.get("snapshot") or sync.get("status") not in {"partial", "ready"}:
                result["next_actions"].append({"kind": "account", "reason": sync.get("reason") or "refresh_signed_in_account"})
                return result
            result["detail_collection"] = {"queries": queries, "visit_limit": detail_limit,
                "review": "query_filters_can_miss_broad_basket_offers"}
            if not result["lines"]:
                return result
            now = datetime.now().astimezone().isoformat()
            result["price_resolution"] = resolve_prices(result["lines"], channel=channel, store_id=store_id, as_of=now)
            if len(result["lines"]) != len(items):
                return result
            assessment = assess_offers(result["lines"], account_sync=sync, account_ref=account_ref,
                confirmations=confirmations, channel=channel, store_id=store_id,
                store_timezone=store_timezone, as_of=now)
            result["eligibility"] = {k: v for k, v in assessment.items() if k != "confirmation_requests"}
            for line in result["price_resolution"]["lines"]:
                if line["unresolved"]:
                    result["next_actions"].append({"kind": "price", "line_id": line["line_id"], "checks": line["unresolved"]})
            prepared = assessment["preparation"]
            if prepared.get("reason"):
                result["next_actions"].append({"kind": "account", "reason": prepared["reason"]})
            result["next_actions"].extend({"kind": "offer_terms", **p} for p in prepared["pending"])
            optimized = optimize_purchase(result["lines"], account_sync=sync, account_ref=account_ref,
                confirmations=assessment["confirmation_requests"], reward_offers=reward_offers, channel=channel,
                store_id=store_id, store_timezone=store_timezone, profile=profile,
                max_candidates=max_candidates, extra_care_ready=extra_care_ready, as_of=now)
            result["optimization"] = optimized
            result["recommended"] = optimized["recommended"]
            result["status"] = optimized["status"]
            if not assessment["coverage"]["selection_complete"]:
                result["next_actions"].append({"kind": "offer_scope", "reason": "relevant_coupon_selection_truncated"})
                if result["status"] == "conditional_estimate":
                    result["status"] = "partial"
            if extra_care_ready is not True:
                result["next_actions"].append({"kind": "card", "reason": "confirm_qualifying_card_readiness"})
            return result
    except asyncio.TimeoutError:
        result["reason"] = "planning_time_budget_reached"
        result["next_actions"].append({"kind": "retry", "reason": result["reason"]})
        return result
