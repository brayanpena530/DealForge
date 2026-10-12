import pytest

from dealforge.providers.cvs.eligibility import extract_requirements


@pytest.mark.parametrize("text,kind,value,spend,quantity", [
    ("$10 off $40 on Hair Products (excludes trial/travel sizes)", "amount_off", "10.00", "40.00", None),
    ("SAVE $2.00 MOTRIN on any ONE (1) Adult Product", "amount_off", "2.00", None, 1),
    ("$6.00 Off Any 2 Revlon cosmetics", "amount_off", "6.00", None, 2),
    ("30% off your purchase", "percent_off", "30.00", None, None),
    ("$1.00 off any TWO (2) qualifying products", "amount_off", "1.00", None, 2),
])
def test_explicit_requirements_preserve_evidence_and_unknowns(text, kind, value, spend, quantity):
    rule = extract_requirements("cvs-example", text)
    assert rule["discount"] == {"kind": kind, "value": value}
    assert rule["minimum_spend"] == spend
    assert rule["minimum_quantity"] == quantity
    assert rule["status"] == "partial" and not rule["checkout_eligibility_verified"]
    assert {"eligible_product_ids", "stacking", "spend_qualification_basis"} <= set(rule["unknowns"])
    assert rule["evidence"]


def test_conflicting_requirements_never_choose_the_cheapest_or_first():
    rule = extract_requirements("x", "$2 off $20 on any 1 soap\n$3 off $30 on any 2 soap\n10% off")
    assert rule["discount"]["kind"] == "unknown"
    assert rule["minimum_spend"] is rule["minimum_quantity"] is None
    assert {"amount_off", "discount_kind", "minimum_spend", "minimum_quantity"} <= set(rule["conflicts"])


@pytest.mark.parametrize("text", ["on any ONE (4) item", "any 2,000 items", "any 0 items", "one coupon per purchase",
                                  "$2 off 20 oz soap", "$2 off any 20 ct package", "2 in 1 shampoo"])
def test_ambiguous_quantity_is_not_a_purchase_requirement(text):
    assert extract_requirements("x", text)["minimum_quantity"] is None


@pytest.mark.parametrize("text", ["$2.123 off", "save $1,000", "$5 off $20.123", "150% off", "-10% off"])
def test_malformed_amounts_and_percentages_do_not_become_valid_discounts(text):
    rule = extract_requirements("x", text)
    assert rule["minimum_spend"] is None
    if not text.startswith("$5 off"):
        assert rule["discount"]["kind"] == "unknown"


def test_literal_exclusions_and_repeated_evidence():
    rule = extract_requirements("x", "$2 off (excludes trial sizes)\n$2 off (excludes trial sizes)")
    assert rule["exclusions_text"] == ["excludes trial sizes"]
    assert rule["evidence"] == [{"field": "amount_off", "text": "$2 off"}]
