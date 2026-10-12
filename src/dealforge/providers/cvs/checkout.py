"""Deterministic, conditional single-basket arithmetic; never a POS guarantee.

Only explicitly attested rules are modeled. Unknown rules are reported and
suppress the corresponding complete totals. No automatic coupon selection,
ad-to-SKU inference, 98% threshold tolerance, tax calculation or account changes.
"""

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from dealforge.providers.cvs.pricing import (
    comparison_time, fresh, identifier, money, resolve_prices, valid_window,
)

CENT = Decimal("0.01")
ZERO = Decimal(0)


def rounded(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _quantity(value):
    if type(value) is not int or not 1 <= value <= 2500:
        raise ValueError("Rule quantities must be integers 1–2500")
    return value


def _allocate(amount, targets, units, field):
    """Proportional cents allocation with stable largest-remainder tie breaks."""
    weights = {k: units[k]["remaining"] for k in targets}
    total = sum(weights.values(), ZERO)
    amount = min(rounded(amount), total)
    if not amount or not total:
        return ZERO
    raw = {k: amount * w / total for k, w in weights.items()}
    cuts = {k: v.quantize(CENT, rounding=ROUND_DOWN) for k, v in raw.items()}
    pennies = int((amount - sum(cuts.values(), ZERO)) / CENT)
    order = sorted(targets, key=lambda k: raw[k] - cuts[k], reverse=True)
    for key in order[:pennies]:
        cuts[key] += CENT
    for key, cut in cuts.items():
        units[key][field] += cut
        units[key]["remaining"] -= cut
    return amount


def _basis(unit, name):
    if name == "pre_coupon":
        return unit["base"]
    if name == "after_coupons":
        return unit["base"] - unit["coupon_discount"]
    if name == "after_redemption":
        return unit["remaining"]
    raise ValueError("Unknown qualification basis")


def _rule_check(rule, units, channel, now, card_ready, *, earning=False):
    identifier(rule.get("id"))
    targets = rule.get("unit_ids")
    if (not isinstance(targets, list) or not targets or len(targets) != len(set(targets))
            or any(not isinstance(k, str) or k not in units for k in targets)):
        raise ValueError("Every rule needs unique unit_ids returned by price resolution")
    # Preserve basket order so reordering equivalent target sets changes nothing.
    targets = [k for k in units if k in targets]
    window = valid_window(rule, now)
    if window == "outside_validity_window":
        return targets, "not_applicable", window
    if rule.get("state") in {"redeemed", "unavailable"}:
        return targets, "not_applicable", "not_available"
    channels = rule.get("channels")
    if channels is not None and (not isinstance(channels, list) or not channels
            or any(c not in {"in_store", "pickup", "online"} for c in channels)):
        raise ValueError("Rule channels must explicitly name in_store, pickup or online")
    if channels and channel not in channels:
        return targets, "not_applicable", "channel_excluded"
    if type(rule.get("remaining_uses")) is int and rule["remaining_uses"] == 0:
        return targets, "not_applicable", "redemption_limit_reached"
    if window:
        return targets, "unresolved", window
    if not fresh(rule.get("observed_at"), now):
        return targets, "unresolved", "rule_stale_or_future"
    if card_ready is not True:
        return targets, "unresolved", "qualifying_card_required"
    if rule.get("state") not in ({"on_card", "presented", "automatic"} if earning else {"on_card", "presented"}):
        return targets, "unresolved", "activation_or_presentation_required"
    if not channels:
        return targets, "unresolved", "channel_unknown"
    for flag in ("terms_verified", "product_eligibility_verified", "stacking_verified", "limits_verified"):
        if rule.get(flag) is not True:
            return targets, "unresolved", flag + "_required"
    if type(rule.get("remaining_uses")) is not int or rule["remaining_uses"] < 1:
        return targets, "unresolved", "remaining_uses_unknown"
    if not isinstance(rule.get("source_ref"), str) or not rule["source_ref"].strip() or len(rule["source_ref"]) > 500:
        return targets, "unresolved", "rule_source_reference_required"
    return targets, None, None


def _qualification(rule, targets, units, threshold_reserved=None):
    """Require complete threshold terms, never infer pre/post-coupon spend."""
    if rule.get("minimum_quantity") is None or rule.get("minimum_spend") is None:
        return "unresolved", "qualification_terms_unknown", None
    qty, spend = _quantity(rule["minimum_quantity"]), money(rule["minimum_spend"])
    if len(targets) < qty:
        return "not_applicable", "minimum_quantity_not_met", None
    basis = rule.get("qualification_basis")
    if spend and basis not in {"pre_coupon", "after_coupons", "after_redemption"}:
        return "unresolved", "spend_qualification_basis_unknown", None
    budget = {k: max(ZERO, _basis(units[k], basis) - (threshold_reserved or {}).get(k, ZERO))
              for k in targets} if spend else {}
    if sum(budget.values(), ZERO) < spend:
        return "not_applicable", "minimum_spend_not_met", None
    return None, None, (spend, budget)


def evaluate_basket(lines, *, channel, store_id, coupons=None, redemptions=None,
                    reward_offers=None, extra_care_ready=False, reward_value="0.80", as_of=None):
    """Resolve prices, then model fixed coupons → old rewards → percent coupons.

    Rule checks are caller attestations. Percent coupons use the remaining
    eligible regular-price subtotal and a maximum $99 discount. Repeat threshold
    coupons reserve separate qualifying spend. This models one redemption per
    supplied ID and one award per supplied reward offer; no transaction splitting.
    """
    now = comparison_time(as_of)
    valuation = money(reward_value)
    if valuation > 1 or type(extra_care_ready) is not bool:
        raise ValueError("reward_value must be 0–1; extra_care_ready must be boolean")
    groups = [v if v is not None else [] for v in (coupons, redemptions, reward_offers)]
    ids = set()
    for group in groups:
        if not isinstance(group, list) or len(group) > 25:
            raise ValueError("Use up to 25 rules of each type")
        for rule in group:
            if not isinstance(rule, dict):
                raise ValueError("Rules must be objects")
            key = identifier(rule.get("id"))
            if key in ids:
                raise ValueError("Rule IDs must be unique across coupons, redemptions and rewards")
            ids.add(key)
    if sum(r.get("kind") == "percent_back" for r in groups[2]) > 1:
        raise ValueError("Supply the base 2% program once, covering its eligible units")
    if sum(r.get("kind") == "membership_bonus" for r in groups[1]) > 1:
        raise ValueError("Supply at most one monthly membership bonus")
    prices = resolve_prices(lines, channel=channel, store_id=store_id, as_of=now.isoformat())
    result = {"schema_version": 1, "status": "needs_input", "as_of": now.isoformat(),
        "checkout_verified": False, "price_resolution": prices, "rules": [], "units": [],
        "totals": None, "review": ["only_supplied_rules_are_modeled", "rule_verification_is_caller_attested",
            "order_and_proportional_allocation_are_estimates", "tax_fees_and_shipping_excluded",
            "new_rewards_are_for_future_purchases", "no_98_percent_threshold_tolerance"]}
    if prices["status"] != "resolved":
        return result
    units = {}
    for line in prices["lines"]:
        for key in line["unit_ids"]:
            value = money(line["price"]["amount"])
            units[key] = {"base": value, "remaining": value, "coupon_discount": ZERO,
                "reward_used": ZERO, "sale_status": line["price"]["sale_status"]}
    mfr_used, threshold_reserved = set(), {}
    spent, consumed, wasted, earned = ZERO, ZERO, ZERO, ZERO
    unresolved_cash = unresolved_earned = False

    def report(rule, kind, status, reason=None, **fields):
        nonlocal unresolved_cash, unresolved_earned
        if status == "unresolved":
            if kind == "earning":
                unresolved_earned = True
            else:
                unresolved_cash = True
        result["rules"].append({"id": rule["id"], "type": kind, "status": status,
                               "reason": reason, **fields})

    fixed, percent = [], []
    for rule in groups[0]:
        if rule.get("kind") == "amount_off":
            fixed.append(rule)
        elif rule.get("kind") == "percent_off":
            percent.append(rule)
        else:
            report(rule, "coupon", "unresolved", "unsupported_coupon_kind")

    def apply_coupon(rule):
        targets, status, reason = _rule_check(rule, units, channel, now, extra_care_ready)
        if status:
            report(rule, "coupon", status, reason)
            return
        issuer = rule.get("issuer")
        if issuer not in {"mfr_coupon", "store_coupon"}:
            report(rule, "coupon", "unresolved", "issuer_unknown")
            return
        if issuer == "mfr_coupon" and mfr_used.intersection(targets):
            report(rule, "coupon", "not_applicable", "manufacturer_coupon_unit_overlap")
            return
        if rule.get("value") is None:
            report(rule, "coupon", "unresolved", "discount_value_unknown")
            return
        value = money(rule["value"])
        if rule["kind"] == "percent_off":
            if not 0 < value <= 100:
                raise ValueError("Percent discounts must be greater than 0 and at most 100")
            if issuer != "store_coupon":
                report(rule, "coupon", "unresolved", "manufacturer_percent_rule_unsupported")
                return
            if any(units[k]["sale_status"] == "unknown" for k in targets):
                report(rule, "coupon", "unresolved", "regular_price_eligibility_unknown")
                return
            targets = [k for k in targets if units[k]["sale_status"] == "regular"]
            if not targets:
                report(rule, "coupon", "not_applicable", "percent_coupon_excludes_sale_items")
                return
        status, reason, qualification = _qualification(rule, targets, units,
            threshold_reserved if issuer == "store_coupon" else None)
        if status:
            report(rule, "coupon", status, reason)
            return
        if rule["kind"] == "percent_off":
            amount = rounded(sum((units[k]["remaining"] for k in targets), ZERO) * value / 100)
            amount = min(amount, Decimal(99), money(rule.get("max_discount", "99.00")))
        else:
            amount = value
        used = _allocate(amount, targets, units, "coupon_discount")
        if used and issuer == "mfr_coupon":
            mfr_used.update(targets)
        if used and issuer == "store_coupon":
            remaining, budgets = qualification
            # Reserve separate spend for each threshold coupon, even if callers
            # attach two coupons to the same entire category/basket.
            for key, available in budgets.items():
                reservation = min(remaining, available)
                threshold_reserved[key] = threshold_reserved.get(key, ZERO) + reservation
                remaining -= reservation
        report(rule, "coupon", "applied" if used else "not_applicable",
            None if used else "no_eligible_balance", discount=f"{used:.2f}",
            unused_coupon_value=f"{max(ZERO, amount - used):.2f}")

    for rule in fixed:
        apply_coupon(rule)
    for rule in groups[1]:
        targets, status, reason = _rule_check(rule, units, channel, now, extra_care_ready)
        if status:
            report(rule, "redemption", status, reason)
            continue
        if rule.get("kind") not in {"extrabucks", "membership_bonus"}:
            report(rule, "redemption", "unresolved", "unsupported_reward_certificate_kind")
            continue
        if rule.get("amount") is None or type(rule.get("partial_redemption")) is not bool:
            report(rule, "redemption", "unresolved", "certificate_value_or_partial_redemption_unknown")
            continue
        status, reason, _ = _qualification(rule, targets, units)
        if status:
            report(rule, "redemption", status, reason)
            continue
        face = money(rule["amount"])
        # A monthly bonus needs a $10 CVS.com purchase; a smaller custom
        # threshold cannot override that public restriction.
        if rule["kind"] == "membership_bonus" and channel in {"online", "pickup"}:
            if sum((units[k]["remaining"] for k in targets), ZERO) < max(face, Decimal(10)):
                report(rule, "redemption", "not_applicable", "online_bonus_minimum_not_met")
                continue
        used = _allocate(face, targets, units, "reward_used")
        lost = face - used if used and not rule["partial_redemption"] else ZERO
        spent += used
        consumed += used + lost
        wasted += lost
        report(rule, "redemption", "applied" if used else "not_applicable",
            None if used else "no_eligible_balance", used=f"{used:.2f}", forfeited=f"{lost:.2f}",
            certificate_remaining=f"{face - used - lost:.2f}")
    for rule in percent:
        apply_coupon(rule)
    for rule in groups[2]:
        targets, status, reason = _rule_check(rule, units, channel, now, extra_care_ready, earning=True)
        if status:
            report(rule, "earning", status, reason)
            continue
        if rule.get("progress_verified") is not True or rule.get("prior_spend") is None or rule.get("prior_quantity") is None:
            report(rule, "earning", "unresolved", "prior_progress_unknown")
            continue
        prior_spend = money(rule["prior_spend"])
        prior_qty = rule["prior_quantity"]
        if type(prior_qty) is not int or not 0 <= prior_qty <= 2500:
            raise ValueError("prior_quantity must be an integer 0–2500")
        if rule.get("minimum_spend") is None or rule.get("minimum_quantity") is None:
            report(rule, "earning", "unresolved", "qualification_terms_unknown")
            continue
        minimum = money(rule["minimum_spend"])
        minimum_qty = _quantity(rule["minimum_quantity"])
        basis = rule.get("qualification_basis")
        if (minimum or rule.get("kind") == "percent_back") and basis not in {"pre_coupon", "after_coupons", "after_redemption"}:
            report(rule, "earning", "unresolved", "spend_qualification_basis_unknown")
            continue
        subtotal = sum((_basis(units[k], basis) for k in targets), ZERO) if basis in {"pre_coupon", "after_coupons", "after_redemption"} else ZERO
        if subtotal + prior_spend < minimum or len(targets) + prior_qty < minimum_qty:
            report(rule, "earning", "not_applicable", "reward_threshold_not_met",
                qualifying_spend=f"{subtotal:.2f}")
            continue
        if rule.get("kind") == "extrabucks":
            if rule.get("amount") is None:
                report(rule, "earning", "unresolved", "reward_amount_unknown")
                continue
            amount = money(rule["amount"])
        elif rule.get("kind") == "percent_back":
            if rule.get("value") is None or rule.get("max_reward") is None:
                report(rule, "earning", "unresolved", "percent_reward_value_or_remaining_cap_unknown")
                continue
            value = money(rule["value"])
            # The supported base loyalty program is specifically 2%, with
            # known remaining daily/transaction limits supplied by the caller.
            if (value != 2 or basis != "after_redemption" or minimum < Decimal("0.50")
                    or prior_spend or prior_qty):
                report(rule, "earning", "unresolved", "unsupported_base_loyalty_terms")
                continue
            amount = min(rounded(subtotal * value / 100), money(rule["max_reward"]), Decimal(5))
        else:
            report(rule, "earning", "unresolved", "unsupported_reward_offer_kind")
            continue
        earned += amount
        report(rule, "earning", "earned", amount=f"{amount:.2f}", qualifying_spend=f"{subtotal:.2f}")

    base = sum((u["base"] for u in units.values()), ZERO)
    discounts = sum((u["coupon_discount"] for u in units.values()), ZERO)
    cash = sum((u["remaining"] for u in units.values()), ZERO)
    result["units"] = [{"unit_id": key, **{k: f"{v:.2f}" if isinstance(v, Decimal) else v
                        for k, v in unit.items()}} for key, unit in units.items()]
    result["status"] = "partial" if unresolved_cash or unresolved_earned else "conditional_estimate"
    result["totals"] = {"currency": "USD", "item_subtotal": f"{base:.2f}",
        "modeled_coupon_discounts": f"{discounts:.2f}", "modeled_rewards_spent": f"{spent:.2f}",
        "modeled_reward_face_value_consumed": f"{consumed:.2f}", "modeled_reward_value_forfeited": f"{wasted:.2f}",
        "modeled_cash_before_tax": f"{cash:.2f}",
        "cash_due_before_tax": None if unresolved_cash else f"{cash:.2f}",
        "modeled_new_rewards": f"{earned:.2f}",
        "new_rewards_earned": None if unresolved_earned or unresolved_cash else f"{earned:.2f}",
        "tax": None, "checkout_total": None, "reward_value_factor": f"{valuation:.2f}",
        "economic_cost_before_tax": None if unresolved_cash or unresolved_earned else
            f"{rounded(cash + consumed - valuation * earned):.2f}"}
    return result
