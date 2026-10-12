"""Synthetic account → bound terms → prices → selection → wallet transitions."""

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from dealforge.providers.cvs.optimizer import optimize_purchase
from dealforge.providers.cvs.purchase_rules import prepare_purchase, expiration
from test_cvs_checkout import lines, rule, NOW


def account(rewards=None, coupons=None):
    return {"status": "partial", "account_identity_verified": True,
        "account_binding": {"status": "matched"}, "offer_rules": [],
        "snapshot": {"schema_version": 1, "account_ref": "test-account", "observed_at": NOW,
        "source": "fixture", "status": "partial", "rewards_complete": True, "coupons_complete": True,
        "rewards": rewards or [], "coupons": coupons or []}}


def certificate(key="old", amount="5.00", expiry="2026-11-30", partial=False, kind="extrabucks"):
    return {"reward_id": key, "kind": kind, "amount": amount, "expires_on": expiry,
        "state": "on_card", "channel": "in_store", "partial_redemption": partial,
        "raw_terms": "Synthetic qualifying-items certificate terms"}


def coupon(key="coupon", headline="$3 off toothpaste", state="on_card"):
    return {"coupon_id": key, "headline": headline, "issuer": "store_coupon", "expires_on": "2026-11-30",
        "state": state, "channel": "in_store", "raw_terms": "Synthetic full coupon terms"}


def options(sync, data=None, confirmations=None, **kwargs):
    return dict(lines=data or lines(), account_sync=sync, account_ref="test-account",
        confirmations=confirmations, channel="in_store", store_id="10001", as_of=NOW,
        allow_fixture=True, extra_care_ready=True, **kwargs)


def confirmations(sync, data=None, overrides=None):
    data = data or lines()
    keys = [r["reward_id"] for r in sync["snapshot"]["rewards"]] + [c["coupon_id"] for c in sync["snapshot"]["coupons"]]
    draft = prepare_purchase(data, account_sync=sync, account_ref="test-account", channel="in_store", store_id="10001",
        as_of=NOW, allow_fixture=True, confirmations=[{"id": key} for key in keys])
    fingerprints = {p["id"]: p["source_fingerprint"] for p in draft["pending"]}
    return [{"id": key, "source_fingerprint": fingerprints[key], "observed_at": NOW,
        "source_ref": "synthetic observed complete terms", "unit_ids": [f'{l["line_id"]}:{i}' for l in data for i in range(1,l["quantity"]+1)],
        "terms_verified": True, "product_eligibility_verified": True, "limits_verified": True,
        "stacking_verified": True, "minimum_quantity": 1, "minimum_spend": "0.00",
        "qualification_basis": "pre_coupon", "remaining_uses": 1,
        **(dict(kind="amount_off", value="3.00", issuer="store_coupon") if key in {c["coupon_id"] for c in sync["snapshot"]["coupons"]} else {}),
        **(overrides or {}).get(key, {})} for key in keys]


def optimize(sync, data=None, overrides=None, **kwargs):
    return optimize_purchase(**options(sync, data, confirmations(sync, data, overrides), **kwargs))


def test_expiring_credit_wins_over_fresh_credit_despite_overage():
    data = lines("3.00")
    sync = account([certificate("expiring", "10.00", "2026-10-11", kind="membership_bonus"), certificate("fresh", "5.00")])
    result = optimize(sync, data)
    best = result["recommended"]
    assert best["redemption_order"] == ["expiring"]
    assert best["totals"]["cash_due_before_tax"] == "0.00"
    assert best["totals"]["modeled_reward_value_forfeited"] == "4.00"
    assert next(w for w in best["ending_wallet"] if w["id"] == "fresh")["amount"] == "5.00"
    assert best["scenarios"][0]["economic_cost_before_tax"] == "0.00"
    assert result["search"]["complete"] and result["synthetic_data"]


def test_fresh_large_certificate_loses_to_smaller_fit():
    result = optimize(account([certificate("early-large", "10.00", "2026-11-10"), certificate("later-small", "5.00")]), lines("3.00"))
    assert result["recommended"]["redemption_order"] == ["later-small"]
    assert result["recommended"]["totals"]["cash_due_before_tax"] == "1.00"
    assert result["lowest_cash"]["totals"]["cash_due_before_tax"] == "0.00"
    assert result["recommended"]["totals"]["modeled_reward_value_forfeited"] == "0.00"


