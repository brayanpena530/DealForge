"""Synthetic checkout scenarios; no shopper data or claims of live POS acceptance."""

from copy import deepcopy
from decimal import Decimal

import pytest

from dealforge.providers.cvs.checkout import evaluate_basket
from dealforge.providers.cvs.pricing import resolve_prices

NOW = "2026-10-10T12:00:00-05:00"
URL = "https://www.cvs.com/shop/example-prodid-123?skuId=456"


def lines(amount="5.99", quantity=2, *, sale_status="regular"):
    product = {"product_id": "123", "sku_id": "456", "url": URL,
        "selected_sku_verified": True, "availability": "in_stock",
        "price": {"amount": amount, "currency": "USD", "channel": "online",
                  "source": "cvs_rendered_page", "source_url": URL, "observed_at": NOW},
        "store_context": {"store_id": "10001"}}
    evidence = {"product_id": "123", "sku_id": "456", "amount": amount,
        "currency": "USD", "channel": "in_store", "store_id": "10001",
        "source": "shelf_label", "source_ref": "synthetic shelf observation",
        "observed_at": NOW, "verified_by_caller": True, "sale_status": sale_status}
    return [{"line_id": "paste", "quantity": quantity, "product": product, "price_evidence": [evidence]}]


def rule(key="coupon", **kwargs):
    return {"id": key, "unit_ids": ["paste:1", "paste:2"], "state": "on_card",
        "channels": ["in_store", "online", "pickup"], "observed_at": NOW,
        "valid_from": "2026-10-04T00:00:00-05:00", "valid_to": "2026-10-11T23:59:59-05:00",
        "terms_verified": True, "product_eligibility_verified": True,
        "stacking_verified": True, "limits_verified": True, "remaining_uses": 1,
        "source_ref": "synthetic full terms", "minimum_quantity": 1, "minimum_spend": "0.00",
        "qualification_basis": "pre_coupon", "kind": "amount_off", "value": "3.00",
        "issuer": "mfr_coupon", **kwargs}


def evaluate(data=None, **kwargs):
    return evaluate_basket(data or lines(), channel="in_store", store_id="10001", as_of=NOW,
                           extra_care_ready=True, **kwargs)


def test_two_item_coupon_old_certificate_and_new_reward_are_separate():
    result = evaluate(coupons=[rule(minimum_quantity=2)],
        redemptions=[rule("old", kind="extrabucks", amount="4.00", partial_redemption=False)],
        reward_offers=[rule("new", kind="extrabucks", amount="5.00", minimum_spend="10.00",
            progress_verified=True, prior_spend="0.00", prior_quantity=0)])
    total = result["totals"]
    assert result["status"] == "conditional_estimate" and not result["checkout_verified"]
    assert total["item_subtotal"] == "11.98"
    assert total["modeled_coupon_discounts"] == "3.00"
    assert total["modeled_rewards_spent"] == "4.00"
    assert total["cash_due_before_tax"] == "4.98"
    assert total["new_rewards_earned"] == "5.00"
    assert total["economic_cost_before_tax"] == "4.98"
    assert total["tax"] is None and total["checkout_total"] is None


def test_unknown_discount_suppresses_cash_and_reward_guarantees():
    result = evaluate(coupons=[rule(stacking_verified=False)], reward_offers=[rule(
        "earn", kind="extrabucks", amount="5.00", progress_verified=True, prior_spend="0.00", prior_quantity=0)])
    assert result["status"] == "partial"
    assert result["totals"]["modeled_cash_before_tax"] == "11.98"
    assert result["totals"]["cash_due_before_tax"] is None
    assert result["totals"]["new_rewards_earned"] is None
    assert result["totals"]["economic_cost_before_tax"] is None


