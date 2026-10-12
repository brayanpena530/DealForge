"""Bounded one-checkout selection with conditional, expiry-aware wallet scenarios."""

from datetime import timedelta
from decimal import Decimal
import hashlib
from itertools import permutations, product
import json
from zoneinfo import ZoneInfo

from dealforge.providers.cvs.checkout import evaluate_basket, rounded
from dealforge.providers.cvs.pricing import comparison_time, money
from dealforge.providers.cvs.purchase_rules import prepare_purchase
from dealforge.providers.cvs.wallet import parse_timestamp
from dealforge.shopper_settings import planning_profile, store_timezone as local_timezone

ZERO = Decimal(0)


def _ordered_subsets(values):
    yield ()
    for length in range(1, len(values) + 1):
        yield from permutations(values, length)


def _count(values):
    # Weighted ordered subsets: a selected coupon can have several assignments.
    coefficients = [1]
    for value in values:
        weight = len(value.get("unit_options", [value["unit_ids"]]))
        coefficients.append(0)
        for k in range(len(coefficients) - 1, 0, -1):
            coefficients[k] += coefficients[k - 1] * weight
    total, factorial = 0, 1
    for k, coefficient in enumerate(coefficients):
        if k:
            factorial *= k
        total += factorial * coefficient
    return total


def _assignments(coupons):
    choices = [c.get("unit_options", [c["unit_ids"]]) for c in coupons]
    for selected in product(*choices):
        yield tuple({**c, "unit_ids": targets} for c, targets in zip(coupons, selected))


def _profile(profile):
    profile = planning_profile(profile)
    if not isinstance(profile, dict) or set(profile) - {"return_days", "future_eligible_spend", "cash_budget", "new_reward_value"}:
        raise ValueError("Use return_days, future_eligible_spend, cash_budget and new_reward_value")
    days = profile.get("return_days", [7, 30])
    if not isinstance(days, list) or not 1 <= len(days) <= 3 or len(set(days)) != len(days) or any(type(d) is not int or not 1 <= d <= 90 for d in days):
        raise ValueError("Use 1â€“3 unique return-day scenarios between 1 and 90")
    future = profile.get("future_eligible_spend")
    budget = profile.get("cash_budget")
    factor = money(profile.get("new_reward_value", "0.80"))
    if factor > 1:
        raise ValueError("new_reward_value must be 0â€“1")
    return sorted(days), money(future) if future is not None else None, money(budget) if budget is not None else None, factor


def _wallet_value(wallet, when, channel, future_budget):
    """Explicit fungible-future-basket scenario, not a redemption guarantee.

    A shared budget is consumed once across all certificates. Unknown new terms
    are valued at zero. Without a budget, face value is a labeled upper-bound
    preservation scenario rather than invented expected spend.
    """
    eligible, rows = [], []
    for entry in wallet:
        available = False
        try:
            available = (entry.get("preservation_terms_confirmed") is True
                and channel in (entry.get("channels") or []) and entry.get("state") in {"available", "on_card", "earned"}
                and parse_timestamp(entry["valid_from"]) <= when <= parse_timestamp(entry["valid_to"]))
        except (ValueError, KeyError):
            pass
        amount = money(entry["amount"]) if entry.get("amount") is not None else ZERO
        factor = money(entry.get("value_factor", "1.00"))
        row = {"id": entry["id"], "amount": f"{amount:.2f}", "usable_value": "0.00",
               "usable_under_scenario": available, "valid_to": entry.get("valid_to")}
        rows.append(row)
        if available and amount:
            eligible.append((factor, entry["valid_to"], entry["id"], amount, row))
    budget = future_budget
    total = ZERO
    for factor, _, _, amount, row in sorted(eligible, key=lambda e: (-e[0], e[1], e[2])):
        usable = amount if budget is None else min(amount, budget)
        value = rounded(usable * factor)
        row["usable_value"] = f"{value:.2f}"
        total += value
        if budget is not None:
            budget -= usable
    return total, rows


def _candidate_stream(coupons, redemptions):
    fixed = [c for c in coupons if c["kind"] == "amount_off"]
    percent = [c for c in coupons if c["kind"] == "percent_off"]
    canonical = fixed + percent
    # Seed useful comparisons so a large wallet cannot consume the whole search
    # budget before any coupon or single-certificate alternative is tried.
    yield (), ()
    yield tuple(canonical), ()
    yield (), tuple(redemptions)
    yield tuple(canonical), tuple(redemptions)
    for c in canonical:
        yield (c,), ()
    for r in redemptions:
        yield (), (r,)
        yield tuple(canonical), (r,)
    for c, r in product(canonical, redemptions):
        yield (c,), (r,)
    # Avoid itertools.product on factorial generators: it eagerly pools inputs.
    for f in _ordered_subsets(fixed):
        for p in _ordered_subsets(percent):
            for r in _ordered_subsets(redemptions):
                yield f + p, r


