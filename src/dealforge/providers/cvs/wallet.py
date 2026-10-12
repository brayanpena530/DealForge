"""Read-only account snapshot contract for CVS adapters and agent tools.

This module does not log in, scrape CVS, activate offers or price a basket.
Unknown amounts, expiry dates and eligibility remain unknown. The same input
contract can be populated by a browser adapter or an initial local import.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
import re
from typing import Any

from dealforge.models import PromoSource


class AccountStatus(str, Enum):
    READY = "ready"
    PARTIAL = "partial"
    NEEDS_LOGIN = "needs_login"
    BLOCKED_ACCESS = "blocked_access"


class RewardKind(str, Enum):
    EXTRABUCKS = "extrabucks"
    MEMBERSHIP_BONUS = "membership_bonus"
    GIFT_CARD = "gift_card"
    STORE_CREDIT = "store_credit"
    UNKNOWN = "unknown"


class OfferState(str, Enum):
    AVAILABLE = "available"
    ON_CARD = "on_card"
    REDEEMED = "redeemed"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class Channel(str, Enum):
    IN_STORE = "in_store"
    ONLINE = "online"
    BOTH = "both"
    UNKNOWN = "unknown"


def _object(value: Any, allowed: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    if set(value) - allowed:
        raise ValueError(f"{name} contains unsupported fields")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value.strip()


def _money(value: Any) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,12}(?:\.\d{1,2})?", value):
        raise ValueError("amount must be a nonnegative decimal string with at most 12 integer digits and two decimal places")
    return Decimal(value).quantize(Decimal("0.01"))


def _date(value: Any) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("expires_on must be an ISO date or null")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("expires_on must be a valid ISO date") from None


def parse_timestamp(value: Any) -> datetime:
    """Require an explicit UTC offset, including Z, for capture and freshness."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO string with a UTC offset")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("timestamp must be an ISO string with a UTC offset") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return result


