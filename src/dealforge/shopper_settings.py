"""Private shopper preferences stored outside the repository, never logged."""

import json
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo


def preferences_path():
    override = os.environ.get("DEALFORGE_PREFERENCES_FILE")
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) if os.name == "nt" else Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    path = Path(override).expanduser().resolve() if override else (base / "DealForge" / "preferences.json").resolve()
    repo = Path(__file__).resolve().parents[2]
    if path == repo or repo in path.parents:
        raise ValueError("Private preferences must be stored outside the repository")
    return path


def preferences(provider):
    path = preferences_path()
    try:
        if path.stat().st_size > 16_384:
            raise ValueError("Invalid local preference file")
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Could not read local preferences") from None
    if not isinstance(data, dict) or not isinstance(data.get(provider, {}), dict):
        raise ValueError("Invalid local preference file")
    return data.get(provider, {})


def shopping_zip(provider, value=None):
    value = value if value is not None else preferences(provider).get("zip_code")
    if not isinstance(value, str) or not re.fullmatch(r"\d{5}", value):
        raise ValueError("Pass a five-digit ZIP or configure it in local preferences")
    return value


def shopping_store(provider, value=None):
    value = value if value is not None else preferences(provider).get("store_id")
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"\d{1,12}", value)):
        raise ValueError("Invalid local store preference")
    return value


def store_timezone(value=None):
    value = value if value is not None else preferences("cvs").get("store_timezone", "UTC")
    if not isinstance(value, str):
        raise ValueError("Invalid store timezone")
    try:
        ZoneInfo(value)
    except (KeyError, ValueError):
        raise ValueError("Invalid store timezone") from None
    return value


def planning_profile(value=None):
    value = value if value is not None else preferences("cvs").get("profile", {})
    if not isinstance(value, dict):
        raise ValueError("Invalid local planning profile")
    return value
