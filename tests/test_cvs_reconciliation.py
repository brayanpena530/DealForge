from copy import deepcopy

import pytest

from dealforge.providers.cvs.reconciliation import reconcile_receipt
from test_cvs_optimizer import account, certificate, optimize


def receipt(plan):
    totals = plan["totals"]
    return {"source":"fixture","verified_by_caller":True,"store_id":plan["store_id"],
        "channel":plan["channel"],"transaction_at":plan["as_of"],
        "item_subtotal":totals["item_subtotal"],"coupon_discounts":totals["modeled_coupon_discounts"],
        "rewards_spent":totals["modeled_rewards_spent"],"reward_value_forfeited":totals["modeled_reward_value_forfeited"],
        "new_rewards_earned":totals["new_rewards_earned"],"cash_before_tax":totals["cash_due_before_tax"],
        "coupon_ids":plan["coupon_order"],"reward_redemption_ids":plan["redemption_order"],
        "earned_offer_ids":[r["id"] for r in plan["calculation"]["rules"] if r["type"] == "earning" and r["status"] == "earned"],
        "items":[{"product_id":l["product_id"],"sku_id":l["sku_id"],"quantity":l["quantity"],
                  "unit_price":l["price"]["amount"]} for l in plan["calculation"]["price_resolution"]["lines"]]}


def test_matching_fixture_never_claims_real_pos_validation():
    plan = optimize(account([certificate()]))["recommended"]
    result = reconcile_receipt(plan,receipt(plan))
    assert result["status"] == "matched" and result["synthetic_data"]
    assert not result["real_receipt_reconciled"] and not result["checkout_verified"]
    actual = receipt(plan)
    actual["source"] = "shopper_receipt"
    assert not reconcile_receipt(plan,actual)["real_receipt_reconciled"]


def test_caller_receipt_against_nonfixture_plan_labeled_as_attestation():
    # Synthetic construction tests labels, not an actual receipt observation.
    plan = optimize(account())["recommended"]
    plan["synthetic_data"] = False
    actual = receipt(plan)
    actual["source"] = "shopper_receipt"
    result = reconcile_receipt(plan,actual)
    assert result["real_receipt_reconciled"] and result["evidence_is_caller_attested"]
    assert not result["checkout_verified"]


@pytest.mark.parametrize("field,value",[("cash_before_tax","99.00"),("store_id","999"),
    ("channel","online"),("transaction_at","2026-10-12T12:00:00-05:00")])
def test_receipt_context_and_money_differences(field,value):
    plan = optimize(account())["recommended"]
    actual = receipt(plan)
    actual[field] = value
    result = reconcile_receipt(plan,actual)
    assert result["status"] == "mismatch"
    assert any(m["field"] == field for m in result["mismatches"])


def test_missing_fields_and_exact_sku_mismatch():
    plan = optimize(account())["recommended"]
    actual = receipt(plan)
    actual.pop("reward_value_forfeited")
    assert reconcile_receipt(plan,actual)["status"] == "partial"
    actual = receipt(plan)
    actual["items"][0]["sku_id"] = "different"
    assert reconcile_receipt(plan,actual)["status"] == "mismatch"


def test_invalid_receipt_and_input_not_mutated():
    plan = optimize(account())["recommended"]
    actual = receipt(plan)
    saved = deepcopy((plan,actual))
    reconcile_receipt(plan,actual)
    assert (plan,actual) == saved
    with pytest.raises(ValueError):
        reconcile_receipt(plan,{**actual,"verified_by_caller":False})


def test_same_money_with_different_certificate_is_not_a_wallet_match():
    plan = optimize(account([certificate()]))["recommended"]
    actual = receipt(plan)
    actual["reward_redemption_ids"] = ["different-certificate"]
    assert reconcile_receipt(plan,actual)["status"] == "mismatch"
