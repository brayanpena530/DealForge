"""Tests for resolving a grocery list against the H-E-B weekly ad.

Every flyer string here is real ad copy from the 77008 flyer for the week of
2026-09-16 (`fixtures/heb_flyer_8130390.json`). The grocery lists are ordinary
ones -- the point of most of these tests is what a shopper actually types.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dealforge.providers.heb.catalog import (
    build_offers,
    match_list,
    match_query,
    normalize,
    tokenize,
)

FIXTURE = Path(__file__).parent / "fixtures" / "heb_flyer_8130390.json"

#: A plain weekly shop. Not chosen to flatter the matcher -- milk, eggs and
#: bananas are in it precisely because the ad does not carry them.
SHOPPING_LIST = [
    "milk",
    "eggs",
    "greek yogurt",
    "chicken breast",
    "shrimp",
    "bacon",
    "tortillas",
    "pumpkins",
    "coffee",
    "bread",
    "bananas",
    "apples",
    "sports drink",
    "diapers",
    "ice cream",
    "frozen pizza",
    "potatoes",
    "spinach",
    "batteries",
    "salad",
]


@pytest.fixture(scope="module")
def flyer_items() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["items"]


@pytest.fixture(scope="module")
def offers(flyer_items):
    return build_offers(flyer_items)


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------


def test_trademark_marks_do_not_leak_into_tokens():
    """NFKD decomposes U+2122 into the letters "TM".

    Stripping after normalizing leaves "organicstm", which matches nothing a
    shopper would ever type.
    """
    assert "organic" in tokenize("H-E-B Organics™ Fresh Baby Spinach")
    assert not any(t.endswith("tm") for t in tokenize("H-E-B Organics™"))
    assert "tienda" in tokenize("H-E-B Mi Tienda® Items")


def test_sizes_and_counts_are_stripped():
    tokens = tokenize("Meal Simple Microwaveable Meals, 10.5 - 14 oz. assorted varieties")
    assert "meal" in tokens and "microwaveable" in tokens
    assert not {"10.5", "14", "oz"} & tokens
    assert "assorted" not in tokens, "marketing filler carries no signal"


def test_size_ranges_with_counts():
    tokens = tokenize("Frozen Shrimp, 31 - 40 ct., 16 oz.")
    assert tokens == {"frozen", "shrimp"}


@pytest.mark.parametrize(
    "plural,singular",
    [
        ("potatoes", "potato"),
        ("tomatoes", "tomato"),
        ("batteries", "battery"),
        ("peaches", "peach"),
        ("pumpkins", "pumpkin"),
        ("tortillas", "tortilla"),
    ],
)
def test_plurals_fold_to_the_singular_not_just_consistently(plural, singular):
    """Both spellings have to reach the same stem, in both directions.

    Folding "potatoes" to "potatoe" is self-consistent and still wrong: the ad
    says "Potatoes", so a list that says "potato" would silently miss.
    """
    assert tokenize(plural) == tokenize(singular)


def test_stem_final_s_is_not_stripped():
    assert tokenize("molasses") == {"molasses"}
    assert tokenize("asparagus") == {"asparagus"}


def test_normalize_is_idempotent():
    once = normalize("H-E-B Texas Roots™ Pumpkins or Mums")
    assert normalize(once) == once


# ---------------------------------------------------------------------------
# turning flyer rows into eligibility sets
# ---------------------------------------------------------------------------


def test_rows_with_no_promotion_are_dropped(offers, flyer_items):
    """There is nothing to optimize about a row carrying no offer."""
    assert len(offers) == 55
    assert all(o.promotions for o in offers)


def test_a_row_listing_alternatives_is_matchable_by_each_one(offers):
    """`Pomegranates, or H-E-B Flavor Bombs Sweet Tomatoes` is one offer
    covering two different products."""
    by_pomegranate = match_query("pomegranates", offers)
    by_tomato = match_query("tomatoes", offers)
    assert by_pomegranate and by_tomato
    assert by_pomegranate[0].offer.name == by_tomato[0].offer.name


def test_a_fragment_does_not_lose_the_head_noun(offers):
    """`Duracell Coppertop AA or AAA Batteries` splits into fragments where
    "AA" has no noun. Matching also scores the whole row, so "batteries" and
    "AAA batteries" both land."""
    assert match_query("batteries", offers)[0].offer.name.startswith("Duracell")
    assert match_query("AAA batteries", offers)[0].offer.name.startswith("Duracell")


def test_brand_family_rows_are_flagged(offers):
    families = [o.name for o in offers if o.is_brand_family]
    assert any("Mi Tienda" in n for n in families)
    assert any("TRESEmm" in n for n in families)


def test_brand_family_rows_are_still_matchable(offers):
    """Regression: excluding them lost "diapers" entirely.

    `HUGGIES Diapers, Little Swimmers, Wipes, Pull-Ups, or Goodnites Items`
    ends in "Items" like a pure brand family, but names real products.
    """
    matches = match_query("diapers", offers)
    assert matches, "diapers are in this flyer"
    assert "HUGGIES" in matches[0].offer.name


# ---------------------------------------------------------------------------
# which offers cannot be valued from the ad alone
# ---------------------------------------------------------------------------


def test_percent_and_threshold_offers_need_a_price(offers):
    """`SAVE 20%` on pumpkins is unusable without knowing pumpkin prices."""
    pumpkins = match_query("pumpkins", offers)[0].offer
    assert pumpkins.savings_need_a_price is True

    huggies = match_query("diapers", offers)[0].offer
    assert huggies.savings_need_a_price is True, "a $40 threshold needs unit prices"


def test_a_stated_price_settles_it(offers):
    """When the ad prints a price, nothing further is needed."""
    priced = [o for o in offers if any(p.unit_price for p in o.promotions)]
    assert priced
    assert all(not o.savings_need_a_price for o in priced)


def test_how_much_of_the_flyer_needs_a_price_book(offers):
    """Locks the measured figure. This is the size of the gap the price book
    has to close before the optimizer can run on the whole ad."""
    assert sum(o.savings_need_a_price for o in offers) == 12


# ---------------------------------------------------------------------------
# matching a shopper's words
# ---------------------------------------------------------------------------


def test_exact_match_scores_one(offers):
    match = match_query("bacon", offers)[0]
    assert match.score == 1.0
    assert match.matched == {"bacon"}
    assert not match.missing


def test_several_options_are_returned_not_just_the_first(offers):
    """Two brands of Greek yogurt are on offer. Picking one here would take a
    decision that belongs to the optimizer."""
    names = [m.offer.name for m in match_query("greek yogurt", offers)]
    assert len(names) >= 2
    assert any("FAGE" in n for n in names)
    assert any("Chobani" in n for n in names)


def test_head_noun_must_match_on_a_multi_word_query(offers):
    """Regression: "frozen pizza" scored 0.5 against "Frozen Fully Cooked
    Meatballs" and "Frozen Tilapia Fillets" -- the shared word is the
    adjective, and none of those is a pizza."""
    names = [m.offer.name for m in match_query("frozen pizza", offers)]
    assert names == ["Amy's Frozen Pizza"]


def test_unusual_word_order_still_matches(offers):
    """The head-noun rule is skipped when every word matched, so a list that
    says "yogurt, greek" is not punished for it."""
    assert match_query("yogurt greek", offers)


def test_an_item_not_in_the_ad_returns_nothing(offers):
    """An empty result is the correct answer, not a failure. Most of a weekly
    shop is not in the circular, and a wrong match would cost real money."""
    for absent in ("milk", "eggs", "bananas"):
        assert match_query(absent, offers) == []


def test_a_loose_match_reports_the_words_it_added(offers):
    """"tortillas" reaches "Central Market Totopos Tortilla Chips". That may
    not be what the shopper meant, so the extra words are exposed rather than
    silently accepted."""
    match = next(
        m for m in match_query("tortillas", offers) if "Totopos" in m.offer.name
    )
    assert "chip" in match.extra
    assert match.is_exact is False


def test_matches_are_ordered_best_first(offers):
    scores = [m.score for m in match_query("shrimp", offers)]
    assert scores == sorted(scores, reverse=True)


def test_empty_and_noise_queries_are_safe(offers):
    assert match_query("", offers) == []
    assert match_query("   ", offers) == []
    assert match_query("16 oz", offers) == []


# ---------------------------------------------------------------------------
# a whole list
# ---------------------------------------------------------------------------


def test_every_list_item_gets_an_entry(offers):
    resolved = match_list(SHOPPING_LIST, offers)
    assert set(resolved) == set(SHOPPING_LIST)


def test_coverage_of_an_ordinary_shopping_list(offers):
    """How much of a normal weekly shop this flyer can speak to at all.

    If this drops, either the ad got thinner or the matcher regressed -- both
    are worth a failing test, because it is the number that decides whether
    the tool is useful on any given week.
    """
    resolved = match_list(SHOPPING_LIST, offers)
    matched = [item for item, found in resolved.items() if found]
    assert len(matched) == 17
    assert set(SHOPPING_LIST) - set(matched) == {"milk", "eggs", "bananas"}


def test_every_match_carries_its_reasoning(offers):
    """A plan has to be auditable against the ad, so no match is a bare score."""
    for found in match_list(SHOPPING_LIST, offers).values():
        for match in found:
            assert match.matched
            assert match.term
            assert match.offer.promotions
            assert all(p.raw.strip() for p in match.offer.promotions)


def test_no_match_is_ever_returned_below_the_threshold(offers):
    for found in match_list(SHOPPING_LIST, offers, min_score=0.5).values():
        assert all(m.score >= 0.5 for m in found)
