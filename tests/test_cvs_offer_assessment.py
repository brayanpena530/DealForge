"""Offer evidence must narrow, never manufacture, checkout eligibility."""

from copy import deepcopy
from datetime import datetime

import pytest

from dealforge.providers.cvs.eligibility import extract_requirements, extract_term_candidates
from dealforge.providers.cvs.offer_assessment import assess_offers
from dealforge.providers.cvs.purchase_rules import prepare_purchase
from test_cvs_checkout import lines, NOW
from test_cvs_optimizer import account, coupon, confirmations

KEY = "cvs-" + "a" * 24


def sample():
    data = lines()
    data[0]["product"]["name"] = "Example soap, 4 oz"
    sync = account(coupons=[coupon(KEY, "$3 off any one Example soap")])
    sync["snapshot"]["coupons"][0]["issuer"] = None
    candidate = extract_requirements(KEY, "$3 off any one Example soap")
    candidate.update(description_text="Example soap", definition_observed=True,
        eligible_products_observed=[data[0]["product"]], eligibility_observed_at=NOW,
        term_candidates=extract_term_candidates("Manufacturer coupon. Valid in-store only. Limit one coupon per purchase. Excludes trial/travel sizes."))
    sync["offer_rules"] = [candidate]
    return data, sync


@pytest.mark.parametrize("text,fields", [
    ("Manufacturer's coupon. Valid in-store only. Limit one (1) coupon per purchase.",
     {"issuer": "mfr_coupon", "channels": ["in_store"], "per_purchase_limit": 1}),
    ("CVS coupon\nValid online only", {"issuer": "store_coupon", "channels": ["online", "pickup"]}),
    ("Valid in-store and online. Qualifying spend calculated after coupons.",
     {"channels": ["in_store", "online", "pickup"], "qualification_basis": "after_coupons"}),
    ("Minimum purchase before coupons. Limit two coupons per transaction.",
     {"qualification_basis": "pre_coupon", "per_purchase_limit": 2}),
    ("Not valid in-store only. Not a manufacturer coupon. Limit one (4) coupon per purchase.", {}),
])
def test_literal_term_fields_and_negations(text, fields):
    result = extract_term_candidates(text)
    assert result["fields"] == fields
    assert not result["checkout_eligibility_verified"]
    assert "remaining_uses" not in result["fields"]


def test_term_conflicts_and_restrictions_remain_visible():
    terms = extract_term_candidates("Valid in-store only. Valid online only. Excludes travel sizes. Cannot be combined with another store coupon.")
    assert terms["conflicts"] == ["channels"]
    assert "channels" not in terms["fields"]
    assert terms["restrictions_text"] == ["Excludes travel sizes", "Cannot be combined with another store coupon"]


@pytest.mark.parametrize("text,spend", [("$3 off when you spend $15", "15.00"), ("$3 off with a purchase of $20.50", "20.50"), ("$3 off when you spend $15.001", None)])
def test_explicit_spend_phrases(text, spend):
    assert extract_requirements(KEY, text)["minimum_spend"] == spend


def test_maximum_discount_does_not_become_fixed_coupon_value():
    assert extract_requirements(KEY, "Save up to $3 off") ["discount"]["value"] is None


def test_exact_membership_suggests_units_but_leaves_terms_and_remaining_uses_pending():
    data, sync = sample()
    result = assess_offers(data, account_sync=sync, account_ref="test-account", store_id="10001", as_of=NOW, allow_fixture=True)
    offer = result["offers"][0]
    assert offer["suggested_unit_ids"] == ["paste:1", "paste:2"]
    assert offer["eligible_quantity_observed"] == 2
    assert offer["eligible_spend_before_coupons"] == "11.98"
    assert "remaining_uses_unknown" in offer["remaining_checks"]
    assert "stacking_verified_required" in offer["remaining_checks"]
    assert offer["source_fingerprint"] and not offer["checkout_eligibility_verified"]


def test_stale_membership_and_family_matches_cannot_supply_unit_suggestions():
    data, sync = sample()
    sync["offer_rules"][0]["eligibility_observed_at"] = "2026-10-08T12:00:00-05:00"
    result = assess_offers(data, account_sync=sync, account_ref="test-account", store_id="10001", as_of=NOW, allow_fixture=True)
    assert not result["offers"][0]["suggested_unit_ids"]
    assert result["offers"][0]["matches"][0]["kind"] == "sku_membership_needs_recheck"


