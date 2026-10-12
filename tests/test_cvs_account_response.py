"""Synthetic source-shaped cases; no personal response is stored in this repo."""

from copy import deepcopy
from datetime import datetime
import json

import pytest

from dealforge.providers.cvs.account_response import normalize_profile_response


NOW = datetime.fromisoformat("2026-10-10T12:00:00-05:00")


@pytest.fixture
def response():
    reward = {
        "cmpgnId": 100, "cpnNbr": 200, "cpnSeqNbr": 12345678901,
        "cpnDsc": "ExtraCare Plus $10 bonus reward", "maxRedeemAmt": "10.00",
        "expirDt": "2026-10-16", "viewableInd": "Y", "cpnSuppressCd": "N",
        "loadableInd": "N", "redeemableInd": "Y", "digitizedCpnInd": "N",
        "redeemEligibleChannelCd": "B", "mfrInd": "N",
    }
    coupon = {
        "cmpgnId": 300, "cpnNbr": 400, "cpnSeqNbr": 22345678901,
        "cpnDsc": "$3 off $15 Example Brand soap", "maxRedeemAmt": "3.00",
        "expirDt": "2026-10-17", "viewableInd": "Y",
        "loadableInd": "Y", "redeemableInd": "N", "mfrInd": "N",
    }
    return {"statusCode": "0000", "data": {"getEcCustomerProfile": {
        "errorCd": None,
        "couponCategories": {"extrabucks": [reward], "personalizedDeals": [coupon], "otherDeals": []},
        "xtraCard": {"xtraCardNbr": "999999999999999", "xtraCardCipherTxt": "SECRET-CIPHER"},
        "xtraCarePrefs": {"phr": {"enrolled": "PRIVATE-PREFERENCE"}},
        "xtraCare": {"cpns": [reward, coupon], "extraBuckRewardsSummary": {"availableExtraBucks": 10.0}},
    }}, "email": "private@example.test", "token": "SECRET-TOKEN"}


def normalize(response, **kwargs):
    return normalize_profile_response(response, account_ref="cvs-test", observed_at=NOW, **kwargs)


def categories(response):
    return response["data"]["getEcCustomerProfile"]["couponCategories"]


def test_reconciles_visible_collection_without_claiming_full_terms(response):
    hidden = deepcopy(categories(response)["personalizedDeals"][0])
    hidden.update(cpnNbr=401, viewableInd="N", loadableInd="N")
    categories(response)["personalizedDeals"].append(hidden)
    draft = normalize(response, reported_count=2)
    assert draft.collection["source_records"] == 3
    assert draft.collection["hidden_records"] == 1
    assert draft.collection["collected_records"] == 2
    assert draft.snapshot.rewards_complete and draft.snapshot.coupons_complete
    assert draft.to_dict()["status"] == "partial"
    assert draft.to_dict()["terms_complete"] is False


def test_sensitive_fields_raw_lists_and_redeemable_identities_are_not_returned(response):
    output = json.dumps(normalize(response, reported_count=2).to_dict())
    for private in ("999999999999999", "SECRET-CIPHER", "PRIVATE-PREFERENCE", "private@example.test",
                    "SECRET-TOKEN", "12345678901", "22345678901", "xtraCard", "xtraCarePrefs"):
        assert private not in output
    assert len(normalize(response).snapshot.rewards) == 1  # ignores redundant xtraCare.cpns


def test_certificate_identity_distinguishes_two_issues_of_same_reward(response):
    second = deepcopy(categories(response)["extrabucks"][0])
    second.update(cpnSeqNbr=12345678902, maxRedeemAmt="0.50", cpnDsc="$0.50 ExtraBucks Reward")
    categories(response)["extrabucks"].append(second)
    draft = normalize(response, reported_count=3)
    assert len({r.reward_id for r in draft.snapshot.rewards}) == 2


