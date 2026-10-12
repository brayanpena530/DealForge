"""Extract explicit coupon requirements without claiming checkout eligibility.

Only literal money/quantity expressions are interpreted. Unknown CVS flag
semantics, product membership, stacking and qualification basis stay unknown.
"""

from __future__ import annotations

from decimal import Decimal
import re
from typing import Any


def _decimal(value: str) -> str | None:
    if not re.fullmatch(r"\d{1,12}(?:\.\d{1,2})?", value):
        return None
    return f"{Decimal(value):.2f}"


def extract_requirements(offer_id: str, text: str) -> dict[str, Any]:
    """Return evidenced candidates; these are never a complete checkout rule set."""
    evidence = []
    amounts = set()
    percentages = set()
    spends = set()
    quantities = set()
    # Preserve source evidence while ignoring repeated copies of the same text.
    for match in re.finditer(r"(?:\$(\d+(?:\.\d+)?)\s*(?:off|on your purchase)\b|\bsave\s*\$(\d+(?:\.\d+)?)(?![\d.,]))", text, re.I):
        if re.search(r"\bup to\s*$", text[max(0, match.start() - 15):match.start()], re.I):
            continue
        value = _decimal(match[1] or match[2])
        if value is not None:
            amounts.add(value)
            evidence.append({"field": "amount_off", "text": match[0]})
    for match in re.finditer(r"(?<![\w.,-])(\d+(?:\.\d+)?)\s*%\s*off\b", text, re.I):
        value = _decimal(match[1])
        if value is not None and 0 < Decimal(value) <= 100:
            percentages.add(value)
            evidence.append({"field": "percent_off", "text": match[0]})
    for match in re.finditer(r"(?:\boff\s*|\bwhen you spend\s*|\bwith (?:a )?purchase of\s*)\$(\d+(?:\.\d+)?)(?![\d.,])\b", text, re.I):
        value = _decimal(match[1])
        if value is not None and Decimal(value) > 0:
            spends.add(value)
            evidence.append({"field": "minimum_spend", "text": match[0]})
    quantity_pattern = (r"(?:^|\n|\b(?:on|any|buy|off)\s+)(?:any\s+)?"
                        r"(?:(one|two|three)\b(?:\s*\(\s*(\d+)\s*\))?|(\d{1,2})\b(?![.,]\d)(?!\s*%))")
    for match in re.finditer(quantity_pattern, text, re.I):
        value = int(match[3]) if match[3] else {"one": 1, "two": 2, "three": 3}[match[1].lower()]
        if match[2] and int(match[2]) != value:
            continue
        if match[3] and not re.search(r'\b(?:on|any|buy|off)\b', match[0], re.I):
            # A bare leading numeral may describe a product size or name.
            continue
        if re.match(r"\s*(?:coupon|offer|reward|oz|ounce|ounces|ml|liters?|grams?|g|mg|ct|count|pack|in)\b", text[match.end():], re.I):
            continue
        if value > 0:
            quantities.add(value)
            evidence.append({"field": "minimum_quantity", "text": match[0]})
    exclusion_matches = re.findall(r"\b(?:excludes?|excluding)\s+[^\n)]+", text, re.I)
    unknowns = ["eligible_product_ids", "issuer", "channel", "stacking",
                "remaining_uses", "spend_qualification_basis"]
    conflicts = []
    for name, values in (("amount_off", amounts), ("percent_off", percentages),
                         ("minimum_spend", spends), ("minimum_quantity", quantities)):
        if len(values) > 1:
            conflicts.append(name)
    if amounts and percentages:
        conflicts.append("discount_kind")
    discount_kind = "unknown"
    discount_value = None
    if len(amounts) == 1 and not percentages:
        discount_kind, discount_value = "amount_off", next(iter(amounts))
    elif len(percentages) == 1 and not amounts:
        discount_kind, discount_value = "percent_off", next(iter(percentages))
    unique_evidence = [dict(items) for items in dict.fromkeys(tuple(row.items()) for row in evidence)]
    return {
        "offer_id": offer_id, "status": "partial", "checkout_eligibility_verified": False,
        "discount": {"kind": discount_kind, "value": discount_value},
        "minimum_spend": next(iter(spends)) if len(spends) == 1 else None,
        "minimum_quantity": next(iter(quantities)) if len(quantities) == 1 else None,
        "exclusions_text": list(dict.fromkeys(exclusion_matches)),
        "evidence": unique_evidence, "unknowns": unknowns, "conflicts": sorted(set(conflicts)),
    }