def optimize_purchase(lines, *, account_sync, account_ref="cvs-personal", confirmations=None,
                      reward_offers=None, channel="in_store", store_id,
                      store_timezone=None, profile=None, max_candidates=1000,
                      extra_care_ready=False, as_of=None, allow_fixture=False):
    now = comparison_time(as_of)
    store_timezone = local_timezone(store_timezone)
    if type(max_candidates) is not int or not 4 <= max_candidates <= 10000:
        raise ValueError("max_candidates must be 4â€“10000")
    days, future_budget, cash_budget, factor = _profile(profile)
    zone = ZoneInfo(store_timezone if channel == "in_store" else "America/New_York")
    now_local = now.astimezone(zone)
    prepared = prepare_purchase(lines, account_sync=account_sync, account_ref=account_ref,
        confirmations=confirmations, channel=channel, store_id=store_id, store_timezone=store_timezone,
        as_of=now.isoformat(), allow_fixture=allow_fixture)
    result = {"schema_version": 1, "status": "needs_input", "checkout_verified": False,
        "as_of": now.isoformat(), "account_ref": account_ref, "channel": channel, "store_id": store_id,
        "preparation": prepared, "recommended": None, "lowest_cash": None, "no_credit": None,
        "baseline": None, "scenario_recommendations": [], "review": []}
    if prepared["status"] == "needs_input" or prepared["price_resolution"]["status"] != "resolved":
        return result
    earning = reward_offers if reward_offers is not None else []
    if not isinstance(earning, list) or len(earning) > 25 or any(not isinstance(r, dict) for r in earning):
        raise ValueError("Use up to 25 explicit reward-offer objects")
    # An earning campaign must not be cloned across several ad group IDs.
    campaigns = [r.get("campaign_id") for r in earning]
    if any(not isinstance(k, str) or not k.strip() for k in campaigns) or len(set(campaigns)) != len(campaigns):
        raise ValueError("Each reward offer requires a unique campaign_id")
    options = dict(channel=channel, store_id=store_id, extra_care_ready=extra_care_ready,
                   as_of=now.isoformat(), reward_value=f"{factor:.2f}")
    # Validate campaign IDs/rule IDs once across the entire source universe.
    all_ids = [r["id"] for r in prepared["coupons"] + prepared["redemptions"]] + [r.get("id") for r in earning]
    if len(set(all_ids)) != len(all_ids):
        raise ValueError("Offer IDs must be unique across account and earning campaigns")
    pending_ids = {p["id"] for p in prepared["pending"]}
    excluded, eligible_coupons, eligible_redemptions = [], [], []
    for kind, rules, target in (("coupon", prepared["coupons"], eligible_coupons),
                                ("redemption", prepared["redemptions"], eligible_redemptions)):
        for rule in rules:
            if rule["id"] in pending_ids:
                excluded.append({"id": rule["id"], "type": kind, "status": "unresolved", "reason": "account_rule_confirmation_incomplete"})
                continue
            choices = rule.get("unit_options",[rule["unit_ids"]])
            valid_choices, failures = [], []
            for choice in choices:
                assigned = {**rule,"unit_ids":choice}
                solo = evaluate_basket(lines, **options, **({"coupons": [assigned]} if kind == "coupon" else {"redemptions": [assigned]}))
                row = solo["rules"][0]
                if row["status"] == "applied":
                    valid_choices.append(choice)
                else:
                    failures.append(row)
            if valid_choices:
                selected = {**rule,"unit_ids":valid_choices[0]}
                if "unit_options" in rule:
                    selected["unit_options"] = valid_choices
                if any(r["status"] == "unresolved" for r in failures):
                    selected["unit_options_complete"] = False
                    excluded.append({"id":rule["id"],"type":kind,"status":"unresolved","reason":"some_unit_assignments_unresolved"})
                target.append(selected)
            else:
                row = next((r for r in failures if r["status"] == "unresolved"),failures[0])
                excluded.append({"id": rule["id"], "type": kind, "status": row["status"], "reason": row["reason"]})
    if len(eligible_coupons) > 25 or len(eligible_redemptions) > 25:
        raise ValueError("Narrow the selected scope to 25 coupons and 25 certificates")
    total_search = (_count([c for c in eligible_coupons if c["kind"] == "amount_off"])
                    * _count([c for c in eligible_coupons if c["kind"] == "percent_off"])
                    * _count(eligible_redemptions))
    opening = [{**w, "valid_from": prepared["source_snapshot_observed_at"], "value_factor": "1.00"}
               for w in prepared["wallet"]]
    opening_map = {w["id"]:w for w in opening}
    opening_values = {d: _wallet_value(opening, now_local + timedelta(days=d), channel, future_budget)[0] for d in days}
    earning_map = {r["id"]: r for r in earning}
    seen, equivalent, plans = set(), set(), []
    explored = skipped = over_budget = 0
    def expanded():
        for selected, redemptions in _candidate_stream(eligible_coupons, eligible_redemptions):
            for coupons in _assignments(selected):
                yield coupons, redemptions
    for coupons, redemptions in expanded():
        key = (tuple((c["id"],tuple(c["unit_ids"])) for c in coupons), tuple(r["id"] for r in redemptions))
        if key in seen:
            continue
        if explored >= max_candidates:
            break
        seen.add(key)
        explored += 1
        calculation = evaluate_basket(lines, **options, coupons=list(coupons), redemptions=list(redemptions), reward_offers=earning)
        totals = calculation["totals"]
        if totals["cash_due_before_tax"] is None:
            skipped += 1
            continue
        cash = money(totals["cash_due_before_tax"])
        used = {r["id"]: r for r in calculation["rules"] if r["type"] == "redemption" and r["status"] == "applied"}
        ending = [{**entry, "amount": used[entry["id"]]["certificate_remaining"] if entry["id"] in used else entry["amount"]} for entry in opening]
        new_terms_unknown = []
        for row in calculation["rules"]:
            if row["type"] == "earning" and row["status"] == "earned" and money(row["amount"]):
                r = earning_map[row["id"]]
                confirmed = r.get("earned_terms_verified") is True
                try:
                    start, end = parse_timestamp(r.get("earned_valid_from")), parse_timestamp(r.get("earned_valid_to"))
                    confirmed = confirmed and now <= start <= end
                except ValueError:
                    confirmed = False
                channels = r.get("earned_channels")
                confirmed = confirmed and isinstance(channels, list) and bool(channels) and all(c in {"in_store", "pickup", "online"} for c in channels)
                if not confirmed:
                    new_terms_unknown.append(row["id"])
                ending.append({"id": "earned-" + row["id"], "kind": "extrabucks", "amount": row["amount"],
                    "valid_from": r.get("earned_valid_from"), "valid_to": r.get("earned_valid_to"),
                    "state": "earned", "channels": channels, "value_factor": f"{factor:.2f}",
                    "preservation_terms_confirmed": confirmed})
        scores = []
        for d in days:
            when = now_local + timedelta(days=d)
            value, wallet_rows = _wallet_value(ending, when, channel, future_budget)
            scores.append({"return_days": d, "return_at": when.isoformat(),
                "opening_usable_value": f"{opening_values[d]:.2f}", "ending_usable_value": f"{value:.2f}",
                "economic_cost_before_tax": f"{rounded(cash + opening_values[d] - value):.2f}"})
        portfolio = tuple((w["id"], w["amount"]) for w in ending)
        signature = (cash, totals["modeled_coupon_discounts"], portfolio,
                     tuple((r["id"], r.get("amount")) for r in calculation["rules"] if r["type"] == "earning"))
        if signature in equivalent:
            continue
        equivalent.add(signature)
        applied = {r["id"] for r in calculation["rules"] if r["status"] == "applied"}
        coupon_order = [c["id"] for c in coupons if c["id"] in applied]
        redemption_order = [r["id"] for r in redemptions if r["id"] in applied]
        coupon_assignments = [{"id":c["id"],"unit_ids":c["unit_ids"]} for c in coupons if c["id"] in applied]
        plan_id = "plan-" + hashlib.sha256(json.dumps([coupon_assignments,redemption_order,signature], default=str).encode()).hexdigest()[:16]
        plan = {"plan_id": plan_id, "coupon_order": coupon_order, "redemption_order": redemption_order,
            "coupon_assignments": coupon_assignments, "as_of":now.isoformat(),"store_id":store_id,"channel":channel,
            "synthetic_data":prepared["synthetic_data"],
            "totals": totals, "ending_wallet": ending, "scenarios": scores,
            "calculation":{k:v for k,v in calculation.items() if k not in {"units","price_resolution"}},
            "unknown_new_reward_terms": new_terms_unknown,
            "within_cash_budget": cash_budget is None or cash <= cash_budget}
        # All candidates use the same validated prices. Avoid retaining one
        # copy of thousands of unit allocations per search candidate.
        plan["calculation"]["price_resolution"] = prepared["price_resolution"]
        plan["_rank"] = (max(Decimal(s["economic_cost_before_tax"]) for s in scores),
            sum((Decimal(s["economic_cost_before_tax"]) for s in scores), ZERO),
            money(totals["modeled_reward_value_forfeited"]), cash, len(redemption_order),
            tuple(sorted(opening_map[key]["valid_to"] for key in redemption_order)), plan_id)
        plans.append(plan)
        if not plan["within_cash_budget"]:
            over_budget += 1
    feasible = [p for p in plans if p["within_cash_budget"]]
    def view(plan):
        if plan is None:
            return None
        result = {k:v for k,v in plan.items() if not k.startswith("_")}
        result["scenarios"] = [{**s,"wallet_values":_wallet_value(plan["ending_wallet"],
            parse_timestamp(s["return_at"]),channel,future_budget)[1]} for s in plan["scenarios"]]
        return result
    recommended = min(feasible, key=lambda p: p["_rank"]) if feasible else None
    lowest = min(feasible, key=lambda p: (money(p["totals"]["cash_due_before_tax"]), p["_rank"])) if feasible else None
    no_credit = [p for p in feasible if not p["redemption_order"]]
    baseline = next((p for p in plans if not p["coupon_order"] and not p["redemption_order"]), None)
    result.update(recommended=view(recommended), lowest_cash=view(lowest),
        no_credit=view(min(no_credit, key=lambda p: p["_rank"])) if no_credit else None,
        baseline=view(baseline), excluded_rules=excluded,
        search={"candidate_limit": max_candidates, "explored": explored, "possible_ordered_selections": total_search,
                "complete": explored == total_search and all(c.get("unit_options_complete",True) for c in eligible_coupons),
                "unit_assignments_complete": all(c.get("unit_options_complete",True) for c in eligible_coupons), "unique_outcomes": len(plans),
                "unrankable": skipped, "over_cash_budget": over_budget,
                "claim": "best_found_for_modeled_order_and_selected_rules"},
        assumptions={"return_days": days, "future_eligible_spend": f"{future_budget:.2f}" if future_budget is not None else None,
            "future_value_mode": "shared_eligible_basket" if future_budget is not None else "face_value_upper_bound",
            "new_reward_value": f"{factor:.2f}", "cash_budget_before_tax": f"{cash_budget:.2f}" if cash_budget is not None else None,
            "checkouts": 1},
        review=["conditional_order_and_future_redemption_assumptions", "tax_fees_excluded_from_budget",
            "coupon_subset_and_redemption_order_require_checkout_confirmation",
            "no_extras_or_transaction_splitting", "refresh_account_prices_and_activation_before_checkout",
            "stock_requires_separate_confirmation", "receipt_validation_not_yet_performed"])
    if future_budget is None:
        result["review"].append("future_basket_unknown_preservation_values_are_upper_bounds")
    result["at_risk_rewards"] = [{"id":w["id"],"amount":w["amount"],"valid_to":w["valid_to"],
        "expires_before_first_return":parse_timestamp(w["valid_to"]) < now_local+timedelta(days=days[0])}
        for w in opening if w["valid_to"] and now <= parse_timestamp(w["valid_to"]) < now_local+timedelta(days=days[-1])]
    result["status"] = "no_feasible_plan" if not feasible else "partial" if (prepared["pending"] or not prepared["inventory_complete"]
        or any(e["status"] == "unresolved" for e in excluded) or recommended["unknown_new_reward_terms"]
        or recommended["totals"]["new_rewards_earned"] is None) else "conditional_estimate"
    for index, d in enumerate(days):
        if feasible:
            best = min(feasible, key=lambda p: (Decimal(p["scenarios"][index]["economic_cost_before_tax"]), p["_rank"]))
            result["scenario_recommendations"].append({"return_days": d, "plan_id": best["plan_id"],
                "coupon_order": best["coupon_order"], "redemption_order": best["redemption_order"],
                "cash_before_tax": best["totals"]["cash_due_before_tax"], "scenario": best["scenarios"][index]})
    result["sensitive_to_return_date"] = len({s["plan_id"] for s in result["scenario_recommendations"]}) > 1
    result["synthetic_data"] = prepared["synthetic_data"]
    result["execution_ready"] = False
    return result