@pytest.mark.parametrize("changes,reason", [
    ({"terms_verified": False}, "terms_verified_required"),
    ({"product_eligibility_verified": False}, "product_eligibility_verified_required"),
    ({"limits_verified": False}, "limits_verified_required"),
    ({"state": "available"}, "activation_or_presentation_required"),
    ({"channels": None}, "channel_unknown"),
    ({"remaining_uses": None}, "remaining_uses_unknown"),
    ({"observed_at": "2026-10-08T12:00:00-05:00"}, "rule_stale_or_future"),
    ({"valid_to": None}, "validity_window_unknown"),
    ({"issuer": None}, "issuer_unknown"),
    ({"value": None}, "discount_value_unknown"),
    ({"minimum_spend": None}, "qualification_terms_unknown"),
    ({"minimum_spend": "10.00", "qualification_basis": None}, "spend_qualification_basis_unknown"),
])
def test_missing_terms_never_become_savings(changes, reason):
    result = evaluate(coupons=[rule(**changes)])
    assert result["rules"][0]["reason"] == reason
    assert result["totals"]["cash_due_before_tax"] is None


@pytest.mark.parametrize("changes,reason", [
    ({"valid_to": "2026-10-09T23:59:59-05:00"}, "outside_validity_window"),
    ({"state": "redeemed"}, "not_available"),
    ({"channels": ["online"]}, "channel_excluded"),
    ({"remaining_uses": 0}, "redemption_limit_reached"),
    ({"minimum_quantity": 3}, "minimum_quantity_not_met"),
    ({"minimum_spend": "12.00"}, "minimum_spend_not_met"),
])
def test_known_inapplicable_rule_does_not_block_base_estimate(changes, reason):
    result = evaluate(coupons=[rule(**changes)])
    assert result["rules"][0]["reason"] == reason
    assert result["totals"]["cash_due_before_tax"] == "11.98"


def test_one_manufacturer_coupon_per_target_unit():
    result = evaluate(coupons=[rule("one", unit_ids=["paste:1"]),
        rule("overlap", unit_ids=["paste:1"]), rule("other", unit_ids=["paste:2"])])
    assert result["rules"][1]["reason"] == "manufacturer_coupon_unit_overlap"
    assert result["totals"]["cash_due_before_tax"] == "5.98"


def test_two_three_off_fifteen_store_coupons_need_thirty_dollars():
    coupons = [rule("first", issuer="store_coupon", minimum_spend="15.00"),
               rule("second", issuer="store_coupon", minimum_spend="15.00")]
    short = evaluate(lines("10.00"), coupons=coupons)
    assert short["totals"]["modeled_coupon_discounts"] == "3.00"
    full = evaluate(lines("15.00"), coupons=coupons)
    assert full["totals"]["modeled_coupon_discounts"] == "6.00"


def test_percent_after_fixed_and_redemption_and_sale_items_excluded():
    data = lines("10.00", 1) + [dict(lines("10.00", 1, sale_status="sale")[0], line_id="sale")]
    targets = ["paste:1", "sale:1"]
    result = evaluate(data, coupons=[rule("flat", unit_ids=targets, value="2.00"),
        rule("pct", unit_ids=targets, issuer="store_coupon", kind="percent_off", value="20.00")],
        redemptions=[rule("old", unit_ids=targets, kind="extrabucks", amount="4.00", partial_redemption=True)])
    assert result["totals"]["modeled_coupon_discounts"] == "3.40"
    assert result["totals"]["cash_due_before_tax"] == "12.60"


def test_percent_unknown_sale_status_and_cap():
    unresolved = evaluate(lines(sale_status="unknown"), coupons=[rule(kind="percent_off", issuer="store_coupon", value="20.00")])
    assert unresolved["rules"][0]["reason"] == "regular_price_eligibility_unknown"
    capped = evaluate(lines("1000.00"), coupons=[rule(kind="percent_off", issuer="store_coupon", value="20.00")])
    assert capped["totals"]["modeled_coupon_discounts"] == "99.00"


@pytest.mark.parametrize("partial,waste,remaining", [(False, "4.00", "0.00"), (True, "0.00", "4.00")])
def test_certificate_overage_and_zero_cash(partial, waste, remaining):
    result = evaluate(lines("3.00"), redemptions=[rule("old", kind="extrabucks", amount="10.00", partial_redemption=partial)])
    assert result["totals"]["cash_due_before_tax"] == "0.00"
    assert result["totals"]["modeled_reward_value_forfeited"] == waste
    assert result["rules"][0]["certificate_remaining"] == remaining


