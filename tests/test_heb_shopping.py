"""Tests for the shopping-list resolver (offline, fake client)."""

from __future__ import annotations

from decimal import Decimal

from dealforge.providers.heb.schemas import ProductDetail, ProductResult
from dealforge.providers.heb.shopping import best_match, resolve_list


def make_product(name: str, location: str | None = None) -> ProductResult:
    slug = name.lower().replace(" ", "-")
    return ProductResult(
        item_id="1",
        name=name,
        url=f"https://www.heb.com/product-detail/{slug}/1",
        price=Decimal("2.00"),
        location=location,
    )


class FakeClient:
    """Minimal HEBBrowserClient double."""

    def __init__(self, results_by_query: dict[str, list[ProductResult]]) -> None:
        self._results = results_by_query
        self.detail_locations: dict[str, str | None] = {}
        self.detail_calls: list[str] = []

    def search(self, query: str, limit: int = 10) -> list[ProductResult]:
        return self._results.get(query, [])[:limit]

    def get_product_by_url(self, url: str) -> ProductDetail:
        self.detail_calls.append(url)
        return ProductDetail(
            item_id="1", name="x", url=url, location=self.detail_locations.get(url)
        )


# ---------------------------------------------------------------------------


def test_best_match_picks_best_token_overlap():
    results = [
        make_product("H-E-B Tortilla Chips 11 oz"),
        make_product("H-E-B Grade AA Cage Free Extra Large Brown Eggs 18 ct"),
    ]
    best = best_match("eggs", results)
    assert best is results[1]


def test_best_match_none_below_min_score():
    results = [make_product("H-E-B Tortilla Chips 11 oz")]
    assert best_match("eggs", results) is None


def test_best_match_empty_results():
    assert best_match("eggs", []) is None


def test_resolve_list_uses_search_location():
    product = make_product("Dole Bananas", "In Produce, A3")
    client = FakeClient({"bananas": [product]})
    (item,) = resolve_list(["bananas"], client)
    assert item.query == "bananas"
    assert item.product is product
    assert item.location is not None
    assert item.location.zone == "Produce"
    assert item.location.aisle_code == "A3"
    assert item.unplaced is False
    assert client.detail_calls == []  # no product-page fallback needed


def test_resolve_list_falls_back_to_product_page():
    product = make_product("H-E-B Salsa Medium 16 oz", location=None)
    client = FakeClient({"salsa": [product]})
    client.detail_locations[product.url] = "In Produce, B4"
    (item,) = resolve_list(["salsa"], client)
    assert client.detail_calls == [product.url]
    assert item.location is not None
    assert item.location.aisle_code == "B4"
    assert item.unplaced is False


def test_resolve_list_unplaced_when_no_match():
    client = FakeClient({"unicorn horns": []})
    (item,) = resolve_list(["unicorn horns"], client)
    assert item.product is None
    assert item.location is None
    assert item.unplaced is True


def test_resolve_list_unplaced_when_location_unparseable():
    product = make_product("Mystery Item", "Somewhere over there")
    client = FakeClient({"mystery": [product]})
    (item,) = resolve_list(["mystery"], client)
    assert item.location is None
    assert item.unplaced is True


def test_resolve_list_preserves_query_order():
    client = FakeClient(
        {
            "bananas": [make_product("Dole Bananas", "In Produce, A3")],
            "eggs": [make_product("H-E-B Large White Eggs 12 ct", "In Dairy, A24")],
        }
    )
    items = resolve_list(["eggs", "bananas"], client)
    assert [i.query for i in items] == ["eggs", "bananas"]