def test_return_date_sensitivity_and_partial_certificate_remaining():
    sync = account([certificate("sensitive", "10.00", "2026-10-30")])
    result = optimize(sync, lines("3.00"))
    assert result["sensitive_to_return_date"]
    assert result["scenario_recommendations"][0]["redemption_order"] == []
    assert result["scenario_recommendations"][1]["redemption_order"] == ["sensitive"]
    partial = optimize(account([certificate(partial=True)]), lines("1.00"))
    assert partial["recommended"]["ending_wallet"][0]["amount"] == "3.00"
    assert partial["recommended"]["totals"]["modeled_reward_value_forfeited"] == "0.00"


def test_percent_coupon_can_make_preserving_credit_better():
    sync = account([certificate(amount="10.00")], [coupon()])
    result = optimize(sync, lines("10.00"), overrides={"coupon": {"kind": "percent_off", "value": "20.00"}})
    assert result["recommended"]["redemption_order"] == []
    assert result["recommended"]["totals"]["cash_due_before_tax"] == "16.00"
    assert result["lowest_cash"]["totals"]["cash_due_before_tax"] == "8.00"


def test_cash_budget_can_change_recommendation_and_infeasible_is_explicit():
    sync = account([certificate(amount="10.00")])
    result = optimize(sync, lines("3.00"), profile={"cash_budget": "0.00"})
    assert result["recommended"]["totals"]["cash_due_before_tax"] == "0.00"
    infeasible = optimize(account(), profile={"cash_budget": "1.00"})
    assert infeasible["status"] == "no_feasible_plan" and infeasible["recommended"] is None
    assert infeasible["baseline"]["within_cash_budget"] is False


def test_new_credit_requires_own_expiration_and_shared_future_budget():
    sync = account([certificate(amount="10.00")])
    award = rule("new", kind="extrabucks", amount="5.00", state="automatic", campaign_id="campaign-one",
        progress_verified=True, prior_spend="0.00", prior_quantity=0,
        earned_terms_verified=True, earned_valid_from=NOW, earned_valid_to="2026-11-30T23:59:59-06:00",
        earned_channels=["in_store"])
    result = optimize(sync, reward_offers=[award], profile={"future_eligible_spend": "5.00"})
    for plan in (result["recommended"], result["lowest_cash"], result["baseline"]):
        assert all(Decimal(s["ending_usable_value"]) <= 5 for s in plan["scenarios"])
    unknown = dict(award, earned_valid_to=None)
    result = optimize(account(), reward_offers=[unknown])
    assert result["status"] == "partial"
    assert result["recommended"]["unknown_new_reward_terms"] == ["new"]
    assert result["recommended"]["scenarios"][0]["ending_usable_value"] == "0.00"
    assert result["recommended"]["totals"]["cash_due_before_tax"] == "11.98"


def test_coupon_choice_and_manufacturer_overlap():
    sync = account(coupons=[dict(coupon("small"),issuer="mfr_coupon"), dict(coupon("large"),issuer="mfr_coupon")])
    result = optimize(sync, overrides={"small": {"issuer": "mfr_coupon", "value": "2.00"},
                                      "large": {"issuer": "mfr_coupon", "value": "4.00"}})
    assert set(result["recommended"]["coupon_order"]) == {"small", "large"}
    assert result["recommended"]["totals"]["cash_due_before_tax"] == "5.98"
    assignments = result["recommended"]["coupon_assignments"]
    assert set(assignments[0]["unit_ids"]).isdisjoint(assignments[1]["unit_ids"])


def test_rule_subset_and_redemption_order_search_is_complete_and_bounded():
    sync = account([certificate("one"),certificate("two")],[coupon()])
    result = optimize(sync)
    assert result["search"]["possible_ordered_selections"] == 10
    assert result["search"]["complete"] and result["search"]["explored"] == 10
    limited = optimize(sync, max_candidates=4)
    assert not limited["search"]["complete"] and limited["search"]["explored"] == 4
    assert limited["baseline"] is not None and limited["no_credit"] is not None