def test_unknown_partial_redemption_and_no_reward_burn_on_zero_balance():
    unknown = evaluate(redemptions=[rule("old", kind="extrabucks", amount="10.00")])
    assert unknown["totals"]["cash_due_before_tax"] is None
    zero = evaluate(lines("1.00"), coupons=[rule(value="5.00")], redemptions=[
        rule("old", kind="extrabucks", amount="10.00", partial_redemption=False)])
    assert zero["totals"]["modeled_reward_face_value_consumed"] == "0.00"
    assert zero["rules"][0]["discount"] == "2.00"


def test_online_monthly_bonus_minimum():
    result = evaluate_basket(lines("4.00"), channel="pickup", store_id="10001", as_of=NOW,
        extra_care_ready=True, redemptions=[rule("bonus", kind="membership_bonus", amount="10.00", partial_redemption=False)])
    assert result["rules"][0]["reason"] == "online_bonus_minimum_not_met"
    assert result["totals"]["cash_due_before_tax"] == "8.00"


def test_reward_spend_basis_changes_qualification_and_no_98_percent():
    before = rule("earn", kind="extrabucks", amount="5.00", minimum_spend="10.00",
        progress_verified=True, prior_spend="0.00", prior_quantity=0)
    assert evaluate(lines("4.90"), reward_offers=[before])["totals"]["new_rewards_earned"] == "0.00"
    assert evaluate(coupons=[rule()], reward_offers=[before])["totals"]["new_rewards_earned"] == "5.00"
    after = dict(before, qualification_basis="after_coupons")
    assert evaluate(coupons=[rule()], reward_offers=[after])["totals"]["new_rewards_earned"] == "0.00"
    progress = dict(before, prior_spend="2.00")
    assert evaluate(lines("4.00"), reward_offers=[progress])["totals"]["new_rewards_earned"] == "5.00"


def test_unknown_reward_preserves_cash_but_not_economic_total():
    result = evaluate(reward_offers=[rule("earn", kind="extrabucks", amount="5.00")])
    assert result["totals"]["cash_due_before_tax"] == "11.98"
    assert result["totals"]["new_rewards_earned"] is None


def test_base_two_percent_net_spend_cap_and_duplicate_program():
    base = rule("base", kind="percent_back", value="2.00", max_reward="5.00", minimum_spend="0.50",
        progress_verified=True, prior_spend="0.00", prior_quantity=0, qualification_basis="after_redemption")
    assert evaluate(coupons=[rule()], reward_offers=[base])["totals"]["new_rewards_earned"] == "0.18"
    assert evaluate(lines("500.00"), reward_offers=[base])["totals"]["new_rewards_earned"] == "5.00"
    assert evaluate(lines("500.00"), reward_offers=[dict(base, max_reward="1.00")])["totals"]["new_rewards_earned"] == "1.00"
    with pytest.raises(ValueError):
        evaluate(reward_offers=[base, dict(base, id="duplicate-program")])


def test_invalid_identifiers_units_values_and_duplicate_rules():
    for rules in ([rule(), rule()], [rule(unit_ids=["missing:1"])],
                  [rule(value=3.5)], [rule(kind="percent_off", value="140.00")]):
        with pytest.raises((ValueError, TypeError)):
            evaluate(coupons=rules)


def test_cent_allocation_conserves_money_and_input_is_unchanged():
    data = lines("0.05", 3)
    coupon = rule(unit_ids=["paste:1", "paste:2", "paste:3"], value="0.02")
    saved = deepcopy((data, coupon))
    result = evaluate(data, coupons=[coupon])
    for unit in result["units"]:
        assert Decimal(unit["base"]) == Decimal(unit["coupon_discount"]) + Decimal(unit["reward_used"]) + Decimal(unit["remaining"])
        assert Decimal(unit["remaining"]) >= 0
    assert result["totals"]["cash_due_before_tax"] == "0.13"
    assert (data, coupon) == saved


