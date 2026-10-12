import pytest

from dealforge.providers.cvs.catalog import (
    clean_eligible_products, eligible_products, parse_product_detail, parse_products,
    parse_stores, price_observation, product_identity, rank_products, store_search_observed,
)

URL = "https://www.cvs.com/shop/example-soap-prodid-100?skuId=200"


@pytest.mark.parametrize("summary,expected", [
    ('Only show stores with pickup 5 results for 00000 5 results for "00000"', True),
    ('5 results for “00000”', True),
    ('5 results for 00000 and 3 results for 00001', False),
    ('5 results for 000000', False),
])
def test_store_zip_summary_handles_aggregated_text_without_mixing_zips(summary, expected):
    import json
    snapshot = 'uid=1 dialog "Change your CVS Store"\n  uid=2 StaticText ' + json.dumps(summary)
    assert store_search_observed(snapshot,"00000") is expected

CARDS = '''uid=1 main
  uid=2 paragraph "Results for Example soap"
  uid=3 generic
    uid=4 link "Example Soap, 4 OZ" url="https://www.cvs.com/shop/example-soap-prodid-100?skuId=200"
      uid=5 StaticText "Example Soap, 4 OZ"
    uid=6 StaticText "$3.49"
    uid=7 StaticText "$0.87/oz"
    uid=8 button "Add one Example Soap to cart."
  uid=9 generic
    uid=10 link "Other Soap, 8 OZ" url="https://www.cvs.com/shop/other-soap-prodid-101?skuId=201"
      uid=11 StaticText "Other Soap, 8 OZ"
    uid=12 StaticText "$5.99"
'''


@pytest.mark.parametrize("url", ["https://evil.test/shop/soap-prodid-100", "https://www.cvs.com.evil.test/shop/soap-prodid-100",
    "http://www.cvs.com/shop/soap-prodid-100", "https://secret@www.cvs.com/shop/soap-prodid-100",
    "https://www.cvs.com:9222/shop/soap-prodid-100", "https://www.cvs.com/shop/soap-prodid-100?skuId=1&skuId=2",
    "https://www.cvs.com/shop/soap-prodid-100?skuId=SECRET", "https://www.cvs.com/shop/deals/100"])
def test_product_url_boundary_rejects_untrusted_or_ambiguous_identity(url):
    assert product_identity(url) is None


def test_product_identity_strips_tracking_and_keeps_family_separate_from_sku():
    assert product_identity(URL + "&session=SECRET#reviews") == {"product_id": "100", "sku_id": "200", "url": URL}
    assert product_identity(URL.split("?")[0])["sku_id"] is None


def test_card_local_prices_do_not_leak_between_products_and_are_not_shelf_prices():
    products = parse_products(CARDS)
    assert [(p["sku_id"], p["price"]["amount"]) for p in products] == [("200", "3.49"), ("201", "5.99")]
    assert all(p["price"]["channel"] == "online" and p["in_store_price"] is None for p in products)


def test_ads_header_and_footer_prices_are_excluded():
    snapshot = 'uid=0 banner\n  uid=0_1 StaticText "$10.63"\n' + CARDS.replace('  uid=3 generic',
        '  uid=ad Iframe "Advertisement"\n    uid=ad_1 StaticText "$0.01"\n    uid=ad_2 link "Ad" url="https://www.cvs.com/shop/ad-prodid-900"\n  uid=3 generic')
    snapshot += '\nuid=f contentinfo\n  uid=f_1 link "Footer" url="https://www.cvs.com/shop/footer-prodid-901"\n  uid=f_2 StaticText "$100"'
    assert [p["sku_id"] for p in parse_products(snapshot)] == ["200", "201"]


def test_missing_or_conflicting_price_never_becomes_zero_or_cheapest():
    assert price_observation(["Save $2", "$1/oz"])["amount"] is None
    assert price_observation(["$2.99", "$4.99"])["amount"] is None
    assert parse_products(CARDS.replace('    uid=6 StaticText "$3.49"', ''))[0]["price"]["amount"] is None


def test_text_matches_expose_brand_and_size_gaps_without_claiming_exact_sku():
    products = rank_products(parse_products(CARDS), "soap", brand="Example", size="4 oz")
    assert products[0]["sku_id"] == "200"
    assert products[0]["match"]["constraint_gaps"] == []
    assert not products[0]["match"]["exact_product_match"]
    assert products[1]["match"]["constraint_gaps"] == ["brand_not_matched", "size_not_matched"]


def test_eligible_carousel_is_scoped_and_never_exhaustive_checkout_proof():
    snapshot = 'uid=1 main\n  uid=2 region "Eligible Products"\n    uid=3 link "Soap" url="' + URL + '"\n  uid=4 region "Recommended"\n    uid=5 link "Other" url="https://www.cvs.com/shop/other-prodid-101"'
    products = eligible_products(snapshot)
    assert len(products) == 1 and products[0]["sku_id"] == "200"
    assert not products[0]["eligibility_checkout_verified"]
    assert clean_eligible_products([{**products[0], "cookie": "SECRET"}]) == products
    assert clean_eligible_products([{**products[0], "sku_id": "wrong"}]) == []