def _enum(enum: type[Enum], value: Any, field: str) -> Any:
    try:
        return enum(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {field}") from None


@dataclass(frozen=True)
class RewardCertificate:
    reward_id: str
    kind: RewardKind
    amount: Decimal | None
    expires_on: date | None
    state: OfferState
    channel: Channel
    partial_redemption: bool | None
    raw_terms: str


@dataclass(frozen=True)
class PersonalCoupon:
    coupon_id: str
    headline: str
    issuer: PromoSource | None
    expires_on: date | None
    state: OfferState
    channel: Channel
    raw_terms: str


@dataclass(frozen=True)
class AccountSnapshot:
    account_ref: str
    observed_at: datetime
    source: str
    status: AccountStatus
    rewards_complete: bool
    coupons_complete: bool
    rewards: tuple[RewardCertificate, ...]
    coupons: tuple[PersonalCoupon, ...]

    @classmethod
    def from_dict(cls, data: Any) -> AccountSnapshot:
        data = _object(data, {
            "schema_version", "account_ref", "observed_at", "source", "status",
            "rewards_complete", "coupons_complete", "rewards", "coupons",
        }, "snapshot")
        if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise ValueError("schema_version must be 1")
        for field in ("rewards_complete", "coupons_complete"):
            if type(data.get(field)) is not bool:
                raise ValueError(f"{field} must be a boolean")
        if data.get("source") not in ("browser", "manual", "fixture"):
            raise ValueError("source must be browser, manual or fixture")
        for field in ("rewards", "coupons"):
            if not isinstance(data.get(field), list):
                raise ValueError(f"{field} must be an array")

        rewards = []
        for row in data["rewards"]:
            row = _object(row, {
                "reward_id", "kind", "amount", "expires_on", "state",
                "channel", "partial_redemption", "raw_terms",
            }, "reward")
            partial = row.get("partial_redemption")
            if partial is not None and type(partial) is not bool:
                raise ValueError("partial_redemption must be a boolean or null")
            rewards.append(RewardCertificate(
                reward_id=_text(row.get("reward_id"), "reward_id"),
                kind=_enum(RewardKind, row.get("kind", "unknown"), "reward kind"),
                amount=_money(row.get("amount")),
                expires_on=_date(row.get("expires_on")),
                state=_enum(OfferState, row.get("state", "unknown"), "reward state"),
                channel=_enum(Channel, row.get("channel", "unknown"), "reward channel"),
                partial_redemption=partial,
                raw_terms=_text(row.get("raw_terms"), "reward raw_terms"),
            ))

        coupons = []
        for row in data["coupons"]:
            row = _object(row, {
                "coupon_id", "headline", "issuer", "expires_on", "state",
                "channel", "raw_terms",
            }, "coupon")
            issuer = row.get("issuer")
            if issuer is not None:
                issuer = _enum(PromoSource, issuer, "coupon issuer")
                if issuer not in (PromoSource.MFR_COUPON, PromoSource.STORE_COUPON):
                    raise ValueError("coupon issuer must be mfr_coupon, store_coupon or null")
            coupons.append(PersonalCoupon(
                coupon_id=_text(row.get("coupon_id"), "coupon_id"),
                headline=_text(row.get("headline"), "coupon headline"),
                issuer=issuer,
                expires_on=_date(row.get("expires_on")),
                state=_enum(OfferState, row.get("state", "unknown"), "coupon state"),
                channel=_enum(Channel, row.get("channel", "unknown"), "coupon channel"),
                raw_terms=_text(row.get("raw_terms"), "coupon raw_terms"),
            ))

        for records, field in ((rewards, "reward_id"), (coupons, "coupon_id")):
            ids = [getattr(row, field) for row in records]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {field}")

        return cls(
            account_ref=_text(data.get("account_ref"), "account_ref"),
            observed_at=parse_timestamp(data.get("observed_at")),
            source=data["source"],
            status=_enum(AccountStatus, data.get("status"), "account status"),
            rewards_complete=data["rewards_complete"],
            coupons_complete=data["coupons_complete"],
            rewards=tuple(rewards), coupons=tuple(coupons),
        )

    def to_dict(self) -> dict[str, Any]:
        def record(row: RewardCertificate | PersonalCoupon) -> dict[str, Any]:
            values = vars(row).copy()
            for key, value in values.items():
                if isinstance(value, Enum):
                    values[key] = value.value
                elif isinstance(value, Decimal):
                    values[key] = f"{value:.2f}"
                elif isinstance(value, date):
                    values[key] = value.isoformat()
            return values

        return {
            "schema_version": 1,
            "account_ref": self.account_ref,
            "observed_at": self.observed_at.isoformat(),
            "source": self.source,
            "status": self.status.value,
            "rewards_complete": self.rewards_complete,
            "coupons_complete": self.coupons_complete,
            "rewards": [record(row) for row in self.rewards],
            "coupons": [record(row) for row in self.coupons],
        }


def summarize_wallet(
    snapshot: AccountSnapshot,
    *,
    as_of: datetime,
    max_age: timedelta = timedelta(hours=24),
) -> dict[str, Any]:
    """Inventory, not a spendable balance or a checkout eligibility guarantee.

    ``as_of`` must carry the shopping location's offset: expiry dates are
    compared with its local calendar date. The 24h freshness limit is a product
    default, not CVS policy. Expiring today is included and explicitly flagged.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must include the shopping location's UTC offset")
    if max_age <= timedelta(0):
        raise ValueError("max_age must be positive")
    age = as_of - snapshot.observed_at
    if age < timedelta(0):
        raise ValueError("snapshot was observed after as_of")

    status = snapshot.status.value
    warnings = []
    if not snapshot.rewards_complete or not snapshot.coupons_complete:
        warnings.append("Account collection is incomplete.")
    if age > max_age:
        warnings.append("Refresh the account snapshot before planning a purchase.")
    if snapshot.source == "fixture":
        warnings.append("Synthetic fixture data; this is not the shopper's account.")

    totals: dict[str, Decimal] = {}
    records = snapshot.to_dict()
    for row, result in zip(snapshot.rewards, records["rewards"]):
        expired = None if row.expires_on is None else row.expires_on < as_of.date()
        result["expired"] = expired
        result["expires_today"] = row.expires_on == as_of.date()
        if row.state is OfferState.UNKNOWN:
            warnings.append(f"Reward {row.reward_id} has unknown availability.")
        if row.state not in (OfferState.AVAILABLE, OfferState.ON_CARD):
            continue
        if row.amount is None or expired is None or row.kind is RewardKind.UNKNOWN:
            warnings.append(f"Reward {row.reward_id} has unknown value, expiry or type.")
        if row.channel is Channel.UNKNOWN or row.partial_redemption is None:
            warnings.append(f"Reward {row.reward_id} needs redemption terms verified.")
        if expired is False and row.amount is not None and row.kind is not RewardKind.UNKNOWN:
            totals[row.kind.value] = totals.get(row.kind.value, Decimal(0)) + row.amount

    for row, result in zip(snapshot.coupons, records["coupons"]):
        result["expired"] = None if row.expires_on is None else row.expires_on < as_of.date()
        if row.state is OfferState.UNKNOWN:
            warnings.append(f"Coupon {row.coupon_id} has unknown availability.")
        if row.state in (OfferState.AVAILABLE, OfferState.ON_CARD):
            if row.expires_on is None or row.issuer is None or row.channel is Channel.UNKNOWN:
                warnings.append(f"Coupon {row.coupon_id} needs eligibility details verified.")

    if status not in ("needs_login", "blocked_access"):
        if age > max_age:
            status = "stale"
        elif warnings:
            status = "partial"

    # Historical records can be useful, but never advertise a balance while
    # authentication/access is unresolved or the snapshot is stale.
    if status in ("needs_login", "blocked_access", "stale"):
        totals = {}
    actions = {
        "needs_login": "Sign in to CVS in the connected browser.",
        "blocked_access": "Check CVS access in your normal browser; avoid repeated login attempts.",
        "stale": "Refresh the account snapshot.",
        "partial": "Complete the snapshot and verify unknown reward/coupon terms.",
        "ready": "Resolve your shopping list against current store prices and offers.",
    }
    return {
        "status": status,
        "account": records,
        "as_of": as_of.isoformat(),
        "known_unexpired_face_value_by_kind": {kind: f"{value:.2f}" for kind, value in totals.items()},
        "balance_is_checkout_verified": False,
        "warnings": warnings,
        "next_action": actions[status],
    }
