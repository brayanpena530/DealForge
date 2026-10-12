"""Tests use fictional local preferences, never the developer's configuration."""

import json
import pytest


@pytest.fixture(autouse=True)
def isolated_preferences(tmp_path_factory, monkeypatch):
    path = tmp_path_factory.mktemp("private-preferences") / "preferences.json"
    path.write_text(json.dumps({
        "cvs": {"zip_code": "00000", "store_timezone": "America/Chicago",
                "profile": {"return_days": [14, 28]}},
        "heb": {"zip_code": "00000", "store_id": "101", "name": "Example H-E-B", "address": "Example store address"},
    }), encoding="utf-8")
    monkeypatch.setenv("DEALFORGE_PREFERENCES_FILE", str(path))