@pytest.mark.parametrize("mutation,reason", [
    (lambda s:s["snapshot"].update(observed_at="2026-10-08T12:00:00-05:00"), "account_stale_or_access_unresolved"),
    (lambda s:s["snapshot"].update(account_ref="different"), "account_reference_mismatch"),
    (lambda s:s.update(status="needs_login"), "refresh_signed_in_account"),
])
def test_stale_wrong_or_unavailable_account_is_blocked(mutation,reason):
    sync = account()
    mutation(sync)
    result = optimize_purchase(**options(sync))
    assert result["status"] == "needs_input" and result["preparation"]["reason"] == reason


def test_real_source_requires_continuity_fixture_requires_opt_in():
    sync = account()
    params = options(sync)
    params["allow_fixture"] = False
    assert optimize_purchase(**params)["preparation"]["reason"] == "fixture_requires_explicit_simulation"
    sync["snapshot"]["source"] = "browser"
    sync["account_identity_verified"] = False
    assert optimize_purchase(**params)["preparation"]["reason"] == "account_continuity_not_matched"


def test_fingerprint_and_immutable_snapshot_prevent_stale_or_invented_credit():
    sync = account([certificate()])
    facts = confirmations(sync)
    sync["snapshot"]["rewards"][0]["amount"] = "2.00"
    result = optimize_purchase(**options(sync,confirmations=facts))
    assert result["status"] == "partial" and result["recommended"]["redemption_order"] == []
    facts = confirmations(sync)
    facts[0]["amount"] = "100.00"
    prepared = prepare_purchase(**{k:v for k,v in options(sync,confirmations=facts).items() if k!="extra_care_ready"})
    assert "confirmation_cannot_override_amount" in prepared["pending"][0]["checks"]
    assert prepared["redemptions"][0]["amount"] == "2.00"


def test_snapshot_activation_and_unknown_terms_are_not_overwritten():
    sync = account(coupons=[coupon(state="available")])
    result = optimize(sync, overrides={"coupon": {"state":"on_card"}})
    assert result["recommended"]["coupon_order"] == []
    assert result["status"] == "partial"
    sync = account([certificate(partial=None)])
    result = optimize(sync)
    assert result["recommended"]["redemption_order"] == []
    confirmed = optimize(sync, overrides={"old":{"partial_redemption_verified":True,"partial_redemption":False}})
    assert confirmed["status"] == "conditional_estimate"


def test_candidate_value_conflict_and_duplicate_campaigns_are_rejected():
    sync = account(coupons=[coupon()])
    sync["offer_rules"] = [{"offer_id":"coupon","discount":{"kind":"amount_off","value":"3.00"},"conflicts":[]}]
    result = optimize(sync, overrides={"coupon":{"value":"10.00"}})
    assert result["recommended"]["coupon_order"] == []
    award = rule("a",campaign_id="same",kind="extrabucks",amount="5.00")
    with pytest.raises(ValueError):
        optimize(account(),reward_offers=[award,dict(award,id="b")])


def test_exact_price_missing_and_no_extras_or_input_mutation():
    sync, data = account([certificate()]), lines()
    saved = deepcopy((sync,data))
    result = optimize(sync,data)
    assert result["assumptions"]["checkouts"] == 1
    assert (sync,data) == saved
    data[0]["price_evidence"] = []
    assert optimize(sync,data)["recommended"] is None


def test_expiry_cutoff_uses_actual_dst_and_online_eastern_zone():
    assert expiration("2026-11-10","in_store","America/Chicago").endswith("-06:00")
    assert expiration("2026-10-10","online","America/Chicago").endswith("-04:00")
    result = optimize(account(),profile={"return_days":[28]})
    assert result["recommended"]["scenarios"][0]["return_at"].endswith("-06:00")


def test_equal_cost_prefers_earlier_expiry_and_risk_report():
    sync = account([certificate("later","5.00","2026-12-30"),certificate("earlier","5.00","2026-11-30")])
    assert optimize(sync,lines("2.50"))["recommended"]["redemption_order"] == ["earlier"]
    risk = optimize(account([certificate(expiry="2026-10-20")]))
    assert risk["at_risk_rewards"][0]["expires_before_first_return"]


