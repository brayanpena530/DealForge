"""Compare caller-normalized receipt evidence to a conditional basket calculation."""

from datetime import timedelta

from dealforge.providers.cvs.pricing import money
from dealforge.providers.cvs.wallet import parse_timestamp


def reconcile_receipt(plan, receipt):
    if not isinstance(plan,dict) or not isinstance(receipt,dict):
        raise ValueError("Use a saved optimizer plan and a normalized receipt")
    if receipt.get("source") not in {"fixture","shopper_receipt"} or receipt.get("verified_by_caller") is not True:
        raise ValueError("Receipt source and caller verification are required")
    calculation = plan.get("calculation")
    if not isinstance(calculation,dict) or not isinstance(calculation.get("totals"),dict):
        raise ValueError("Plan must contain its calculation and totals")
    checked, missing, mismatches = [], [], []
    for field in ("store_id","channel"):
        if receipt.get(field) != plan.get(field) or plan.get(field) is None:
            mismatches.append({"field":field,"reason":"receipt_context_mismatch"})
    age = parse_timestamp(receipt.get("transaction_at")) - parse_timestamp(plan.get("as_of"))
    if not timedelta(0) <= age <= timedelta(hours=24):
        mismatches.append({"field":"transaction_at","reason":"plan_outside_recheck_window"})
    mappings = {"item_subtotal":"item_subtotal","coupon_discounts":"modeled_coupon_discounts",
        "rewards_spent":"modeled_rewards_spent","reward_value_forfeited":"modeled_reward_value_forfeited",
        "new_rewards_earned":"new_rewards_earned","cash_before_tax":"cash_due_before_tax"}
    for receipt_field, plan_field in mappings.items():
        actual, expected = receipt.get(receipt_field), calculation["totals"].get(plan_field)
        if actual is None or expected is None:
            missing.append(receipt_field)
            continue
        actual, expected = money(actual), money(expected)
        checked.append(receipt_field)
        if actual != expected:
            mismatches.append({"field":receipt_field,"expected":f"{expected:.2f}",
                "observed":f"{actual:.2f}","difference":f"{actual-expected:.2f}"})
    identities = {"coupon_ids":plan.get("coupon_order",[]),"reward_redemption_ids":plan.get("redemption_order",[]),
        "earned_offer_ids":[r["id"] for r in calculation["rules"] if r["type"] == "earning" and r["status"] == "earned"]}
    for field, expected_ids in identities.items():
        actual_ids = receipt.get(field)
        if actual_ids is None:
            missing.append(field)
            continue
        if not isinstance(actual_ids,list) or any(not isinstance(k,str) for k in actual_ids) or len(set(actual_ids)) != len(actual_ids):
            raise ValueError("Receipt offer identities must be unique opaque IDs")
        checked.append(field)
        if set(actual_ids) != set(expected_ids):
            mismatches.append({"field":field,"reason":"offer_or_certificate_identity_differs"})
    items = receipt.get("items")
    if items is None:
        missing.append("items")
    else:
        if not isinstance(items,list) or len(items)>25:
            raise ValueError("Receipt items must be up to 25 exact SKU rows")
        observed = {}
        for row in items:
            if (not isinstance(row,dict) or type(row.get("quantity")) is not int or row["quantity"] < 1
                    or not isinstance(row.get("product_id"),str) or not isinstance(row.get("sku_id"),str)):
                raise ValueError("Receipt items need product_id, sku_id, quantity and pre-coupon unit_price")
            key = (row["product_id"],row["sku_id"],money(row.get("unit_price")))
            observed[key] = observed.get(key,0)+row["quantity"]
        expected = {}
        for row in calculation["price_resolution"]["lines"]:
            key = (row["product_id"],row["sku_id"],money(row["price"]["amount"]))
            expected[key] = expected.get(key,0)+row["quantity"]
        checked.append("items")
        if observed != expected:
            mismatches.append({"field":"items","reason":"sku_quantity_or_base_price_differs"})
    synthetic = receipt["source"] == "fixture" or plan.get("synthetic_data") is True
    return {"status":"mismatch" if mismatches else "partial" if missing else "matched",
        "plan_id":plan.get("plan_id"),"synthetic_data":synthetic,
        "real_receipt_reconciled":not synthetic and not mismatches and not missing,
        "evidence_is_caller_attested":True,"checkout_verified":False,
        "checked":checked,"missing":missing,"mismatches":mismatches,
        "review":["matching_one_receipt_does_not_prove_future_pos_behavior","tax_and_fees_not_reconciled",
                  "raw_receipt_and_personal_identifiers_not_required"]}
