"""Resolve dated, exact-SKU evidence without relabeling online prices as shelf prices."""

from datetime import datetime, timedelta
from decimal import Decimal
import re

from dealforge.providers.cvs.catalog import product_identity
from dealforge.providers.cvs.wallet import parse_timestamp


def money(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,8}(?:\.\d{1,2})?", value):
        raise ValueError("Money must be a nonnegative decimal string (up to eight integer digits)")
    return Decimal(value)


def comparison_time(as_of=None):
    return parse_timestamp(as_of) if as_of is not None else datetime.now().astimezone()


def fresh(value, now):
    try:
        return timedelta(0) <= now - parse_timestamp(value) <= timedelta(hours=24)
    except ValueError:
        return False


def valid_window(row, now):
    """Missing dates are unresolved; a known expired/future window is inapplicable."""
    try:
        start, end = parse_timestamp(row.get("valid_from")), parse_timestamp(row.get("valid_to"))
    except ValueError:
        return "validity_window_unknown"
    if end < start:
        return "invalid_validity_window"
    return None if start <= now <= end else "outside_validity_window"


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("Use short opaque identifiers containing letters, numbers, underscores or hyphens")
    return value


def resolve_prices(lines, *, channel, store_id, as_of=None):
    """Each line contains line_id, quantity, product and optional price_evidence.

    Manual/ad evidence is a caller attestation, never collector verification.
    Ad evidence additionally needs SKU membership, store applicability and card
    conditions confirmed; only explicit unit prices are supported here.
    """
    if channel not in {"in_store", "pickup", "online"} or not re.fullmatch(r"\d{1,12}", str(store_id)):
        raise ValueError("Choose in_store, pickup or online and an explicit store ID")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 25:
        raise ValueError("Use 1–25 basket lines")
    now, result, seen = comparison_time(as_of), [], set()
    for line in lines:
        if not isinstance(line, dict):
            raise ValueError("Basket lines must be objects")
        key = identifier(line.get("line_id"))
        qty = line.get("quantity")
        if key in seen or type(qty) is not int or not 1 <= qty <= 100:
            raise ValueError("Line IDs must be unique; quantities must be integers 1–100")
        seen.add(key)
        product = line.get("product")
        if not isinstance(product, dict):
            raise ValueError("Each line needs a normalized product observation")
        identity = product_identity(product.get("url", ""))
        if not identity or any(identity[k] != product.get(k) for k in ("product_id", "sku_id")):
            raise ValueError("Product URL, product ID and SKU must agree")
        gaps, candidates, rejected = [], [], []
        if not identity["sku_id"] or product.get("selected_sku_verified") is not True:
            gaps.append("exact_sku_not_verified")
        if "selected_sku_mismatch" in (product.get("match") or {}).get("constraint_gaps", []):
            gaps.append("selected_sku_mismatch")
        evidence = line.get("price_evidence", [])
        if not isinstance(evidence, list) or len(evidence) > 10 or any(not isinstance(e, dict) for e in evidence):
            raise ValueError("Use up to ten price evidence objects per line")
        if channel in {"online", "pickup"}:
            observed = product.get("price") or {}
            if not isinstance(observed, dict):
                raise ValueError("Invalid product price observation")
            evidence = evidence + [{**observed, **identity, "source": observed.get("source"),
                "store_id": (product.get("store_context") or {}).get("store_id"),
                "source_ref": observed.get("source_url"), "sale_status": "unknown"}]
        for e in evidence:
            reason = None
            source = e.get("source")
            online = source == "cvs_rendered_page" and e.get("channel") == "online"
            if e.get("product_id") != identity["product_id"] or e.get("sku_id") != identity["sku_id"]:
                reason = "price_sku_mismatch"
            elif str(e.get("store_id")) != str(store_id):
                reason = "price_store_mismatch"
            elif e.get("channel") != channel and not (online and channel == "pickup"):
                reason = "price_channel_mismatch"
            elif e.get("currency") != "USD" or e.get("amount") is None:
                reason = "unit_price_unknown"
            elif not fresh(e.get("observed_at"), now):
                reason = "price_stale_or_future"
            elif not isinstance(e.get("source_ref"), str) or not e["source_ref"].strip() or len(e["source_ref"]) > 500:
                reason = "price_source_reference_missing"
            elif online:
                source_identity = product_identity(e["source_ref"])
                if not source_identity or any(source_identity[k] != identity[k] for k in ("product_id", "sku_id")):
                    reason = "price_source_identity_mismatch"
            elif source not in {"shelf_label", "receipt", "store_quote", "weekly_ad"}:
                reason = "unsupported_price_source"
            elif e.get("verified_by_caller") is not True:
                reason = "price_evidence_needs_confirmation"
            elif source == "weekly_ad":
                reason = valid_window(e, now)
                if reason is None and any(e.get(k) is not True for k in (
                        "sku_membership_verified", "store_applicability_verified", "card_conditions_met")):
                    reason = "ad_applicability_not_verified"
                if reason is None and e.get("price_kind") != "unit":
                    reason = "ad_multibuy_or_bogo_needs_explicit_price"
            if reason:
                rejected.append(reason)
                continue
            value = money(e["amount"])
            sale_status = e.get("sale_status", "unknown")
            if sale_status not in {"sale", "regular", "unknown"}:
                raise ValueError("sale_status must be sale, regular or unknown")
            candidates.append({"amount": f"{value:.2f}", "currency": "USD", "channel": channel,
                "source": source, "source_ref": e["source_ref"], "observed_at": e["observed_at"],
                "store_id": str(store_id), "sale_status": sale_status,
                "caller_attested": not online})
        values = {c["amount"] for c in candidates}
        if not candidates:
            gaps.append("current_price_not_resolved")
        elif len(values) > 1:
            gaps.append("conflicting_current_prices")
        statuses = {c["sale_status"] for c in candidates if c["sale_status"] != "unknown"}
        sale_status = next(iter(statuses)) if len(statuses) == 1 else "unknown"
        resolved = candidates[0] if candidates and not gaps else None
        if resolved:
            resolved = {**resolved, "sale_status": sale_status}
        result.append({"line_id": key, "quantity": qty, **identity,
            "unit_ids": [f"{key}:{i}" for i in range(1, qty + 1)],
            "price": resolved, "price_evidence": candidates, "unresolved": gaps,
            "rejected_evidence": sorted(set(rejected)),
            "availability": product.get("availability", "unknown"),
            "availability_scope": product.get("availability_scope", "unknown")})
    return {"schema_version": 1, "status": "needs_input" if any(l["unresolved"] for l in result) else "resolved",
        "as_of": now.isoformat(), "channel": channel, "store_id": str(store_id), "lines": result,
        "checkout_verified": False, "price_freshness_hours": 24,
        "review": ["caller_attestations_are_not_live_verification", "availability_requires_separate_check"]}