def test_item_assignment_filter_checks_all_alternatives_and_reports_cap():
    data = lines("10.00",1,sale_status="sale")+[dict(lines("10.00",1)[0],line_id="regular")]
    sync = account(coupons=[coupon()])
    best = optimize(sync,data,overrides={"coupon":{"kind":"percent_off","value":"20.00","scope":"item"}})
    assert best["recommended"]["coupon_assignments"] == [{"id":"coupon","unit_ids":["regular:1"]}]
    sync = account(coupons=[dict(coupon(),issuer="mfr_coupon")])
    bounded = optimize(sync,lines(quantity=30),overrides={"coupon":{"issuer":"mfr_coupon"}})
    assert not bounded["search"]["unit_assignments_complete"] and not bounded["search"]["complete"]


def test_confirmations_do_not_transfer_to_changed_skus_or_quantities():
    sync = account([certificate()])
    facts = confirmations(sync)
    data = lines(quantity=3)
    result = optimize_purchase(**options(sync,data,confirmations=facts))
    assert result["recommended"]["redemption_order"] == []
    assert result["status"] == "partial"


def test_unknown_earning_checks_and_cash_budget_never_create_free_money():
    award = rule("earn",campaign_id="campaign",kind="extrabucks",amount="50.00")
    result = optimize(account(),reward_offers=[award])
    assert result["status"] == "partial"
    assert result["recommended"]["totals"]["cash_due_before_tax"] == "11.98"
    assert result["recommended"]["totals"]["new_rewards_earned"] is None


def test_cadence_uses_shared_wallet_budget_without_double_counting():
    result = optimize(account([certificate("a","10.00"),certificate("b","10.00")]),
                      profile={"future_eligible_spend":"5.00"})
    assert result["recommended"]["redemption_order"]
    for scenario in result["recommended"]["scenarios"]:
        assert scenario["opening_usable_value"] == "5.00"
        assert Decimal(scenario["ending_usable_value"]) <= 5


def test_synthetic_browser_sync_flows_into_real_account_rule_path(tmp_path):
    import asyncio
    from dealforge.providers.cvs.account_sync import AccountCollector
    from dealforge.providers.cvs.account_state import AccountState
    from test_cvs_account_sync import Browser
    async def run():
        collector = AccountCollector(Browser(),poll_seconds=0,state=AccountState(tmp_path/"account-state"))
        await collector.sync(account_ref="test-account")
        synced = await collector.sync(account_ref="test-account")
        assert synced["account_identity_verified"] and synced["snapshot"]["source"] == "browser"
        timestamp = synced["snapshot"]["observed_at"]
        data = lines("3.00")
        data[0]["price_evidence"][0]["observed_at"] = timestamp
        data[0]["product"]["price"]["observed_at"] = timestamp
        params = dict(lines=data,account_sync=synced,account_ref="test-account",store_id="10001",as_of=timestamp)
        prepared = prepare_purchase(**params)
        key = prepared["redemptions"][0]["id"]
        c = {"id":key,"source_fingerprint":prepared["redemptions"][0]["source_fingerprint"],
            "observed_at":timestamp,"source_ref":"synthetic full reward terms","unit_ids":["paste:1","paste:2"],
            "channels":["in_store"],"partial_redemption_verified":True,"partial_redemption":False,
            "terms_verified":True,"product_eligibility_verified":True,"stacking_verified":True,"limits_verified":True,
            "minimum_quantity":1,"minimum_spend":"0.00","qualification_basis":"pre_coupon","remaining_uses":1}
        result = optimize_purchase(**params,confirmations=[c],extra_care_ready=True)
        assert result["lowest_cash"]["redemption_order"] == [key]
        assert result["lowest_cash"]["totals"]["cash_due_before_tax"] == "0.00"
        assert not result["execution_ready"]
    asyncio.run(run())


def test_documented_optimizer_cli_fixture_and_cent_conservation(capsys):
    import json
    from pathlib import Path
    from dealforge.cvs import main
    fixture = Path(__file__).parent/"fixtures"/"cvs_optimizer_example.json"
    assert main(["optimize","--input",str(fixture)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["recommended"]["redemption_order"] == ["example-expiring-bonus"]
    total = result["recommended"]["totals"]
    assert Decimal(total["item_subtotal"]) == Decimal(total["modeled_coupon_discounts"])+Decimal(total["modeled_rewards_spent"])+Decimal(total["cash_due_before_tax"])
    assert total["modeled_reward_face_value_consumed"] == "10.00"
    assert result["synthetic_data"]
