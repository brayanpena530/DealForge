"""Bind selected checkout attestations to fresh account records and exact units."""

from datetime import datetime, time
import hashlib
import json
import re
from itertools import combinations, islice
from math import comb
from zoneinfo import ZoneInfo

from dealforge.providers.cvs.pricing import comparison_time, fresh, identifier, money, resolve_prices
from dealforge.providers.cvs.wallet import AccountSnapshot, parse_timestamp
from dealforge.providers.cvs.eligibility import excluded_units, extract_term_candidates
from dealforge.shopper_settings import store_timezone as local_timezone

CHECKS = ("terms_verified", "product_eligibility_verified", "stacking_verified", "limits_verified")


def expiration(expires_on, channel, store_timezone):
    if expires_on is None:
        return None
    zone = ZoneInfo(store_timezone if channel == "in_store" else "America/New_York")
    return datetime.combine(datetime.fromisoformat(expires_on).date(), time.max, zone).isoformat()


def _fingerprint(account_ref, row, candidate, basket_identities):
    facts = {k: candidate.get(k) for k in ("discount", "minimum_spend", "minimum_quantity", "conflicts",
        "description_text", "exclusions_text", "term_candidates", "eligible_products_observed")}
    raw = json.dumps([account_ref, row, facts, basket_identities], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def prepare_purchase(lines, *, account_sync, account_ref="cvs-personal", confirmations=None,
                     channel="in_store", store_id, store_timezone=None,
                     as_of=None, allow_fixture=False):
    """Return account-derived drafts and gaps; never mutate activation or face value.

    Missing facts require fresh per-record attestations keyed to the returned
    source_fingerprint. Inventory state, amount and expiration cannot be changed
    by an attestation. Coupon amount/threshold conflicts remain unresolved.
    """
    now = comparison_time(as_of)
    store_timezone = local_timezone(store_timezone)
    ZoneInfo(store_timezone)
    if not isinstance(account_sync, dict) or not isinstance(account_ref, str) or not account_ref.strip():
        raise ValueError("Use a sync result and an opaque expected account reference")
    result = {"status": "needs_input", "account_ref": account_ref, "as_of": now.isoformat(),
        "coupons": [], "redemptions": [], "pending": [], "wallet": [], "review": [],
        "checkout_verified": False}
    if account_sync.get("status") not in {"partial", "ready"} or account_sync.get("snapshot") is None:
        result["reason"] = "refresh_signed_in_account"
        return result
    snapshot = AccountSnapshot.from_dict(account_sync["snapshot"])
    data = snapshot.to_dict()
    synthetic = snapshot.source == "fixture"
    result["synthetic_data"] = synthetic
    if type(allow_fixture) is not bool or synthetic and not allow_fixture:
        result["reason"] = "fixture_requires_explicit_simulation"
        return result
    if snapshot.status.value not in {"partial", "ready"} or not fresh(data["observed_at"], now):
        result["reason"] = "account_stale_or_access_unresolved"
        return result
    if snapshot.account_ref != account_ref:
        result["reason"] = "account_reference_mismatch"
        return result
    if not synthetic and (snapshot.source != "browser" or account_sync.get("account_identity_verified") is not True
            or (account_sync.get("account_binding") or {}).get("status") != "matched"):
        result["reason"] = "account_continuity_not_matched"
        return result
    prices = resolve_prices(lines, channel=channel, store_id=store_id, as_of=now.isoformat())
    result["price_resolution"] = prices
    result["source_snapshot_observed_at"] = data["observed_at"]
    result["inventory_complete"] = data["rewards_complete"] and data["coupons_complete"]
    result["review"] = ["only_selected_coupon_confirmations_considered", "confirmations_are_caller_attestations",
        "account_match_means_device_continuity_not_ownership"]
    if not result["inventory_complete"]:
        result["review"].append("inventory_incomplete_best_known_scope_only")
    records = {r["reward_id"]: (r, True) for r in data["rewards"]}
    for row in data["coupons"]:
        if row["coupon_id"] in records:
            raise ValueError("Coupon/reward source IDs must not overlap")
        records[row["coupon_id"]] = (row, False)
    supplied = confirmations if confirmations is not None else []
    if not isinstance(supplied, list) or len(supplied) > 50:
        raise ValueError("Use up to 50 selected confirmation objects")
    confirmed = {}
    for c in supplied:
        if not isinstance(c, dict):
            raise ValueError("Confirmations must be objects")
        key = identifier(c.get("id"))
        if key in confirmed or key not in records:
            raise ValueError("Confirmation IDs must uniquely reference the observed account")
        confirmed[key] = c
    candidates = account_sync.get("offer_rules", [])
    if not isinstance(candidates, list) or any(not isinstance(c, dict) for c in candidates):
        raise ValueError("Account offer rules must be normalized objects")
    candidate_map = {}
    for c in candidates:
        key = c.get("offer_id")
        if key in candidate_map:
            raise ValueError("Duplicate account rule candidates")
        candidate_map[key] = c
    unit_set = {k for l in prices["lines"] for k in l["unit_ids"]}
    basket_identities = [(l["line_id"],l["product_id"],l["sku_id"],l["quantity"]) for l in prices["lines"]]
    # Every current reward is visible, even without terms. Unselected coupons
    # can number in the hundreds; only requested coupon confirmations form rules.
    keys = list(dict.fromkeys([*confirmed, *(r["reward_id"] for r in data["rewards"])]))
    for key in keys:
        row, reward = records[key]
        candidate = candidate_map.get(key, {})
        c = confirmed.get(key, {})
        fingerprint = _fingerprint(account_ref, row, candidate, basket_identities)
        gaps = []
        trusted = (c.get("source_fingerprint") == fingerprint and fresh(c.get("observed_at"), now)
                   and isinstance(c.get("source_ref"), str) and bool(c["source_ref"].strip())
                   and len(c["source_ref"]) <= 500)
        if not trusted:
            gaps.append("fresh_record_bound_confirmation_required")
        # A stale/changed confirmation cannot supply any new fact.
        facts = c if trusted else {}
        term_candidates = candidate.get("term_candidates") or extract_term_candidates(row.get("raw_terms") or "")
        term_fields = term_candidates.get("fields") or {}
        if term_candidates.get("conflicts"):
            gaps.append("conflicting_observed_offer_terms")
        if channel in term_fields.get("excluded_channels", []):
            gaps.append("observed_channel_excluded")
        # Literal source evidence constrains attestations; it never fills the
        # remaining-uses count or marks terms/stacking verified automatically.
        for field in ("channels", "issuer", "qualification_basis"):
            if field in facts and field in term_fields and facts[field] != term_fields[field]:
                gaps.append("confirmation_observed_" + field + "_conflict")
        for field in ("state", "amount", "expires_on"):
            if field in c and c[field] != row.get(field):
                gaps.append("confirmation_cannot_override_" + field)
        targets = facts.get("unit_ids", [])
        if (not isinstance(targets, list) or any(not isinstance(t, str) for t in targets)
                or len(targets) != len(set(targets)) or any(t not in unit_set for t in targets)):
            raise ValueError("Confirmation unit_ids must name unique observed physical units")
        if not targets:
            gaps.append("eligible_units_not_confirmed")
        blocked_units = set(excluded_units(lines, candidate))
        if blocked_units.intersection(targets):
            gaps.append("observed_product_exclusion_conflict")
        if any(re.search(r"\b(?:cannot|may not) be combined\b", text, re.I)
               for text in term_candidates.get("restrictions_text", [])):
            gaps.append("observed_stacking_restriction_not_modeled")
        if any(re.search(r"\bup to\s*\$", text, re.I) for text in term_candidates.get("restrictions_text", [])):
            gaps.append("observed_discount_cap_not_modeled")
        known_channels = {"in_store": ["in_store"], "online": ["online", "pickup"],
                          "both": ["in_store", "online", "pickup"]}.get(row["channel"])
        channels = known_channels or facts.get("channels")
        if known_channels and "channels" in c and c["channels"] != known_channels:
            gaps.append("confirmation_channel_conflict")
        checks = {flag: facts.get(flag) is True for flag in CHECKS}
        gaps.extend(flag + "_required" for flag, yes in checks.items() if not yes)
        rule = {"id": key, "unit_ids": targets, "state": row["state"], "channels": channels,
            "observed_at": data["observed_at"], "valid_from": data["observed_at"],
            "valid_to": expiration(row["expires_on"], channel, store_timezone),
            "minimum_quantity": facts.get("minimum_quantity"), "minimum_spend": facts.get("minimum_spend"),
            "qualification_basis": facts.get("qualification_basis"), "remaining_uses": facts.get("remaining_uses"),
            "source_ref": facts.get("source_ref"), **checks,
            "account_ref": account_ref, "source_fingerprint": fingerprint,
            "source_snapshot_observed_at": data["observed_at"]}
        for field in ("channels", "valid_to", "remaining_uses"):
            if rule[field] is None:
                gaps.append(field + "_unknown")
        if reward:
            kind = row["kind"]
            if kind == "unknown" and facts.get("reward_kind_verified") is True:
                kind = facts.get("kind", "unknown")
            elif "kind" in c and c["kind"] != kind:
                gaps.append("confirmation_reward_kind_conflict")
            partial = row["partial_redemption"]
            if partial is None and facts.get("partial_redemption_verified") is True:
                partial = facts.get("partial_redemption")
            elif partial is not None and "partial_redemption" in c and c["partial_redemption"] != partial:
                gaps.append("confirmation_partial_redemption_conflict")
            rule.update(kind=kind, amount=row["amount"], partial_redemption=partial)
            if kind not in {"extrabucks", "membership_bonus"}:
                gaps.append("unsupported_or_unknown_certificate_kind")
            if row["amount"] is None or type(partial) is not bool:
                gaps.append("certificate_value_or_partial_redemption_unknown")
            result["wallet"].append({"id": key, "kind": kind, "amount": row["amount"],
                "state": row["state"], "valid_to": rule["valid_to"], "channels": channels,
                "source_fingerprint": fingerprint, "preservation_terms_confirmed": not gaps})
            result["redemptions"].append(rule)
        else:
            discount = candidate.get("discount") or {}
            kind, value = discount.get("kind"), discount.get("value")
            if candidate.get("conflicts"):
                gaps.append("conflicting_account_purchase_requirements")
            if kind in {"amount_off", "percent_off"}:
                if any(k in c and c[k] != v for k, v in (("kind", kind), ("value", value))):
                    gaps.append("confirmation_discount_conflict")
            else:
                kind, value = facts.get("kind"), facts.get("value")
            issuer = row["issuer"] or facts.get("issuer")
            if row["issuer"] and "issuer" in c and c["issuer"] != row["issuer"]:
                gaps.append("confirmation_issuer_conflict")
            rule.update(kind=kind, value=value, issuer=issuer)
            if kind not in {"amount_off", "percent_off"} or value is None or issuer not in {"mfr_coupon", "store_coupon"}:
                gaps.append("discount_or_issuer_unknown")
            for field in ("minimum_quantity", "minimum_spend"):
                known = candidate.get(field)
                if known is not None:
                    if field in c and c[field] != known:
                        gaps.append("confirmation_" + field + "_conflict")
                    rule[field] = known
            # Confirmation unit_ids are an eligible pool. A quantity-based
            # manufacturer coupon consumes only its required physical units.
            # Item-scoped store coupons can opt into the same assignment search.
            qty = rule["minimum_quantity"]
            item_scope = issuer == "mfr_coupon" or facts.get("scope") == "item"
            if (item_scope and rule["kind"] in {"amount_off", "percent_off"}
                    and rule["minimum_spend"] is not None and money(rule["minimum_spend"]) == 0
                    and type(qty) is int and 1 <= qty <= len(targets)):
                rule["unit_options"] = [list(choice) for choice in islice(combinations(targets, qty), 25)]
                rule["unit_options_complete"] = comb(len(targets), qty) <= 25
                rule["unit_ids"] = rule["unit_options"][0]
            result["coupons"].append(rule)
        for field in ("minimum_quantity", "minimum_spend"):
            if rule[field] is None:
                gaps.append(field + "_unknown")
        if rule["minimum_spend"] is not None:
            money(rule["minimum_spend"])
        if rule["minimum_quantity"] is not None and (type(rule["minimum_quantity"]) is not int or not 1 <= rule["minimum_quantity"] <= 2500):
            raise ValueError("minimum_quantity must be an integer 1â€“2500")
        if rule["remaining_uses"] is not None and (type(rule["remaining_uses"]) is not int or rule["remaining_uses"] < 0):
            raise ValueError("remaining_uses must be a nonnegative integer")
        if channels is not None and (not isinstance(channels,list) or not channels or any(ch not in {"in_store","pickup","online"} for ch in channels)):
            raise ValueError("Confirm explicit supported channels")
        if reward:
            result["wallet"][-1]["preservation_terms_confirmed"] = not gaps
        if gaps:
            rule["terms_verified"] = False
        result["pending"].append({"id": key, "source_fingerprint": fingerprint,
                                  "checks": sorted(set(gaps))})
    result["pending"] = [p for p in result["pending"] if p["checks"]]
    result["status"] = "partial" if result["pending"] or not result["inventory_complete"] else "prepared"
    return result