def test_coupon_identity_survives_activation_and_is_account_scoped(response):
    original = normalize(response).snapshot.coupons[0]
    categories(response)["personalizedDeals"][0].update(cpnSeqNbr=22345678902, loadableInd="N", redeemableInd="Y")
    activated = normalize(response).snapshot.coupons[0]
    assert original.coupon_id == activated.coupon_id
    assert original.state.value == "available"
    assert activated.state.value == "on_card"
    other = normalize_profile_response(response, account_ref="another-account", observed_at=NOW)
    assert other.snapshot.coupons[0].coupon_id != original.coupon_id


def test_definition_does_not_override_account_amount_expiry_or_activation(response):
    definition = {"cmpgnId": 100, "cpnNbr": 200, "maxRedeemAmt": 999,
                  "loadableInd": "Y", "activeInd": "false", "expirDt": "2099-01-01",
                  "cpnTermsTxt": "Example terms: qualifying products only."}
    draft = normalize(response, definitions=[definition], reported_count=2)
    reward = draft.snapshot.rewards[0]
    assert str(reward.amount) == "10.00"
    assert reward.state.value == "on_card"
    assert reward.expires_on.isoformat() == "2026-10-16"
    assert "qualifying products only" in reward.raw_terms
    assert draft.definitions_observed == (reward.reward_id,)
    assert reward.partial_redemption is None
    assert reward.channel.value == "unknown"
    assert draft.snapshot.coupons[0].issuer is None


def test_wrong_campaign_definition_cannot_supply_terms(response):
    draft = normalize(response, definitions=[{"cmpgnId": 999, "cpnNbr": 200, "cpnTermsTxt": "WRONG TERMS"}])
    assert "WRONG TERMS" not in draft.snapshot.rewards[0].raw_terms
    assert not draft.definitions_observed


def test_conflicting_definitions_are_not_arbitrarily_selected(response):
    drafts = [{"cmpgnId": 100, "cpnNbr": 200, "cpnTermsTxt": text} for text in ("First terms", "Other terms")]
    result = normalize(response, definitions=drafts)
    assert not result.definitions_observed
    assert "First terms" not in result.snapshot.rewards[0].raw_terms


def test_count_mismatch_and_unknown_categories_cannot_claim_complete(response):
    assert not normalize(response, reported_count=99).snapshot.coupons_complete
    assert not normalize(response).snapshot.rewards_complete
    categories(response)["newCategory"] = []
    result = normalize(response, reported_count=2)
    assert not result.snapshot.rewards_complete
    assert "missing_or_changed_category" in result.review


def test_conflicting_certificate_is_quarantined_instead_of_counting_first_value(response):
    duplicate = deepcopy(categories(response)["extrabucks"][0])
    duplicate["maxRedeemAmt"] = "20.00"
    categories(response)["extrabucks"].extend([duplicate, deepcopy(duplicate)])
    result = normalize(response, reported_count=2)
    assert not result.snapshot.rewards
    assert not result.snapshot.rewards_complete
    assert "conflicting_source_identity" in result.review


@pytest.mark.parametrize("bad_amount", [10.0, "NaN", "-1.00", "0.001"])
def test_invalid_source_money_does_not_reach_wallet_totals(response, bad_amount):
    categories(response)["extrabucks"][0]["maxRedeemAmt"] = bad_amount
    draft = normalize(response, reported_count=2)
    assert not draft.snapshot.rewards
    assert draft.collection["rejected_records"] == 1
    assert not draft.snapshot.rewards_complete


def test_missing_amount_stays_unknown(response):
    del categories(response)["extrabucks"][0]["maxRedeemAmt"]
    assert normalize(response).snapshot.rewards[0].amount is None


def test_missing_identity_is_not_replaced_with_array_position(response):
    del categories(response)["extrabucks"][0]["cpnSeqNbr"]
    result = normalize(response, reported_count=2)
    assert not result.snapshot.rewards
    assert result.collection["rejected_records"] == 1


def test_source_error_is_reported_without_echoing_account_data():
    with pytest.raises(ValueError, match="did not report success") as error:
        normalize({"statusCode": "9999", "statusDescription": "SECRET-SERVER-MESSAGE"})
    assert "SECRET" not in str(error.value)
