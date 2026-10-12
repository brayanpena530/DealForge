"""Explain coupon/product relationships without computing unverified savings."""

from datetime import datetime, timedelta
import re

from dealforge.providers.cvs.catalog import clean_eligible_products, normalized, product_identity
from dealforge.providers.cvs.wallet import parse_timestamp


def match_products_to_offers(products, offer_rules, *, as_of=None):
    if (not isinstance(products, list) or not isinstance(offer_rules, list)
            or len(products) > 25 or len(offer_rules) > 25):
        raise ValueError("Use at most 25 product observations and 25 selected offer rules")
    now = as_of or datetime.now().astimezone()
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("An aware comparison timestamp is required")
    relationships = []
    for product in products:
        if not isinstance(product, dict):
            raise ValueError("Product observations must be objects")
        identity = product_identity(product.get("url", ""))
        if not identity or identity["product_id"] != product.get("product_id") or identity["sku_id"] != product.get("sku_id"):
            raise ValueError("Product URL and observed product/SKU identity must agree")
        for rule in offer_rules:
            if not isinstance(rule, dict) or not re.fullmatch(r'cvs-[a-f0-9]{24}', str(rule.get("offer_id", ""))):
                raise ValueError("Offer rules need opaque CVS offer identities")
            observed = clean_eligible_products(rule.get("eligible_products_observed", []))
            same_family = [p for p in observed if p["product_id"] == identity["product_id"]]
            same_sku = [p for p in same_family if identity["sku_id"] and p["sku_id"] == identity["sku_id"]]
            fresh = False
            try:
                age = now - parse_timestamp(rule.get("eligibility_observed_at"))
                fresh = timedelta(0) <= age <= timedelta(hours=24)
            except ValueError:
                pass
            product_gaps = (product.get("match") or {}).get("constraint_gaps", [])
            mismatch = "selected_sku_mismatch" in product_gaps
            tokens = set(normalized(str(product.get("name", ""))).split())
            description = set(normalized(str(rule.get("description_text", ""))).split())
            common = sorted(t for t in tokens & description if len(t) > 2 and t not in {
                "any", "one", "two", "off", "buy", "product", "products", "save", "with", "the", "and"})
            if same_sku:
                kind = "observed_sku_membership" if fresh and not mismatch else "sku_membership_needs_recheck"
            elif same_family:
                kind = "product_family_candidate"
            elif common:
                kind = "text_candidate"
            else:
                continue
            relationships.append({"product_id": identity["product_id"], "sku_id": identity["sku_id"],
                "offer_id": rule["offer_id"], "kind": kind,
                "sku_membership_observed": bool(same_sku and fresh and not mismatch),
                "evidence_fresh": fresh, "text_overlap": common,
                "checkout_eligibility_verified": False,
                "remaining_checks": ["coupon_activation", "quantity_and_spend", "issuer_and_channel",
                                     "exclusions", "redemption_limits", "stacking"],
                "eligible_products_complete": False})
    return {"status": "partial", "observed_at": now.isoformat(), "matches": relationships,
            "review": ["missing_from_partial_carousel_does_not_prove_ineligibility",
                       "matching_does_not_compute_checkout_savings"]}
