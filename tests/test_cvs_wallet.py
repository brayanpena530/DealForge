"""Account inventory boundaries that keep agents from claiming false savings."""

from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path

import pytest

from dealforge.cvs import main
from dealforge.providers.cvs.wallet import AccountSnapshot, parse_timestamp, summarize_wallet


@pytest.fixture
def account():
    data = json.loads((Path(__file__).parent / "fixtures/cvs_account_example.json").read_text())
    data["source"] = "manual"
    return data


NOW = parse_timestamp("2026-10-09T11:00:00-05:00")


def summary(data, **kwargs):
    return summarize_wallet(AccountSnapshot.from_dict(data), as_of=kwargs.pop("as_of", NOW), **kwargs)


def test_reward_types_are_separate_and_not_checkout_verified(account):
    bonus = deepcopy(account["rewards"][0])
    bonus.update(reward_id="bonus", kind="membership_bonus", amount="10.00")
    account["rewards"].append(bonus)
    result = summary(account)
    assert result["status"] == "ready"
    assert result["known_unexpired_face_value_by_kind"] == {"extrabucks": "5.00", "membership_bonus": "10.00"}
    assert result["balance_is_checkout_verified"] is False
    assert result["account"]["coupons"][0]["state"] == "available"


def test_expiry_uses_shopping_local_date_and_includes_today(account):
    account["rewards"][0]["expires_on"] = "2026-10-09"
    result = summary(account, as_of=parse_timestamp("2026-10-09T23:59:00-05:00"))
    assert result["known_unexpired_face_value_by_kind"] == {"extrabucks": "5.00"}
    assert result["account"]["rewards"][0]["expires_today"] is True
    account["observed_at"] = "2026-10-10T00:00:00-05:00"
    result = summary(account, as_of=parse_timestamp("2026-10-10T00:01:00-05:00"))
    assert result["known_unexpired_face_value_by_kind"] == {}
    assert result["account"]["rewards"][0]["expired"] is True


@pytest.mark.parametrize("state", ["redeemed", "unavailable", "unknown"])
def test_nonavailable_rewards_do_not_count(account, state):
    account["rewards"][0]["state"] = state
    result = summary(account)
    assert result["known_unexpired_face_value_by_kind"] == {}
    if state == "unknown":
        assert result["status"] == "partial"


@pytest.mark.parametrize("field", ["amount", "expires_on"])
def test_unknown_value_or_expiry_is_not_zero_or_usable(account, field):
    account["rewards"][0][field] = None
    result = summary(account)
    assert result["status"] == "partial"
    assert result["known_unexpired_face_value_by_kind"] == {}
    assert result["account"]["rewards"][0][field] is None


@pytest.mark.parametrize("status", ["needs_login", "blocked_access"])
def test_unresolved_access_keeps_status_and_hides_historical_totals(account, status):
    account["status"] = status
    result = summary(account)
    assert result["status"] == status
    assert result["known_unexpired_face_value_by_kind"] == {}


def test_stale_snapshot_cannot_advertise_current_balance(account):
    result = summary(account, max_age=timedelta(minutes=30))
    assert result["status"] == "stale"
    assert result["known_unexpired_face_value_by_kind"] == {}


def test_empty_incomplete_snapshot_is_not_a_complete_zero_wallet(account):
    account.update(rewards=[], coupons=[], rewards_complete=False)
    assert summary(account)["status"] == "partial"


def test_synthetic_example_cannot_appear_as_ready_personal_data(account):
    account["source"] = "fixture"
    result = summary(account)
    assert result["status"] == "partial"
    assert any("Synthetic" in warning for warning in result["warnings"])


@pytest.mark.parametrize("amount", [5.1, -1, "-1", "NaN", "Infinity", "1.001", "9" * 40])
def test_rejects_unsafe_money(account, amount):
    account["rewards"][0]["amount"] = amount
    with pytest.raises(ValueError, match="amount"):
        AccountSnapshot.from_dict(account)


def test_duplicate_certificates_cannot_double_count(account):
    account["rewards"].append(deepcopy(account["rewards"][0]))
    with pytest.raises(ValueError, match="duplicate reward_id"):
        AccountSnapshot.from_dict(account)


def test_rejects_credential_fields_without_echoing_values(account):
    account["password"] = "secret-should-not-be-echoed"
    with pytest.raises(ValueError) as error:
        AccountSnapshot.from_dict(account)
    assert "secret" not in str(error.value)


def test_rejects_naive_and_future_observation_times(account):
    account["observed_at"] = "2026-10-09T10:00:00"
    with pytest.raises(ValueError, match="UTC offset"):
        summary(account)
    account["observed_at"] = "2026-10-09T12:00:00-05:00"
    with pytest.raises(ValueError, match="after as_of"):
        summary(account)


def test_round_trip_retains_terms_unknowns_and_money(account):
    account["rewards"][0]["partial_redemption"] = None
    snapshot = AccountSnapshot.from_dict(account)
    assert AccountSnapshot.from_dict(snapshot.to_dict()) == snapshot


def test_cli_returns_agent_json_and_does_not_mutate_snapshot(account, tmp_path, capsys):
    path = tmp_path / "account.json"
    path.write_text(json.dumps(account), encoding="utf-8")
    before = path.read_bytes()
    assert main(["wallet", "--snapshot", str(path), "--as-of", NOW.isoformat()]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    assert path.read_bytes() == before


def test_cli_missing_snapshot_requests_input(capsys):
    assert main(["wallet"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "needs_input"


def test_cli_validation_failure_is_json(account, tmp_path, capsys):
    account["rewards"][0]["amount"] = "NaN"
    path = tmp_path / "account.json"
    path.write_text(json.dumps(account), encoding="utf-8")
    assert main(["wallet", "--snapshot", str(path)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "invalid_input"
    assert "amount" in result["error"]


def test_unknown_coupon_availability_requires_review(account):
    account["coupons"][0]["state"] = "unknown"
    assert summary(account)["status"] == "partial"