def test_explicit_product_exclusion_blocks_attestation_and_unit_suggestion():
    data, sync = sample()
    data[0]["product"]["name"] = "Example soap Travel Size"
    attested = confirmations(sync, data, {KEY: {"issuer": "mfr_coupon"}})
    result = assess_offers(data, account_sync=sync, confirmations=attested,
        account_ref="test-account", store_id="10001", as_of=NOW, allow_fixture=True)
    assert result["offers"][0]["excluded_unit_ids"] == ["paste:1", "paste:2"]
    assert not result["offers"][0]["suggested_unit_ids"]
    assert "observed_product_exclusion_conflict" in result["preparation"]["pending"][0]["checks"]
    assert not result["preparation"]["coupons"][0]["terms_verified"]


@pytest.mark.parametrize("field,value", [("issuer", "store_coupon"), ("channels", ["online", "pickup"]), ("qualification_basis", "pre_coupon")])
def test_confirmation_cannot_override_literal_terms(field, value):
    data, sync = sample()
    sync["offer_rules"][0]["term_candidates"]["fields"]["qualification_basis"] = "after_coupons"
    attested = confirmations(sync, data, {KEY: {"issuer": "mfr_coupon", "qualification_basis": "after_coupons", field: value}})
    result = prepare_purchase(data, account_sync=sync, confirmations=attested,
        account_ref="test-account", store_id="10001", as_of=NOW, allow_fixture=True)
    assert "confirmation_observed_" + field + "_conflict" in result["pending"][0]["checks"]


def test_membership_or_term_change_invalidates_old_confirmation():
    data, sync = sample()
    attested = confirmations(sync, data, {KEY: {"issuer": "mfr_coupon"}})
    sync["offer_rules"][0]["eligible_products_observed"] = []
    result = prepare_purchase(data, account_sync=sync, confirmations=attested,
        account_ref="test-account", store_id="10001", as_of=NOW, allow_fixture=True)
    assert "fresh_record_bound_confirmation_required" in result["pending"][0]["checks"]


def test_stacking_prohibition_cannot_be_bypassed_by_verification_flag():
    data, sync = sample()
    sync["offer_rules"][0]["term_candidates"] = extract_term_candidates("Cannot be combined with another coupon.")
    attested = confirmations(sync, data)
    result = prepare_purchase(data, account_sync=sync, confirmations=attested,
        account_ref="test-account", store_id="10001", as_of=NOW, allow_fixture=True)
    assert "observed_stacking_restriction_not_modeled" in result["pending"][0]["checks"]


def test_trial_exclusion_does_not_accidentally_exclude_travel_items():
    data, sync = sample()
    data[0]["product"]["name"] = "Example soap Travel Size"
    sync["offer_rules"][0]["term_candidates"] = extract_term_candidates("Excludes trial sizes.")
    result = assess_offers(data, account_sync=sync, account_ref="test-account", store_id="10001", as_of=NOW, allow_fixture=True)
    assert not result["offers"][0]["excluded_unit_ids"]


def test_whole_purchase_coupon_remains_a_candidate_without_text_overlap():
    data, sync = sample()
    rule = sync["offer_rules"][0]
    rule.update(description_text="$5 off $30 your purchase", eligible_products_observed=[])
    result = assess_offers(data, account_sync=sync, account_ref="test-account", store_id="10001", as_of=NOW, allow_fixture=True)
    assert result["selected_coupon_ids"] == [KEY]
    assert not result["offers"][0]["suggested_unit_ids"]


def test_explicit_channel_exclusion_blocks_attestation_without_broadening_conditional_prose():
    assert extract_term_candidates("Not valid in-store on trial sizes.")["fields"] == {}
    data, sync = sample()
    sync["offer_rules"][0]["term_candidates"] = extract_term_candidates("Not valid in-store.")
    result = prepare_purchase(data, account_sync=sync, confirmations=confirmations(sync, data),
        account_ref="test-account", store_id="10001", as_of=NOW, allow_fixture=True)
    assert "observed_channel_excluded" in result["pending"][0]["checks"]


def test_relevance_selection_is_bounded_and_truncation_disclosed_without_mutating_inputs():
    data, sync = sample()
    sync["offer_rules"] = [dict(sync["offer_rules"][0], offer_id="cvs-" + f"{i:024x}") for i in range(30)]
    sync["snapshot"]["coupons"] = [dict(coupon(r["offer_id"])) for r in sync["offer_rules"]]
    before = deepcopy(sync)
    result = assess_offers(data, account_sync=sync, account_ref="test-account", store_id="10001", as_of=NOW, allow_fixture=True)
    assert len(result["offers"]) == 25
    assert result["coverage"]["relevant_rule_count"] == 30 and not result["coverage"]["selection_complete"]
    assert sync == before
