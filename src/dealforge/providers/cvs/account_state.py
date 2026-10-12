"""Device-local account bindings and allowlisted definition cache.

No card numbers, credentials, raw responses or certificate sequences are saved.
The binding uses a random local HMAC key; labels cannot silently switch cards.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import re
import tempfile
from typing import Any

from dealforge.providers.cvs.wallet import parse_timestamp

DEFINITION_FIELDS = ("cpnDsc", "cpnQualTxt", "cpnTermsTxt", "disclaimerTxt", "observed_ui_headline")


class AccountChanged(ValueError):
    pass


def default_state_directory() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "DealForge" / "cvs"


class AccountState:
    def __init__(self, directory: Path | None = None):
        self.directory = directory if directory is not None else default_state_directory()

    def _key(self) -> bytes:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / "identity.key"
        try:
            with path.open("xb") as file:
                if os.name != "nt":
                    os.chmod(path, 0o600)
                file.write(secrets.token_bytes(32))
        except FileExistsError:
            pass
        key = path.read_bytes()
        if len(key) != 32:
            raise ValueError("Local CVS binding key is unavailable")
        return key

    def bind(self, account_ref: str, card: Any) -> tuple[str, str | None]:
        """Return first_seen/matched/unavailable plus a private cache namespace.

        This verifies continuity against this device's binding, not ownership
        or the shopper's intended card. A first encounter is explicitly labeled.
        """
        if type(card) is int:
            card = str(card)
        # The observed profile uses a nine-digit internal xtraCardNbr, distinct
        # from the printed/encoded card barcode. Fingerprint this source identity.
        if not isinstance(card, str) or not card.isascii() or not card.isdigit() or not 9 <= len(card) <= 20:
            return "unavailable", None
        fingerprint = hmac.new(self._key(), card.encode(), hashlib.sha256).hexdigest()
        label = hashlib.sha256(account_ref.encode()).hexdigest()
        path = self.directory / f"binding-{label}.json"
        content = {"version": 1, "fingerprint": fingerprint}
        try:
            with path.open("x", encoding="utf-8") as file:
                if os.name != "nt":
                    os.chmod(path, 0o600)
                json.dump(content, file)
            return "first_seen", fingerprint
        except FileExistsError:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict) or existing.get("version") != 1 or not isinstance(existing.get("fingerprint"), str):
                raise ValueError("Local CVS account binding is unavailable")
            if not hmac.compare_digest(existing["fingerprint"], fingerprint):
                raise AccountChanged("CVS account changed for this label")
            return "matched", fingerprint

    def _cache_path(self, namespace: str, offer_id: str) -> Path:
        # Hash both components; neither one is ever interpreted as a path.
        digest = hashlib.sha256(json.dumps([namespace, offer_id]).encode()).hexdigest()
        folder = self.directory / "definitions"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        return folder / f"{digest}.json"

    def read_definition(self, namespace: str, offer_id: str, *, signature: str,
                        now: datetime, max_age: timedelta = timedelta(hours=24)) -> dict | None:
        path = self._cache_path(namespace, offer_id)
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
            age = now - parse_timestamp(content["observed_at"])
            if (content.get("version") != 1 or content.get("signature") != signature
                    or not timedelta(0) <= age <= max_age):
                return None
            definition = content.get("definition")
            if not isinstance(definition, dict) or set(definition) - set(DEFINITION_FIELDS):
                return None
            if not all(isinstance(value, str) for value in definition.values()):
                return None
            return {"definition": definition, "observed_at": content["observed_at"]}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def write_definition(self, namespace: str, offer_id: str, definition: dict, *,
                         signature: str, observed_at: datetime) -> None:
        path = self._cache_path(namespace, offer_id)
        # Cache only description/terms text. No amounts, dates, flags, card IDs,
        # raw catalog identities or certificate sequences can cross this boundary.
        clean = {key: re.sub(r"\b\d{10,}\b", "[masked]", " ".join(definition[key].split())) for key in DEFINITION_FIELDS
                 if isinstance(definition.get(key), str)}
        if not clean.get("cpnTermsTxt"):
            return
        content = {"version": 1, "signature": signature,
                   "observed_at": observed_at.isoformat(), "definition": clean}
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             suffix=".tmp", delete=False) as file:
                name = file.name
                json.dump(content, file, ensure_ascii=True)
            os.replace(name, path)
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)


def description_signature(row: dict) -> str:
    values = [row.get(key) for key in ("cpnDsc", "cpnQualTxt", "webDsc", "mfrOfferValueDsc", "mfrOfferBrandName", "expirDt")]
    return hashlib.sha256(json.dumps(values, ensure_ascii=True).encode()).hexdigest()
