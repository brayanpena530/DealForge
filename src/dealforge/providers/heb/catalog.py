"""Resolve a shopper's grocery list against the H-E-B weekly ad.

A flyer row is not a product. It is an *eligibility set* -- the set of products
an offer covers -- and the ad states that set in prose:

    Pomegranates, or H-E-B Flavor Bombs Sweet Tomatoes
    Duracell Coppertop AA or AAA Batteries
    H-E-B, Higher Harvest by H-E-B, H-E-B Organics, Hill Country Fare, or Central Market

19 of the 55 rows in the Houston flyer name more than one product this way. This
module turns each row into something a list item can be matched against, and
reports *why* it matched so the result stays inspectable.

Matching is deliberately deterministic token overlap, not embeddings. An LLM is
the right tool for "is 'chicken breast' the same as 'Boneless Skinless Chicken
Breast Tenders'", and the agent layer is welcome to re-rank these candidates --
but the candidate set, the scores and the reasons have to be reproducible, or a
plan cannot be audited against the ad.

Nothing here prices anything. :attr:`FlyerOffer.savings_need_a_price` reports
which offers cannot be valued from the ad alone, which is the majority of them.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from dealforge.models import Promotion, PromoKind
from dealforge.providers.heb.sale_story import parse_item

__all__ = [
    "FlyerOffer",
    "Match",
    "build_offers",
    "match_query",
    "match_list",
    "normalize",
    "tokenize",
]

# Sizes and counts: "10.5 - 14 oz.", "31 - 40 ct.", "16 oz.", "1/8 Sheet".
_SIZE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:-\s*\d+(?:\.\d+)?\s*)?"
    r"(?:oz|ct|lb|lbs|pk|pack|g|kg|ml|l|qt|gal|in|inch)\b\.?",
    re.I,
)
_FRACTION = re.compile(r"\b\d+/\d+\b")

# Marketing filler that never helps identify a product.
_FILLER = re.compile(
    r"\b(?:assorted|varieties|variety|select|selected|value|fresh|jumbo|new|"
    r"any|all|or\s+more|item|items|pack|size|family|each|ea)\b",
    re.I,
)

# Words too common in this flyer to carry signal on their own.
_STOPWORDS = frozenset({"and", "or", "of", "the", "with", "in", "by", "for", "a", "to"})

# Rows naming a brand family rather than a product. These qualify basket-level
# offers ("WHEN YOU BUY $20 OF H-E-B Mi Tienda Items") and must not be matched
# as if the shopper asked for a specific thing.
_BRAND_FAMILY = re.compile(r"\bitems\b\s*$", re.I)

# Where a row lists alternatives. Split points, not products in themselves.
_SPLIT = re.compile(r",\s*or\s+|\s+or\s+|,\s+", re.I)

# Plural folding. Only has to be good enough for grocery nouns, and it has to
# fold both directions to the same stem: the ad writes "Potatoes" and the
# shopper may write either, so "potato" and "potatoes" must agree. Folding
# "potatoes" to "potatoe" is consistent but silently loses the singular query.
_PLURAL_IES = re.compile(r"[^aeiou]ies$")
_PLURAL_ES = re.compile(r"(?:oes|ches|shes|xes|zes)$")
# Words whose trailing "s" belongs to the stem: molasses, asparagus, iris.
# "sses" is listed here rather than as a plural rule: folding it would turn
# "molasses" into "molass" to win "dresses" -> "dress", a trade no grocery list
# benefits from. The cost is a missed fold, never a wrong match.
_KEEP_TRAILING_S = re.compile(r"(?:sses|ss|us|is)$")


def normalize(text: str) -> str:
    """Lowercase, strip trademark marks, sizes and marketing filler.

    Trademark marks go first: NFKD decomposes U+2122 into the letters "TM", so
    stripping afterwards would leave "H-E-B Organics(TM)" tokenized as
    "organicstm" and never matchable.
    """
    text = text or ""
    text = text.replace("®", " ").replace("™", " ").replace("℠", " ")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[“”\"'`]", " ", text)
    text = text.lower()
    text = _SIZE.sub(" ", text)
    text = _FRACTION.sub(" ", text)
    text = _FILLER.sub(" ", text)
    text = re.sub(r"[^a-z0-9\-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _singular(word: str) -> str:
    if len(word) <= 3 or not word.endswith("s"):
        return word
    if _PLURAL_IES.search(word):  # batteries -> battery
        return word[:-3] + "y"
    if _PLURAL_ES.search(word):  # potatoes -> potato, peaches -> peach
        return word[:-2]
    if _KEEP_TRAILING_S.search(word):
        return word
    return word[:-1]


def tokenize(text: str) -> frozenset[str]:
    """Normalized, singularized, stopword-free tokens."""
    return frozenset(_ordered_tokens(text))


def _ordered_tokens(text: str) -> list[str]:
    words = (_singular(w) for w in normalize(text).split())
    return [w for w in words if w and w not in _STOPWORDS and len(w) > 1]


def _head_noun(query: str) -> str | None:
    """The last significant word, which in English carries the product.

    "frozen pizza" is a pizza; "chicken breast" is a breast. Single-word
    queries have no head to distinguish, so they return None.
    """
    tokens = _ordered_tokens(query)
    return tokens[-1] if len(tokens) > 1 else None


@dataclass(frozen=True)
class FlyerOffer:
    """One flyer row: the products it covers, and the promotions it carries."""

    flyer_item_id: int | None
    name: str
    promotions: tuple[Promotion, ...]

    #: The whole row plus each alternative it lists, each as a token set. A
    #: query is scored against all of them and the best one wins, so
    #: "batteries" matches "Duracell Coppertop AA or AAA Batteries" through the
    #: whole row while "AAA" matches through a fragment.
    terms: tuple[tuple[str, frozenset[str]], ...]

    #: True for rows like "H-E-B Mi Tienda Items" that name a brand family
    #: rather than a single product. They are still matchable -- "HUGGIES
    #: Diapers, Wipes, Pull-Ups, or Goodnites Items" is a brand family that a
    #: shopper really does ask for by name -- but the flag lets a caller treat
    #: them as the basket-offer qualifiers they usually are.
    is_brand_family: bool

    @property
    def savings_need_a_price(self) -> bool:
        """Whether valuing this offer requires a base price the ad never states.

        A percentage, a basket threshold or a buy-one-get-one is meaningless
        without knowing what the item costs. This is how many of the flyer's
        offers are unusable until a price book exists.
        """
        needs = {PromoKind.SAVE_PERCENT, PromoKind.BASKET_THRESHOLD, PromoKind.BOGO}
        for promo in self.promotions:
            if promo.unit_price is not None:
                return False
            if promo.kind in needs:
                return True
            if promo.kind is PromoKind.BUNDLE and promo.min_spend is not None:
                return True
        return False


@dataclass(frozen=True)
class Match:
    """One flyer row proposed for one list item, with the reasoning attached."""

    query: str
    offer: FlyerOffer
    #: The alternative within the row that matched best.
    term: str
    #: Fraction of the shopper's words found in the row. 1.0 = all of them.
    score: float
    matched: frozenset[str]
    missing: frozenset[str]
    #: Words the row adds that the shopper did not ask for. High values mean a
    #: broader or different product -- "tortillas" against "Tortilla Chips".
    extra: frozenset[str]

    @property
    def is_exact(self) -> bool:
        return self.score >= 1.0 and not self.extra


def _terms(name: str) -> tuple[tuple[str, frozenset[str]], ...]:
    """The whole row, plus each alternative it lists."""
    seen: dict[frozenset[str], str] = {}
    candidates = [name, *(_SPLIT.split(name))]
    for candidate in candidates:
        candidate = candidate.strip(" ,.-")
        tokens = tokenize(candidate)
        if tokens and tokens not in seen:
            seen[tokens] = candidate
    return tuple((text, tokens) for tokens, text in seen.items())


def build_offers(flyer_items: Sequence[dict[str, Any]]) -> list[FlyerOffer]:
    """Turn raw Flipp flyer records into matchable offers.

    Rows carrying no promotion are dropped -- there is nothing to optimize
    about them.
    """
    offers: list[FlyerOffer] = []
    for item in flyer_items:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        promotions = tuple(parse_item(item))
        if not promotions:
            continue
        terms = _terms(name)
        if not terms:
            continue
        offers.append(
            FlyerOffer(
                flyer_item_id=item.get("id"),
                name=name,
                promotions=promotions,
                terms=terms,
                is_brand_family=bool(_BRAND_FAMILY.search(name)),
            )
        )
    return offers


def match_query(
    query: str,
    offers: Iterable[FlyerOffer],
    *,
    min_score: float = 0.5,
    require_head_noun: bool = True,
) -> list[Match]:
    """Candidate flyer rows for one list item, best first.

    `min_score` is the fraction of the shopper's own words that must appear.

    `require_head_noun` additionally demands that the *last* word of a
    multi-word query appear, unless every word matched. Without it "frozen
    pizza" scores 0.5 against "H-E-B Frozen Fully Cooked Meatballs" and against
    "Moontail Seafood Frozen Tilapia Fillets" -- the shared word is the
    adjective, and the shopper is not being offered pizza. The
    every-word-matched escape keeps unusual word order ("yogurt, greek")
    working.
    """
    wanted = tokenize(query)
    if not wanted:
        return []
    head = _head_noun(query)

    matches: list[Match] = []
    for offer in offers:
        best: Match | None = None
        for text, tokens in offer.terms:
            matched = wanted & tokens
            if not matched:
                continue
            score = len(matched) / len(wanted)
            candidate = Match(
                query=query,
                offer=offer,
                term=text,
                score=score,
                matched=matched,
                missing=wanted - tokens,
                extra=tokens - wanted,
            )
            # Prefer a higher score, then the tighter term -- a fragment that
            # matches as well as the whole row is the more precise answer.
            if best is None or (score, -len(candidate.extra)) > (
                best.score,
                -len(best.extra),
            ):
                best = candidate
        if best is None or best.score < min_score:
            continue
        if require_head_noun and head is not None and best.score < 1.0:
            if head not in best.matched:
                continue
        matches.append(best)

    matches.sort(key=lambda m: (-m.score, len(m.extra), m.offer.name))
    return matches


def match_list(
    items: Sequence[str],
    offers: Iterable[FlyerOffer],
    *,
    min_score: float = 0.5,
) -> dict[str, list[Match]]:
    """Resolve a whole grocery list. Items with no deal map to an empty list.

    An empty list is a real answer, not a failure: most of a weekly shop is not
    in the circular, and saying so is more useful than a bad match.
    """
    offers = list(offers)
    return {item: match_query(item, offers, min_score=min_score) for item in items}
