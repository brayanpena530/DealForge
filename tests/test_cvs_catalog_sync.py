import asyncio
from types import SimpleNamespace

import pytest

from dealforge.providers.cvs.account_sync import AccountCollector
from test_cvs_catalog import CARDS, DETAIL, URL


HEADER = '''uid=0 RootWebArea "Shop" url="https://www.cvs.com/shop"
  uid=1 banner
    uid=2 button "Current store: 123 EXAMPLE STREET, Open, Edit"
    uid=3 search "Search CVS"
      uid=4 combobox "Search CVS or ask a question"
      uid=5 button "Submit Search"
'''
DIALOG = '''    uid=6 dialog "Change your CVS Store" modal
      uid=7 button "Close"
      uid=8 link "Select details for 123 EXAMPLE STREET" url="https://www.cvs.com/store-locator/city-XX-pharmacies/city-00000/storeid=99"
      uid=9 textbox "Search by Zip Code, City, State, or Landmark"
      uid=10 button "Submit Search"
'''


class CatalogBrowser:
    def __init__(self):
        self.calls = []
        self.opened = False
        self.dialog = False
        self.searching = False
        self.query = ""
        self.product = False
        self.url = "https://www.cvs.com/shop"
        self.failed = False

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if self.failed:
            raise RuntimeError("SECRET")
        text = ""
        if name == "list_pages":
            text = "4: Rewards (https://www.cvs.com/extracare/home)\n1: Mail (https://mail.example.test/SECRET)"
            if self.opened:
                text += f"\n9: Catalog ({self.url})"
        elif name == "new_page":
            assert args["background"] and args["url"] == "https://www.cvs.com/shop"
            self.opened = True
        elif name == "navigate_page":
            assert args["pageId"] == 9
            self.url = args["url"]
            self.product = "prodid-" in self.url
            self.searching = self.dialog = False
        elif name == "take_snapshot":
            if self.product:
                text = DETAIL.replace('  uid=1 main', HEADER[HEADER.index('  uid=1 banner'):] + '  uid=1 main')
            else:
                text = HEADER + (DIALOG if self.dialog else "")
                text += CARDS.replace("Example soap", self.query) if self.searching else '  uid=20 main\n    uid=21 heading "Shop" level="1"'
        elif name == "fill":
            assert args["uid"] == "4"
            self.query = args["value"]
        elif name == "click":
            assert args["uid"] in {"2", "7", "5"}
            if args["uid"] == "2":
                self.dialog = True
            elif args["uid"] == "7":
                self.dialog = False
            else:
                self.searching = True
        else:
            raise AssertionError(name)
        return SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text=text)])


def test_search_returns_store_scoped_online_prices_and_preserves_wallet_tab():
    browser = CatalogBrowser()
    result = asyncio.run(AccountCollector(browser, poll_seconds=0).search_products(query="Example soap", brand="Example", size="4 oz"))
    assert result["status"] == "partial" and result["store"]["store_id"] == "99"
    assert result["products"][0]["sku_id"] == "200"
    assert result["products"][0]["price"]["amount"] == "3.49"
    assert result["products"][0]["price"]["observed_at"] == result["observed_at"]
    assert not result["in_store_prices_verified"]
    assert all(args.get("pageId", 9) == 9 for _, args in browser.calls)
    assert not any(name == "get_network_request" for name, _ in browser.calls)


def test_pinned_store_mismatch_does_not_search_or_change_store():
    browser = CatalogBrowser()
    result = asyncio.run(AccountCollector(browser, poll_seconds=0).search_products(query="soap", store_id="98"))
    assert result["reason"] == "requested_store_not_selected" and result["products"] == []
    assert not any(name == "fill" for name, _ in browser.calls)


@pytest.mark.parametrize("options", [{"query": ""}, {"query": "soap", "zip_code": "abc"},
    {"query": "soap", "limit": True}, {"query": "soap", "store_id": "SECRET"},
    {"query": "soap", "brand": "bad\ninput"}])
def test_invalid_search_never_touches_chrome(options):
    browser = CatalogBrowser()
    result = asyncio.run(AccountCollector(browser).search_products(**options))
    assert result["status"] == "invalid_input" and not browser.calls


