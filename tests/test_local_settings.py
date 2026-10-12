"""Private preference storage and caller precedence, using fictional values."""

import asyncio
import json
from pathlib import Path

import pytest

from dealforge.shopper_settings import (
    preferences, preferences_path, shopping_zip, shopping_store, store_timezone, planning_profile,
)


def config(tmp_path, monkeypatch, data):
    path = tmp_path / "local.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("DEALFORGE_PREFERENCES_FILE", str(path))
    return path


def test_external_preferences_and_explicit_values_take_precedence(tmp_path, monkeypatch):
    config(tmp_path, monkeypatch, {"cvs": {"zip_code": "00000", "store_id": "10001",
        "store_timezone": "UTC", "profile": {"return_days": [9, 21]}}})
    assert shopping_zip("cvs") == "00000" and shopping_store("cvs") == "10001"
    assert shopping_zip("cvs", "00001") == "00001"
    assert shopping_store("cvs", "10002") == "10002"
    assert store_timezone() == "UTC" and store_timezone("America/New_York") == "America/New_York"
    assert planning_profile()["return_days"] == [9, 21]
    assert planning_profile({"return_days": [3]}) == {"return_days": [3]}


def test_absent_preferences_never_guess_live_location(tmp_path, monkeypatch):
    monkeypatch.setenv("DEALFORGE_PREFERENCES_FILE", str(tmp_path / "missing.json"))
    assert preferences("cvs") == {} and shopping_store("cvs") is None
    assert store_timezone() == "UTC"
    with pytest.raises(ValueError, match="Pass a five-digit ZIP"):
        shopping_zip("cvs")
    assert shopping_zip("cvs", "00000") == "00000"


def test_preferences_inside_repo_are_rejected_without_reading_contents(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("DEALFORGE_PREFERENCES_FILE", str(root / "private.json"))
    with pytest.raises(ValueError, match="outside the repository"):
        preferences_path()


@pytest.mark.parametrize("content", ['{"cvs": []}', 'not json', '{"cvs":{"zip_code":"SECRET-INVALID"}}'])
def test_invalid_preferences_do_not_echo_private_values(tmp_path, monkeypatch, content):
    path = tmp_path / "local.json"
    path.write_text(content)
    monkeypatch.setenv("DEALFORGE_PREFERENCES_FILE", str(path))
    with pytest.raises(ValueError) as error:
        shopping_zip("cvs")
    assert "SECRET" not in str(error.value)


def test_missing_zip_rejected_before_browser_access(tmp_path, monkeypatch):
    config(tmp_path, monkeypatch, {})
    from dealforge.providers.cvs.planning import plan_trip
    class Untouched:
        async def sync(self, **options):
            raise AssertionError("No browser access before configured location")
    with pytest.raises(ValueError, match="Pass a five-digit ZIP"):
        asyncio.run(plan_trip(Untouched(), items=[{"line_id":"soap","quantity":1,"query":"soap"}]))


def test_local_profile_is_used_by_optimizer(tmp_path, monkeypatch):
    config(tmp_path, monkeypatch, {"cvs": {"profile": {"return_days": [9, 21]}}})
    from dealforge.providers.cvs.optimizer import _profile
    assert _profile(None)[0] == [9, 21]
    assert _profile({})[0] == [7, 30]
