from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from dealforge.providers.cvs.matching import match_products_to_offers

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
PRODUCT = {"product_id": "100", "sku_id": "200", "name": "Example soap, 4 oz",
           "url": "https://www.cvs.com/shop/example-soap-prodid-100?skuId=200"}
RULE = {"offer_id": "cvs-" + "a" * 24, "description_text": "SAVE $2 on any Example soap",
        "eligible_products_observed": [PRODUCT], "eligibility_observed_at": NOW.isoformat()}


def test_exact_fresh_sku_membership_is_distinct_from_checkout_eligibility():
    result = match_products_to_offers([PRODUCT], [RULE], as_of=NOW)
    match = result["matches"][0]
    assert match["kind"] == "observed_sku_membership" and match["sku_membership_observed"]
    assert not match["checkout_eligibility_verified"]
    assert "quantity_and_spend" in match["remaining_checks"]
    assert "savings" not in match and "cash_due" not in result


@pytest.mark.parametrize("offset", [timedelta(hours=25), timedelta(seconds=-1)])
def test_stale_or_future_membership_requires_recheck(offset):
    match = match_products_to_offers([PRODUCT], [RULE], as_of=NOW + offset)["matches"][0]
    assert match["kind"] == "sku_membership_needs_recheck"
    assert not match["sku_membership_observed"]


def test_other_variant_is_only_a_family_candidate():
    product = {**PRODUCT, "sku_id": "201", "url": PRODUCT["url"].replace("skuId=200", "skuId=201")}
    match = match_products_to_offers([product], [RULE], as_of=NOW)["matches"][0]
    assert match["kind"] == "product_family_candidate" and not match["sku_membership_observed"]


def test_variant_mismatch_and_missing_timestamps_cannot_prove_membership():
    product = {**PRODUCT, "match": {"constraint_gaps": ["selected_sku_mismatch"]}}
    assert not match_products_to_offers([product], [RULE], as_of=NOW)["matches"][0]["sku_membership_observed"]
    rule = {**RULE, "eligibility_observed_at": None}
    assert not match_products_to_offers([PRODUCT], [rule], as_of=NOW)["matches"][0]["sku_membership_observed"]


def test_text_overlap_is_only_a_candidate_and_missing_products_do_not_prove_exclusion():
    rule = {**RULE, "eligible_products_observed": []}
    result = match_products_to_offers([PRODUCT], [rule], as_of=NOW)
    assert result["matches"][0]["kind"] == "text_candidate"
    unrelated = {**PRODUCT, "name": "Unrelated batteries"}
    result = match_products_to_offers([unrelated], [rule], as_of=NOW)
    assert result["matches"] == []
    assert "missing_from_partial_carousel_does_not_prove_ineligibility" in result["review"]


def test_matching_output_is_allowlisted_and_input_is_not_modified():
    product = {**PRODUCT, "cookie": "SECRET"}
    before = deepcopy(product)
    result = match_products_to_offers([product], [{**RULE, "private": "SECRET"}], as_of=NOW)
    assert "SECRET" not in str(result) and product == before
    with pytest.raises(ValueError):
        match_products_to_offers([{**PRODUCT, "sku_id": "999"}], [RULE], as_of=NOW)
    with pytest.raises(ValueError):
        match_products_to_offers([PRODUCT] * 26, [RULE], as_of=NOW)