def extract_term_candidates(text: str) -> dict[str, Any]:
    """Literal term evidence, separate from issuer codes and checkout attestations.

    A per-purchase limit is not an account's remaining redemption count. No
    missing threshold, restriction or stacking rule is defaulted to zero/allowed.
    """
    if not isinstance(text, str) or len(text) > 100_000:
        raise ValueError("Use bounded observed offer text")
    evidence, values = [], {}

    def collect(field, pattern, convert):
        for match in re.finditer(pattern, text, re.I):
            value = convert(match)
            values.setdefault(field, [])
            if value not in values[field]:
                values[field].append(value)
            evidence.append({"field": field, "value": value, "text": match[0]})

    collect("issuer", r"(?:^|[\n.!?]\s*)(?:this is a\s+)?manufacturer['’]?s? coupon\b",
            lambda m: "mfr_coupon")
    collect("issuer", r"(?:^|[\n.!?]\s*)(?:this is a\s+)?CVS (?:store )?coupon\b",
            lambda m: "store_coupon")
    start = r"(?:^|[\n.!?]\s*)(?:(?:this )?(?:offer|coupon) (?:is )?)?valid "
    collect("channels", start + r"in[- ]store (?:and|&) online\b", lambda m: ["in_store", "online", "pickup"])
    collect("channels", start + r"in[- ]store only\b", lambda m: ["in_store"])
    collect("channels", start + r"online only\b", lambda m: ["online", "pickup"])
    collect("excluded_channels", r"(?:^|[\n.!?]\s*)(?:(?:this )?(?:offer|coupon) (?:is )?)?not valid in[- ]store(?=\s*(?:[.!?\n]|$))",
            lambda m: ["in_store"])
    collect("excluded_channels", r"(?:^|[\n.!?]\s*)(?:(?:this )?(?:offer|coupon) (?:is )?)?not valid online(?=\s*(?:[.!?\n]|$))",
            lambda m: ["online", "pickup"])
    collect("qualification_basis", r"\b(?:minimum|qualifying) (?:purchase|spend)(?: amount)? (?:is )?(?:calculated )?before coupons\b",
            lambda m: "pre_coupon")
    collect("qualification_basis", r"\b(?:minimum|qualifying) (?:purchase|spend)(?: amount)? (?:is )?(?:calculated )?after coupons\b",
            lambda m: "after_coupons")
    collect("per_purchase_limit", r"\blimit (one|two|three|[1-9])(?:\s*\(\s*([1-9])\s*\))? coupons? per (?:purchase|transaction)\b",
            lambda m: int(m[1]) if m[1].isdigit() else {"one": 1, "two": 2, "three": 3}[m[1].lower()])
    # Inconsistent word/numeral limits are discarded rather than interpreted.
    evidence = [e for e in evidence if e["field"] != "per_purchase_limit" or not
                (digits := re.search(r"\(\s*([1-9])\s*\)", e["text"])) or int(digits[1]) == e["value"]]
    if "per_purchase_limit" in values:
        values["per_purchase_limit"] = list(dict.fromkeys(e["value"] for e in evidence if e["field"] == "per_purchase_limit"))
    restrictions = list(dict.fromkeys(m[0].strip() for m in re.finditer(
        r"\b(?:excludes?|excluding|not valid (?:on|with|in[- ]store)|cannot be combined|may not be combined|regular[- ]price(?:d)? items? only|up to\s*\$)[^\n!?]*?(?=\n|$|\.(?:\s|$))",
        text, re.I)))
    return {"fields": {k: v[0] for k, v in values.items() if len(v) == 1},
            "conflicts": sorted(k for k, v in values.items() if len(v) > 1),
            "restrictions_text": restrictions,
            "evidence": evidence,
            "checkout_eligibility_verified": False}


def excluded_units(lines, candidate):
    """Identify literal trial/travel-size conflicts; absence proves nothing."""
    restrictions = [*candidate.get("exclusions_text", []),
                    *(candidate.get("term_candidates") or {}).get("restrictions_text", [])]
    text = " ".join(restrictions)
    labels = set()
    for match in re.finditer(r"\b(?:excludes?|excluding)\s+((?:trial|travel)(?:\s*(?:/|and)\s*(?:trial|travel))?)\s+sizes?\b", text, re.I):
        labels.update(re.findall(r"trial|travel", match[1].lower()))
    excluded = []
    for line in lines:
        name = (line["product"].get("name") or "")
        if any(re.search(r"\b" + label + r"[- ]sizes?\b", name, re.I) for label in labels):
            excluded.extend(f'{line["line_id"]}:{i}' for i in range(1, line["quantity"] + 1))
    return excluded