def test_online_price_works_for_pickup_but_not_in_store():
    data = lines()
    data[0]["price_evidence"] = []
    pickup = resolve_prices(data, channel="pickup", store_id="10001", as_of=NOW)
    assert pickup["status"] == "resolved"
    assert pickup["lines"][0]["price"]["source"] == "cvs_rendered_page"
    assert resolve_prices(data, channel="in_store", store_id="10001", as_of=NOW)["status"] == "needs_input"
    assert evaluate(data)["totals"] is None


@pytest.mark.parametrize("changes,reason", [
    ({"store_id": "999"}, "price_store_mismatch"),
    ({"sku_id": "999"}, "price_sku_mismatch"),
    ({"channel": "online"}, "price_channel_mismatch"),
    ({"observed_at": "2026-10-08T12:00:00-05:00"}, "price_stale_or_future"),
    ({"observed_at": "2026-10-11T12:00:00-05:00"}, "price_stale_or_future"),
    ({"verified_by_caller": False}, "price_evidence_needs_confirmation"),
    ({"source_ref": ""}, "price_source_reference_missing"),
    ({"currency": "CAD"}, "unit_price_unknown"),
])
def test_price_evidence_checks(changes, reason):
    data = lines()
    data[0]["price_evidence"][0].update(changes)
    result = resolve_prices(data, channel="in_store", store_id="10001", as_of=NOW)
    assert result["status"] == "needs_input"
    assert reason in result["lines"][0]["rejected_evidence"]


def test_conflicting_current_prices_unknown_sku_and_online_source_mismatch():
    data = lines()
    data[0]["price_evidence"].append(dict(data[0]["price_evidence"][0], amount="6.99"))
    result = resolve_prices(data, channel="in_store", store_id="10001", as_of=NOW)
    assert "conflicting_current_prices" in result["lines"][0]["unresolved"]
    data = lines()
    data[0]["product"]["selected_sku_verified"] = False
    assert resolve_prices(data, channel="in_store", store_id="10001", as_of=NOW)["status"] == "needs_input"
    data = lines()
    data[0]["product"]["price"]["source_url"] = URL.replace("456", "999")
    assert resolve_prices(data, channel="pickup", store_id="10001", as_of=NOW)["status"] == "needs_input"


def test_ad_price_needs_unit_price_exact_membership_store_card_and_validity():
    data = lines()
    e = data[0]["price_evidence"][0]
    e.update(source="weekly_ad", price_kind="unit", sku_membership_verified=True,
        store_applicability_verified=True, card_conditions_met=True,
        valid_from="2026-10-04T00:00:00-05:00", valid_to="2026-10-11T23:59:59-05:00")
    assert resolve_prices(data, channel="in_store", store_id="10001", as_of=NOW)["status"] == "resolved"
    for changes in ({"sku_membership_verified": False}, {"store_applicability_verified": False},
                    {"card_conditions_met": False}, {"price_kind": "multibuy"}, {"valid_to": None}):
        bad = deepcopy(data)
        bad[0]["price_evidence"][0].update(changes)
        assert resolve_prices(bad, channel="in_store", store_id="10001", as_of=NOW)["status"] == "needs_input"


def test_cli_documented_fixture_runs_without_chrome(capsys):
    import json
    from pathlib import Path
    from dealforge.cvs import main
    path = Path(__file__).parent / "fixtures" / "cvs_basket_example.json"
    assert main(["evaluate", "--input", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["totals"]["cash_due_before_tax"] == "4.98"
    assert result["totals"]["new_rewards_earned"] == "5.00"


def test_automatic_public_reward_and_unknown_card_readiness():
    earn = rule("earn", state="automatic", kind="extrabucks", amount="5.00",
        progress_verified=True, prior_spend="0.00", prior_quantity=0)
    assert evaluate(reward_offers=[earn])["totals"]["new_rewards_earned"] == "5.00"
    blocked = evaluate_basket(lines(), channel="in_store", store_id="10001", as_of=NOW,
                             coupons=[rule()])
    assert blocked["rules"][0]["reason"] == "qualifying_card_required"
    assert blocked["totals"]["cash_due_before_tax"] is None