DETAIL = 'uid=0 RootWebArea "Soap" url="' + URL + '''"
  uid=1 main
    uid=2 heading "Example Soap, 4 OZ" level="1"
    uid=3 StaticText "Item #200"
    uid=4 StaticText "$3.49"
    uid=5 region "Recommended"
      uid=6 StaticText "$9.99"
'''


def test_detail_requires_matching_family_and_observed_selected_sku():
    result = parse_product_detail(DETAIL, product_identity(URL))
    assert result["selected_sku_verified"] and result["price"]["amount"] == "3.49"
    assert parse_product_detail(DETAIL, product_identity(URL.replace("prodid-100", "prodid-999"))) is None
    unknown = parse_product_detail(DETAIL.replace('    uid=3 StaticText "Item #200"\n', ''), product_identity(URL))
    assert not unknown["selected_sku_verified"] and not unknown["match"]["exact_product_match"]
    mismatch = parse_product_detail(DETAIL, product_identity(URL.replace("skuId=200", "skuId=999")))
    assert mismatch["price"]["amount"] is None and not mismatch["selected_sku_verified"]
    assert mismatch["sku_id"] == "200" and mismatch["requested_sku_id"] == "999"
    assert product_identity(mismatch["url"])["sku_id"] == mismatch["sku_id"]
    family = parse_product_detail(DETAIL, product_identity(URL.split("?")[0]))
    assert product_identity(family["url"])["sku_id"] == family["sku_id"] == "200"


def test_store_identity_comes_from_store_details_not_zip_or_address_alone():
    snapshot = '''uid=1 banner
  uid=2 button "Current store: 123 EXAMPLE STREET, Open, Edit"
  uid=3 dialog "Change your CVS Store" modal
    uid=4 link "Select details for 123 EXAMPLE STREET" url="https://www.cvs.com/store-locator/city-XX-pharmacies/city-00000/storeid=99"
    uid=5 link "Select details for OTHER STREET" url="https://www.cvs.com/store-locator/city-XX-pharmacies/city-00002/storeid=98"
'''
    stores = parse_stores(snapshot)
    assert [(s["store_id"], s["zip_code"], s["selected"]) for s in stores] == [("99", "00000", True), ("98", "00002", False)]


@pytest.mark.parametrize("marker_role", ["generic", "StaticText"])
def test_flat_search_cards_keep_variant_ranges_and_adjacent_prices_separate(marker_role):
    snapshot = '''uid=1 main
  uid=2 generic "Start of product results"
  uid=3 link "Soap" url="https://www.cvs.com/shop/soap-prodid-100"
  uid=4 link "Soap" url="https://www.cvs.com/shop/soap-prodid-100"
    uid=5 heading "Soap, 4 OZ" level="2"
  uid=6 StaticText "$5.99"
  uid=7 StaticText "to"
  uid=8 StaticText "$9.59"
  uid=9 button "Send to card: Save $3"
  uid=10 StaticText "$3"
  uid=11 StaticText "Out of stock at Example Street"
  uid=12 link "Other" url="https://www.cvs.com/shop/other-prodid-101"
  uid=13 StaticText "$7.49"
'''
    snapshot = snapshot.replace('uid=2 generic "Start of product results"', f'uid=2 {marker_role} "Start of product results"')
    products = parse_products(snapshot)
    assert len(products) == 2
    assert products[0]["price"]["amount"] is None
    assert products[0]["price"]["displayed_price_range"] == {"minimum": "5.99", "maximum": "9.59"}
    assert products[0]["availability"] == "out_of_stock"
    assert products[1]["price"]["amount"] == "7.49"


def test_detail_image_carousel_split_price_details_sku_and_sponsored_price():
    snapshot = 'uid=0 RootWebArea "Soap" url="' + URL + '''"
  uid=1 main
    uid=2 heading "Example Soap, 4 OZ, 1 Pack" level="1"
    uid=3 region "Carousel"
      uid=4 tab "Product image"
    uid=5 StaticText "$"
    uid=6 StaticText "5.99"
    uid=7 StaticText "$1.17/oz."
    uid=8 StaticText " Available coupons"
    uid=9 heading "$2 OFF" level="3"
    uid=10 StaticText "$2"
    uid=11 heading "How to get it" level="2"
    uid=12 button "Pickup Not Selected Out of stock"
    uid=13 heading "Sponsored" level="2"
    uid=14 StaticText "$7.49"
    uid=15 region "You may also like"
      uid=16 StaticText "Item #999"
      uid=17 StaticText "$9.99"
    uid=18 button "Details" expanded
    uid=19 StaticText "4 OZ, 0.01 lbs. Item # 200"
'''
    result = parse_product_detail(snapshot, product_identity(URL))
    assert result["selected_sku_verified"]
    assert result["price"]["amount"] == "5.99"
    assert result["availability"] == "out_of_stock"
    loading = snapshot.replace('    uid=5 StaticText "$"\n    uid=6 StaticText "5.99"', '    uid=5 generic busy')
    assert parse_product_detail(loading, product_identity(URL)) is None
