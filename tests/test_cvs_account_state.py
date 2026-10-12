from datetime import datetime, timedelta, timezone
import json

import pytest

from dealforge.providers.cvs.account_state import AccountChanged, AccountState, description_signature

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def test_binding_persists_without_card_numbers_and_rejects_account_switch(tmp_path):
    state = AccountState(tmp_path)
    first, namespace = state.bind("personal", "999999999999999")
    assert first == "first_seen"
    assert AccountState(tmp_path).bind("personal", "999999999999999") == ("matched", namespace)
    with pytest.raises(AccountChanged):
        state.bind("personal", "888888888888888")
    assert state.bind("second", "888888888888888")[0] == "first_seen"
    for path in tmp_path.glob("*.json"):
        assert "999999999999999" not in path.read_text()
        assert "888888888888888" not in path.read_text()
    assert AccountState(tmp_path / "other-device").bind("personal", "999999999999999")[1] != namespace


def test_observed_nine_digit_internal_card_identity_is_supported(tmp_path):
    state = AccountState(tmp_path)
    assert state.bind("personal", 999999999)[0] == "first_seen"
    assert state.bind("personal", 999999999)[0] == "matched"


@pytest.mark.parametrize("card", [None, "***1234", True, "1234", "９９９９９９９９９９９９９９９"])
def test_unavailable_card_cannot_create_a_binding(tmp_path, card):
    assert AccountState(tmp_path).bind("personal", card) == ("unavailable", None)
    assert not list(tmp_path.iterdir())


def test_cache_is_allowlisted_scoped_and_invalidated_by_age_or_source_change(tmp_path):
    state = AccountState(tmp_path)
    _, namespace = state.bind("personal", "999999999999999")
    signature = description_signature({"cpnDsc": "$2 off soap", "expirDt": "2099-01-01"})
    state.write_definition(namespace, "offer", {
        "cpnTermsTxt": "Soap only. 123456789012345", "cpnDsc": "$2 off soap",
        "cpnNbr": 123456789012, "cookie": "SECRET", "activeInd": True, "maxRedeemAmt": 2,
    }, signature=signature, observed_at=NOW)
    cached = state.read_definition(namespace, "offer", signature=signature, now=NOW)
    assert cached["definition"] == {"cpnTermsTxt": "Soap only. [masked]", "cpnDsc": "$2 off soap"}
    assert cached["observed_at"] == NOW.isoformat()
    assert state.read_definition("different-card", "offer", signature=signature, now=NOW) is None
    assert state.read_definition(namespace, "offer", signature="changed", now=NOW) is None
    assert state.read_definition(namespace, "offer", signature=signature, now=NOW + timedelta(hours=25)) is None
    assert state.read_definition(namespace, "offer", signature=signature, now=NOW - timedelta(seconds=1)) is None
    text = next((tmp_path / "definitions").glob("*.json")).read_text()
    assert "SECRET" not in text and "123456789012" not in text and "activeInd" not in text


@pytest.mark.parametrize("content", ["not JSON", "[]", '{"version":1}', '{"observed_at":null}'])
def test_corrupt_cache_is_a_miss(tmp_path, content):
    state = AccountState(tmp_path)
    state._cache_path("n", "o").write_text(content)
    assert state.read_definition("n", "o", signature="s", now=NOW) is None


def test_missing_terms_do_not_create_a_cache_entry(tmp_path):
    state = AccountState(tmp_path)
    state.write_definition("n", "o", {"cpnDsc": "summary"}, signature="s", observed_at=NOW)
    assert not list((tmp_path / "definitions").glob("*.json"))
