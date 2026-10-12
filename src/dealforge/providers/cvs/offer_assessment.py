"""Explain evidenced offer eligibility and produce bounded confirmation tasks."""

import re

from dealforge.providers.cvs.matching import match_products_to_offers
from dealforge.providers.cvs.pricing import comparison_time, resolve_prices, valid_window, money
from dealforge.providers.cvs.purchase_rules import prepare_purchase
from dealforge.providers.cvs.eligibility import excluded_units


def assess_offers(lines, *, account_sync, store_id, account_ref="cvs-personal",
                  confirmations=None, channel="in_store", store_timezone=None,
                  as_of=None, allow_fixture=False):
    """Select relevant coupons without asserting that their terms are complete.

    All observed candidates are compared in batches. At most 25 relevant coupons
    enter preparation; selected confirmations take priority over text candidates.
    Membership, activation, price thresholds and explicit conflicts stay separate.
    """
    now = comparison_time(as_of)
    prices = resolve_prices(lines, channel=channel, store_id=store_id, as_of=now.isoformat())
    rules = account_sync.get("offer_rules", [])
    if not isinstance(rules, list) or len(rules) > 1000 or any(not isinstance(r, dict) for r in rules):
        raise ValueError("Use bounded normalized account offer rules")
    supplied = confirmations or []
    if not isinstance(supplied, list) or len(supplied) > 50 or any(not isinstance(c, dict) for c in supplied):
        raise ValueError("Use up to 50 account-bound confirmations")
    explicit = [c.get("id") for c in supplied]
    if len(explicit) != len(set(explicit)):
        raise ValueError("Duplicate confirmation identities")
    matches = []
    for start in range(0, len(rules), 25):
        matches.extend(match_products_to_offers([l["product"] for l in lines], rules[start:start + 25], as_of=now)["matches"])
    rank = {"observed_sku_membership": 0, "sku_membership_needs_recheck": 1,
            "product_family_candidate": 2, "text_candidate": 3}
    relevant = list(dict.fromkeys(m["offer_id"] for m in sorted(matches, key=lambda m: (rank[m["kind"]], m["offer_id"]))))
    # Explicit whole-purchase offers are review candidates for every basket;
    # category offers are never broadened merely because they have a threshold.
    basket_offers = [r["offer_id"] for r in rules if re.search(
        r"\b(?:your (?:next )?purchase|entire purchase)\b", r.get("description_text", ""), re.I)]
    relevant = list(dict.fromkeys([*relevant, *basket_offers]))
    coupon_rows = {c["coupon_id"]: c for c in (account_sync.get("snapshot") or {}).get("coupons", [])}
    explicit_coupons = [k for k in explicit if k in coupon_rows]
    if len(explicit_coupons) > 25:
        raise ValueError("Select up to 25 coupons")
    selected = list(dict.fromkeys([*explicit_coupons, *relevant]))[:25]
    requests = [*supplied, *({"id": key} for key in selected if key not in explicit)]
    if len(requests) > 50:
        requests = [*supplied, *({"id": key} for key in selected if key not in explicit)][:50]
    prepared = prepare_purchase(lines, account_sync=account_sync, account_ref=account_ref,
        confirmations=requests, channel=channel, store_id=store_id, store_timezone=store_timezone,
        as_of=now.isoformat(), allow_fixture=allow_fixture)
    drafts = {r["id"]: r for r in prepared["coupons"]}
    assessments = []
    for rule in rules:
        key = rule.get("offer_id")
        if key not in selected:
            continue
        row = coupon_rows.get(key, {})
        related = [m for m in matches if m["offer_id"] == key]
        unit_pool = []
        blocked = excluded_units(lines, rule)
        for line, resolved in zip(lines, prices["lines"]):
            product = line["product"]
            if product.get("selected_sku_verified") is True and any(
                    m["product_id"] == product["product_id"] and m["sku_id"] == product["sku_id"]
                    and m["sku_membership_observed"] for m in related):
                unit_pool.extend(u for u in resolved["unit_ids"] if u not in blocked)
        gaps = []
        if not unit_pool:
            gaps.append("exact_eligible_units_need_confirmation")
        if row.get("state") != "on_card":
            gaps.append("activation_or_presentation_required")
        if blocked:
            gaps.append("observed_product_exclusion_conflict")
        window = valid_window(drafts.get(key, {}), now)
        if window:
            gaps.append(window)
        quantity = rule.get("minimum_quantity")
        if type(quantity) is int and len(unit_pool) < quantity:
            gaps.append("observed_quantity_below_threshold")
        # Calculate only a PRE-COUPON comparison, not a qualification assertion.
        qualifying_lines = [l for l in prices["lines"] if any(u in unit_pool for u in l["unit_ids"])]
        spend = None
        if qualifying_lines and all(l["price"] for l in qualifying_lines):
            spend = f'{sum(money(l["price"]["amount"]) * len(set(l["unit_ids"]) & set(unit_pool)) for l in qualifying_lines):.2f}'
        if spend is not None and rule.get("minimum_spend") is not None and money(spend) < money(rule["minimum_spend"]):
            gaps.append("observed_pre_coupon_spend_below_threshold")
        checks = next((p["checks"] for p in prepared["pending"] if p["id"] == key), [])
        assessments.append({"id": key, "headline": row.get("headline"),
            "source_text": row.get("raw_terms"),
            "matches": related, "suggested_unit_ids": unit_pool, "excluded_unit_ids": blocked,
            "requirements": {k: rule.get(k) for k in ("discount", "minimum_quantity", "minimum_spend", "conflicts")},
            "term_candidates": rule.get("term_candidates", {}),
            "exclusions_text": rule.get("exclusions_text", []),
            "definition_observed": rule.get("definition_observed") is True,
            "eligible_quantity_observed": len(unit_pool), "eligible_spend_before_coupons": spend,
            "source_fingerprint": (drafts.get(key) or {}).get("source_fingerprint"),
            "remaining_checks": sorted(set([*gaps, *checks])),
            "checkout_eligibility_verified": False})
    return {"status": prepared["status"], "as_of": now.isoformat(), "offers": assessments,
        "preparation": prepared, "selected_coupon_ids": selected,
        "confirmation_requests": requests,
        "coverage": {"observed_rule_count": len(rules), "relevant_rule_count": len(relevant),
                     "selected_coupon_count": len(selected),
                     "selection_complete": set(relevant).issubset(selected)},
        "review": ["literal_terms_are_candidates_until_confirmed", "partial_membership_absence_is_unknown",
                   "per_purchase_limit_is_not_remaining_uses", "no_stacking_or_eligibility_defaults"]}
