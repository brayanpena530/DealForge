"""Normalize an observed CVS profile response into a conservative inventory draft.

This pure adapter does not connect to Chrome, replay private APIs, or activate
offers. It deliberately ignores account/profile details and duplicate raw lists.
Its results remain partial: catalog terms and several flag meanings still need
validation before this becomes the complete ``sync_cvs_account`` collector.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Iterable

from dealforge.providers.cvs.wallet import AccountSnapshot, parse_timestamp
from dealforge.providers.cvs.eligibility import extract_requirements, extract_term_candidates
from dealforge.providers.cvs.catalog import clean_eligible_products


_CATEGORIES = ("extrabucks", "personalizedDeals", "otherDeals")


def _identity(row: dict[str, Any], reward: bool) -> tuple[str, ...]:
    fields = ("cmpgnId", "cpnNbr", "cpnSeqNbr") if reward else ("cmpgnId", "cpnNbr")
    values = []
    for field in fields:
        value = row.get(field)
        if type(value) is int and value >= 0:
            values.append(str(value))
        elif isinstance(value, str) and re.fullmatch(r"\d+", value):
            values.append(str(int(value)))
        else:
            raise ValueError("missing source identity")
    return tuple(values)


def _text(*values: Any) -> str:
    # Source text is allowlisted; omit long identifiers if one occurs in prose.
    parts = []
    for value in values:
        if isinstance(value, str) and value.strip():
            part = re.sub(r"\b\d{10,}\b", "[masked]", " ".join(value.split()))
            if part not in parts:
                parts.append(part)
    return "\n".join(parts)


def _state(row: dict[str, Any]) -> str:
    # Observed against the account UI. Definition-level flags are not account state.
    if row.get("redeemableInd") == "Y" and row.get("loadableInd") == "N":
        return "on_card"
    if row.get("loadableInd") == "Y" and row.get("redeemableInd") == "N":
        return "available"
    return "unknown"


def record_id(account_ref: str, row: dict[str, Any], reward: bool) -> str:
    identity = _identity(row, reward)
    token = json.dumps([account_ref.strip(), "reward" if reward else "coupon", *identity])
    return "cvs-" + hashlib.sha256(token.encode()).hexdigest()[:24]


@dataclass(frozen=True)
class ProfileInventoryDraft:
    snapshot: AccountSnapshot
    collection: dict[str, Any]
    review: tuple[str, ...]
    definitions_observed: tuple[str, ...]
    rule_candidates: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "partial",
            "snapshot": self.snapshot.to_dict(),
            "collection": self.collection,
            "review": list(self.review),
            "definitions_observed": list(self.definitions_observed),
            "terms_complete": False,
            "live_sync_available": False,
            "offer_rules": list(self.rule_candidates),
        }


def normalize_profile_response(
    payload: Any,
    *,
    account_ref: str,
    observed_at: datetime,
    reported_count: int | None = None,
    definitions: Iterable[dict[str, Any]] = (),
    ui_states: dict[tuple[str, ...], str] | None = None,
) -> ProfileInventoryDraft:
    """Read categorized records; return opaque identities and explicit incompleteness.

    ``definitions`` are individual records from observed
    ``extracareCouponDefinition.extracareCouponDefinitions`` responses. They
    enrich text only; their amounts, dates and activation flags cannot replace
    certificate/account-specific fields. A matched UI count verifies the visible
    collection, not checkout eligibility or full coupon-policy coverage.
    """
    if not isinstance(payload, dict) or payload.get("statusCode") != "0000":
        raise ValueError("CVS profile response did not report success")
    data = payload.get("data")
    profile = data.get("getEcCustomerProfile") if isinstance(data, dict) else None
    if not isinstance(profile, dict) or profile.get("errorCd") not in (None, ""):
        raise ValueError("CVS coupon profile is unavailable")
    categories = profile.get("couponCategories")
    if not isinstance(categories, dict):
        raise ValueError("CVS coupon categories are unavailable")
    if not isinstance(account_ref, str) or not account_ref.strip():
        raise ValueError("account_ref must be an opaque nonempty reference")
    if reported_count is not None and (type(reported_count) is not int or reported_count < 0):
        raise ValueError("reported_count must be a nonnegative integer")

    detail_index = {}
    for definition in definitions:
        if not isinstance(definition, dict):
            continue
        try:
            identity = _identity(definition, False)
        except ValueError:
            continue
        # Conflicting definitions must not overwrite one another silently.
        if identity in detail_index and detail_index[identity] != definition:
            detail_index[identity] = None
        else:
            detail_index.setdefault(identity, definition)

    review = {"terms_and_flag_semantics_need_verification"}
    rewards, coupons = [], []
    source_count = hidden_count = visible_count = rejected_count = 0
    seen: dict[str, dict[str, Any]] = {}
    conflicts: set[str] = set()
    enriched, rule_candidates = [], []
    source_complete = set(categories) == set(_CATEGORIES)
    if not source_complete:
        review.add("missing_or_changed_category")
    for category in _CATEGORIES:
        rows = categories.get(category)
        if not isinstance(rows, list):
            source_complete = False
            review.add("missing_or_changed_category")
            continue
        for row in rows:
            source_count += 1
            if not isinstance(row, dict):
                rejected_count += 1
                continue
            if row.get("viewableInd") == "N" or row.get("cpnSuppressCd") == "Y":
                hidden_count += 1
                continue
            visible_count += 1
            reward = category == "extrabucks"
            try:
                identifier = record_id(account_ref, row, reward)
                detail = detail_index.get(_identity(row, False))
                terms = _text(row.get("cpnDsc"), row.get("cpnQualTxt"), row.get("mfrOfferValueDsc"), row.get("mfrOfferBrandName"),
                              detail.get("cpnDsc") if detail else None,
                              detail.get("cpnQualTxt") if detail else None,
                              detail.get("cpnTermsTxt") if detail else None,
                              detail.get("disclaimerTxt") if detail else None)
                record = {
                    "expires_on": row.get("expirDt"),
                    "state": _state(row),
                    # Only code B was observed. Do not extrapolate channel semantics.
                    "channel": "unknown",
                    "raw_terms": terms,
                }
                ui_state = (ui_states or {}).get(_identity(row, False))
                if ui_state in ("available", "on_card"):
                    if record["state"] not in ("unknown", ui_state):
                        record["state"] = "unknown"
                        review.add("account_and_ui_state_disagree")
                    else:
                        record["state"] = ui_state
                if reward:
                    headline = _text(row.get("cpnDsc"), row.get("webDsc")).lower()
                    kind = "membership_bonus" if "extracare plus" in headline and "bonus" in headline else "unknown"
                    if kind == "unknown" and "extrabucks" in headline:
                        kind = "extrabucks"
                    record.update(reward_id=identifier, kind=kind,
                                  amount=row.get("maxRedeemAmt"), partial_redemption=None)
                    # Validate each record using the same strict contract as imports.
                    test_rewards, test_coupons = [record], []
                else:
                    record.update(coupon_id=identifier, headline=_text(row.get("mfrOfferValueDsc"), row.get("mfrOfferBrandName"), row.get("cpnDsc")), issuer=None)
                    # mfrInd and the rendered label disagreed in the initial inspection.
                    test_rewards, test_coupons = [], [record]
                AccountSnapshot.from_dict({
                    "schema_version": 1, "account_ref": account_ref, "observed_at": observed_at.isoformat(),
                    "source": "browser", "status": "partial", "rewards_complete": False,
                    "coupons_complete": False, "rewards": test_rewards, "coupons": test_coupons,
                })
                if identifier in conflicts:
                    rejected_count += 1
                    continue
                if identifier in seen:
                    if seen[identifier] != record:
                        rejected_count += 1
                        conflicts.add(identifier)
                        review.add("conflicting_source_identity")
                    else:
                        review.add("duplicate_source_record")
                    continue
                seen[identifier] = record
                (rewards if reward else coupons).append(record)
                if detail and _text(detail.get("cpnTermsTxt")):
                    enriched.append(identifier)
                if not reward:
                    # Interpret descriptions/qualifiers, not generic policy prose:
                    # "limit one coupon" is not a required product quantity.
                    description = _text(row.get("cpnDsc"), row.get("cpnQualTxt"), row.get("webDsc"),
                                        row.get("mfrOfferValueDsc"), row.get("mfrOfferBrandName"),
                                        detail.get("cpnDsc") if detail else None,
                                        detail.get("cpnQualTxt") if detail else None,
                                        detail.get("observed_ui_headline") if detail else None)
                    candidate = extract_requirements(identifier, description)
                    candidate["description_text"] = description
                    candidate["term_candidates"] = extract_term_candidates(terms)
                    candidate["definition_observed"] = identifier in enriched
                    # Only fresh page observations supply product membership.
                    # Never cache this list or treat a carousel as exhaustive.
                    products = detail.get("observed_eligible_products", []) if detail else []
                    candidate["eligible_products_observed"] = clean_eligible_products(products)
                    candidate["eligible_products_complete"] = False
                    eligibility_time = detail.get("eligibility_observed_at") if detail else None
                    try:
                        candidate["eligibility_observed_at"] = parse_timestamp(eligibility_time).isoformat() if eligibility_time else None
                    except ValueError:
                        candidate["eligibility_observed_at"] = None
                    rule_candidates.append(candidate)
            except (ValueError, TypeError, AttributeError):
                rejected_count += 1
                review.add("source_record_needs_review")

    rewards = [row for row in rewards if row["reward_id"] not in conflicts]
    coupons = [row for row in coupons if row["coupon_id"] not in conflicts]
    enriched = [record_id for record_id in enriched if record_id not in conflicts]
    rule_candidates = [rule for rule in rule_candidates if rule["offer_id"] not in conflicts]
    collected_count = len(rewards) + len(coupons)
    count_matches = reported_count is not None and collected_count == reported_count
    complete = source_complete and count_matches and rejected_count == 0
    if not count_matches:
        review.add("visible_count_not_reconciled")
    snapshot = AccountSnapshot.from_dict({
        "schema_version": 1, "account_ref": account_ref, "observed_at": observed_at.isoformat(),
        "source": "browser", "status": "partial", "rewards_complete": complete,
        "coupons_complete": complete, "rewards": rewards, "coupons": coupons,
    })
    return ProfileInventoryDraft(snapshot, {
        "source_records": source_count, "hidden_records": hidden_count,
        "visible_source_records": visible_count, "collected_records": collected_count,
        "rejected_records": rejected_count, "reported_visible_count": reported_count,
        "visible_count_matches": count_matches,
    }, tuple(sorted(review)), tuple(sorted(enriched)), tuple(rule_candidates))