def test_exact_product_variant_and_price_are_read_without_purchase():
    browser = CatalogBrowser()
    result = asyncio.run(AccountCollector(browser, poll_seconds=0).get_product(product_url=URL))
    assert result["products"][0]["selected_sku_verified"]
    assert result["products"][0]["price"]["amount"] == "3.49"


def test_connection_error_never_echoes_raw_browser_exception():
    browser = CatalogBrowser()
    browser.failed = True
    result = asyncio.run(AccountCollector(browser).search_products(query="soap"))
    assert result["reason"] == "catalog_connection_failed"
    assert "SECRET" not in str(result)


def test_store_discovery_reads_results_without_changing_selected_store():
    class StoresBrowser(CatalogBrowser):
        def __init__(self):
            super().__init__()
            self.found = False
        async def call_tool(self, name, args):
            if name == "fill" and args["uid"] == "9":
                self.calls.append((name, args))
                assert args["value"] == "00000"
                return SimpleNamespace(isError=False, content=[])
            if name == "click" and args["uid"] == "10":
                self.calls.append((name, args))
                self.found = True
                return SimpleNamespace(isError=False, content=[])
            result = await super().call_tool(name, args)
            if name == "take_snapshot" and self.dialog and self.found:
                result.content[0].text = result.content[0].text.replace('      uid=9 textbox',
                    '      uid=other link "Select details for 456 EXAMPLE STREET" url="https://www.cvs.com/store-locator/city-XX-pharmacies/city-00000/storeid=98"\n      uid=9 textbox')
            return result
    browser = StoresBrowser()
    result = asyncio.run(AccountCollector(browser, poll_seconds=0).find_stores())
    assert result["status"] == "ready" and [s["store_id"] for s in result["stores"]] == ["99", "98"]
    assert result["stores"][0]["selected"] and not browser.dialog
    assert not result["store_selection_required"]


def test_store_change_during_lookup_suppresses_prices():
    class ChangedStore(CatalogBrowser):
        async def call_tool(self, name, args):
            result = await super().call_tool(name, args)
            if name == "take_snapshot" and self.searching:
                result.content[0].text = result.content[0].text.replace('Current store: 123 EXAMPLE STREET', 'Current store: OTHER STREET')
            return result
    result = asyncio.run(AccountCollector(ChangedStore(), poll_seconds=0).search_products(query="Example soap"))
    assert result["reason"] == "store_changed_during_lookup" and result["products"] == []


def test_product_waits_for_late_store_header_before_returning_prices():
    class LateHeader(CatalogBrowser):
        loading_snapshots = 0
        async def call_tool(self, name, args):
            result = await super().call_tool(name, args)
            if name == "take_snapshot" and self.product:
                self.loading_snapshots += 1
                if self.loading_snapshots < 3:
                    result.content[0].text = DETAIL
            return result
    browser = LateHeader()
    result = asyncio.run(AccountCollector(browser,poll_seconds=0).get_product(product_url=URL))
    assert browser.loading_snapshots >= 3
    assert result["products"][0]["selected_sku_verified"]
    assert result["products"][0]["price"]["amount"] == "3.49"


def test_store_search_cards_without_ids_return_candidates_instead_of_timing_out():
    class AddressBrowser(CatalogBrowser):
        found = False

        async def call_tool(self, name, args):
            if name == "fill" and args["uid"] == "9":
                return SimpleNamespace(isError=False, content=[])
            if name == "click" and args["uid"] == "10":
                self.found = True
                return SimpleNamespace(isError=False, content=[])
            result = await super().call_tool(name, args)
            if name == "take_snapshot" and self.dialog and self.found:
                result.content[0].text = result.content[0].text.replace('      uid=9 textbox',
                    '      uid=30 StaticText "2 results for 00000"\n'
                    '      uid=31 button "Select store at 456 EXAMPLE STREET"\n      uid=9 textbox')
            return result

    browser = AddressBrowser()
    result = asyncio.run(AccountCollector(browser, poll_seconds=0).find_stores())
    assert result["status"] == "partial" and not browser.dialog
    assert result["stores"][0]["store_id"] == "99"
    candidate = result["stores"][1]
    assert candidate["address"] == "456 EXAMPLE STREET"
    assert candidate["store_id"] is None and candidate["zip_code"] is None
    assert not candidate["identity_verified"] and candidate["search_zip_code"] == "00000"
