"""Browser boundary and collector regressions using synthetic responses only."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dealforge.providers.cvs.account_sync import (
    AccountCollector, HOME, PROFILE_PATH, DEFINITION_PATH, SyncProblem,
    _cvs_url, _detail_links, _network_rows, _visible_count, _rewards_search, _detail_state, ChromeAccountService,
)


@pytest.fixture(autouse=True)
def isolate_local_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "local"))


INVENTORY = '''uid=1_1 main
 uid=1_2 tab "All" selectable selected
 uid=1_3 StaticText "2"
 uid=1_4 StaticText " deals and rewards"
 uid=1_5 heading "$10 ExtraCare Plus bonus" level="3"
 uid=1_6 StaticText "Your monthly reward"
 uid=1_7 link "More details reward" url="https://www.cvs.com/extracare/home#"
 uid=1_8 button "Send to card"
 uid=1_9 heading "$3 off soap" level="3"
 uid=1_10 StaticText "Example soap"
 uid=1_11 link "More details soap" url="https://www.cvs.com/extracare/home#"
 uid=1_12 button "Add to cart"
'''
DETAIL = 'uid=2_1 link "Savings" url="https://www.cvs.com/"\nuid=2_2 StaticText "On card"'
PROFILE = {"statusCode": "0000", "data": {"getEcCustomerProfile": {
    "couponCategories": {
        "extrabucks": [{"cmpgnId": 100, "cpnNbr": 200, "cpnSeqNbr": 12345678901,
                       "cpnDsc": "ExtraCare Plus $10 bonus", "maxRedeemAmt": "10.00",
                       "expirDt": "2099-01-01", "loadableInd": "N", "redeemableInd": "Y"}],
        "personalizedDeals": [{"cmpgnId": 300, "cpnNbr": 400, "cpnDsc": "$3 off soap",
                               "expirDt": "2099-01-01", "loadableInd": "Y", "redeemableInd": "N"}],
        "otherDeals": [],
    },
    "xtraCard": {"xtraCardNbr": "999999999999999"},
    "secret": "SECRET-PROFILE",
}}}
DEFINITION = {"statusCode": "0000", "data": {"extracareCouponDefinition": {
    "extracareCouponDefinitions": [{"cmpgnId": 100, "cpnNbr": 200,
                                    "cpnTermsTxt": "Example qualifying items only."}]}}}


def tool_result(text="", error=False):
    return SimpleNamespace(isError=error, content=[SimpleNamespace(type="text", text=text)])


def test_multi_query_details_share_bounded_visits_and_deduplicate_filters():
    class Collector(AccountCollector):
        def __init__(self, browser):
            super().__init__(browser, poll_seconds=0)
            self.visits = []

        async def _definitions(self, page, limit, deadline, query="", visited=None):
            self.visits.append((query, limit, id(visited)))
            return [], []

    collector = Collector(Browser())
    result = asyncio.run(collector.sync(detail_limit=3, detail_queries=["soap", "reward", "soap"], include_offer_rules=True))
    assert result["snapshot"]
    assert [(q, limit) for q, limit, _ in collector.visits] == [("soap", 2), ("reward", 1)]
    assert len({v[2] for v in collector.visits}) == 1
    assert len(result["offer_rules"]) == 1 and not result["offer_rules"][0]["definition_observed"]


@pytest.mark.parametrize("options", [{"detail_queries": []}, {"detail_queries": ["x"], "detail_query": "x"},
                                      {"detail_queries": ["x"] * 11}, {"detail_queries": ["\n"]},
                                      {"include_offer_rules": "yes"}])
def test_invalid_multi_query_options_do_not_call_browser(options):
    browser = Browser()
    assert asyncio.run(AccountCollector(browser).sync(**options))["status"] == "invalid_input"
    assert not browser.calls


class Browser:
    def __init__(self):
        self.calls = []
        self.url = HOME
        self.more_pages = False
        self.loaded = False
        self.detail = False
        self.profile_id = 10
        self.definition_id = 20
        self.profile = deepcopy(PROFILE)
        self.snapshot = INVENTORY
        self.code = 200
        self.exports = []
        self.no_fresh_request = False
        self.failed = False
        self.removed_after_navigation = False

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.failed:
            raise RuntimeError("SECRET-BROWSER-EXCEPTION")
        if name == "list_pages":
            lines = f"1: Unrelated (https://mail.example.test/SECRET-EMAIL)\n4: CVS ({self.url})"
            if self.more_pages:
                lines += f"\n7: Other CVS ({HOME})"
            if self.removed_after_navigation and self.loaded:
                lines = "1: Unrelated (https://mail.example.test/)"
            return tool_result(lines)
        if name == "navigate_page":
            assert arguments["url"] == HOME
            self.loaded = True
            self.detail = False
            if not self.no_fresh_request:
                self.profile_id += 1
            return tool_result("success")
        if name == "take_snapshot":
            return tool_result(DETAIL if self.detail else self.snapshot)
        if name == "list_network_requests":
            rows = f"reqid={self.profile_id} POST https://www.cvs.com{PROFILE_PATH} [{self.code}]"
            if self.detail or self.definition_id > 20:
                rows += f"\nreqid={self.definition_id} POST https://www.cvs.com{DEFINITION_PATH} [200]"
            rows += "\nreqid=99 GET https://mail.example.test/secret [200]"
            return tool_result(rows)
        if name == "get_network_request":
            file = Path(arguments["responseFilePath"])
            self.exports.append(file)
            payload = self.profile if arguments["reqid"] == self.profile_id else DEFINITION
            file.write_text(json.dumps(payload), encoding="utf-8")
            Path(arguments["requestFilePath"]).write_text("SECRET-REQUEST", encoding="utf-8")
            return tool_result("SECRET-COOKIE SECRET-HEADER")
        if name == "click":
            # Only observed read-only links are ever touched.
            assert arguments["uid"] in {"1_7", "1_11", "2_1"}
            if arguments["uid"] == "2_1":
                self.detail = False
            else:
                self.detail = True
                self.definition_id += 1
            return tool_result("success")
        if name == "press_key":
            assert arguments["key"] == "End"
            return tool_result("success")
        raise AssertionError(name)


def sync(browser, **options):
    return asyncio.run(AccountCollector(browser, poll_seconds=0).sync(**options))


def test_live_inventory_reconciles_and_drops_private_data_and_temp_exports():
    browser = Browser()
    result = sync(browser)
    assert result["status"] == "partial" and result["live_sync_available"]
    assert result["collection"]["visible_count_matches"]
    assert result["snapshot"]["rewards_complete"]
    assert result["definition_coverage"] == {"requested_limit": 0, "matched_records": 0, "missing_records": 2,
                                            "cached_records": 0}
    assert result["wallet"]["known_unexpired_face_value_by_kind"] == {"membership_bonus": "10.00"}
    output = json.dumps(result)
    assert "SECRET" not in output and "999999999999999" not in output and "12345678901" not in output
    assert not any(file.parent.exists() for file in browser.exports)
    assert not any(name == "click" for name, _ in browser.calls)


def test_details_join_source_identity_without_activation_and_leave_inventory():
    browser = Browser()
    result = sync(browser, detail_limit=1)
    assert result["definition_coverage"]["matched_records"] == 1
    assert "qualifying items only" in result["snapshot"]["rewards"][0]["raw_terms"]
    assert not browser.detail
    assert result["terms_complete"] is False


def test_detail_query_visits_only_matching_cards():
    browser = Browser()
    result = sync(browser, detail_limit=1, detail_query="soap")
    clicked = [args["uid"] for name, args in browser.calls if name == "click"]
    assert clicked == ["1_11", "2_1"]
    assert result["collection"]["visible_count_matches"]


def test_missing_detail_query_match_stops_after_bounded_scrolls():
    browser = Browser()
    result = sync(browser, detail_limit=1, detail_query="unmatched brand")
    assert "detail_links_exhausted" in result["review"]
    assert len([name for name, _ in browser.calls if name == "press_key"]) == 3
    assert not any(name == "click" for name, _ in browser.calls)


def test_repeated_syncs_refresh_and_keep_ids_stable():
    async def run():
        browser = Browser()
        collector = AccountCollector(browser, poll_seconds=0)
        first = await collector.sync()
        second = await collector.sync()
        assert first["snapshot"]["rewards"][0]["reward_id"] == second["snapshot"]["rewards"][0]["reward_id"]
        assert len([c for c in browser.calls if c[0] == "navigate_page"]) == 2
    asyncio.run(run())


@pytest.mark.parametrize("code,status", [(401, "needs_login"), (403, "blocked_access")])
def test_denied_profile_never_returns_cached_balance(code, status):
    browser = Browser()
    browser.code = code
    result = sync(browser)
    assert result["status"] == status and result["snapshot"] is None
    assert not browser.exports


@pytest.mark.parametrize("url,text,status", [
    ("https://www.cvs.com/account/login", 'textbox "Password"', "needs_login"),
    (HOME, 'StaticText "Problem with your network address"', "blocked_access"),
])
def test_login_and_access_pages_are_reported(url, text, status):
    browser = Browser()
    browser.url, browser.snapshot = url, text
    result = sync(browser, page_id=4)
    assert result["status"] == status and result["snapshot"] is None


def test_ambiguous_tabs_need_input_without_touching_either():
    browser = Browser()
    browser.more_pages = True
    result = sync(browser)
    assert result["status"] == "needs_input"
    assert not any(name != "list_pages" for name, _ in browser.calls)
    assert sync(browser, page_id=7)["collection"]["visible_count_matches"]


def test_closed_or_changed_tab_stops_capture():
    browser = Browser()
    browser.removed_after_navigation = True
    result = sync(browser)
    assert result["reason"] == "cvs_tab_closed_or_changed"
    assert not browser.exports


def test_connection_exception_is_not_echoed():
    browser = Browser()
    browser.failed = True
    result = sync(browser)
    assert result["status"] == "browser_unavailable"
    assert "SECRET" not in json.dumps(result)


def test_count_mismatch_keeps_collection_incomplete():
    browser = Browser()
    browser.snapshot = INVENTORY.replace('StaticText "2"', 'StaticText "3"')
    result = sync(browser)
    assert not result["snapshot"]["coupons_complete"]
    assert not result["collection"]["visible_count_matches"]


@pytest.mark.parametrize("options", [{"detail_limit": 26}, {"detail_limit": True},
    {"timeout_seconds": 0}, {"page_id": True}, {"account_ref": "999999999999999"},
    {"account_ref": "secret@example.test"}])
def test_invalid_options_do_not_touch_browser(options):
    browser = Browser()
    result = sync(browser, **options)
    assert result["status"] == "invalid_input"
    assert not browser.calls


@pytest.mark.parametrize("url", ["https://www.cvs.com.evil.test/extracare/home",
    "http://www.cvs.com/", "https://user:password@www.cvs.com/", "https://www.cvs.com:9222/",
    "https://mail.example.test/"])
def test_domain_guard_excludes_other_surfaces(url):
    assert not _cvs_url(url)


def test_count_requires_all_selected_and_a_unique_count():
    assert _visible_count(INVENTORY) == 2
    assert _visible_count(INVENTORY.replace('selectable selected', 'selectable')) is None
    assert _visible_count(INVENTORY + '\nStaticText "99 deals and rewards"') is None


def test_network_filter_never_selects_lookalike_hosts_or_other_account_routes():
    text = (f"reqid=1 POST https://www.cvs.com{PROFILE_PATH} [200]\n"
            f"reqid=2 POST https://www.cvs.com.evil.test{PROFILE_PATH} [200]\n"
            "reqid=3 POST https://www.cvs.com/retail/client/experience/v2/load/secret [200]")
    assert _network_rows(text, PROFILE_PATH) == [(1, 200)]


def test_detail_link_filter_excludes_buttons_and_external_links():
    links = _detail_links(INVENTORY + '\nuid=1_99 link "More details" url="https://evil.test/"')
    assert [uid for uid, _ in links] == ["1_7", "1_11"]


def test_interrupted_definition_collection_preserves_inventory_with_review():
    class InterruptedCollector(AccountCollector):
        async def _definitions(self, page, limit, deadline, query=""):
            raise SyncProblem("browser_unavailable", "browser_tool_failed", "retry")
    result = asyncio.run(InterruptedCollector(Browser(), poll_seconds=0).sync(detail_limit=1))
    assert result["snapshot"]["rewards_complete"]
    assert "browser_tool_failed" in result["review"]


def test_login_lost_during_details_suppresses_account_result():
    class InterruptedCollector(AccountCollector):
        async def _definitions(self, page, limit, deadline, query=""):
            raise SyncProblem("needs_login", "cvs_sign_in_required", "sign in")
    result = asyncio.run(InterruptedCollector(Browser(), poll_seconds=0).sync(detail_limit=1))
    assert result["status"] == "needs_login" and result["snapshot"] is None


def test_sync_serializes_calls_sharing_the_browser():
    class SlowBrowser(Browser):
        def __init__(self):
            super().__init__()
            self.active = False
        async def call_tool(self, name, arguments):
            if name == "navigate_page":
                assert not self.active
                self.active = True
                await asyncio.sleep(0.01)
            result = await super().call_tool(name, arguments)
            if name == "get_network_request":
                self.active = False
            return result
    async def run():
        browser = SlowBrowser()
        collector = AccountCollector(browser, poll_seconds=0)
        results = await asyncio.gather(collector.sync(), collector.sync())
        assert all(r["collection"]["visible_count_matches"] for r in results)
    asyncio.run(run())


def test_fresh_profile_is_required_even_if_previous_inventory_is_visible():
    class FastDeadlineCollector(AccountCollector):
        async def _wait_inventory(self, page, deadline, old_ids):
            # Exercise the real wait logic with a short deadline without making
            # the regression take the CLI's minimum ten seconds.
            return await super()._wait_inventory(page, asyncio.get_running_loop().time() + 0.01, old_ids)
    browser = Browser()
    browser.no_fresh_request = True
    result = asyncio.run(FastDeadlineCollector(browser, poll_seconds=0).sync())
    assert result["reason"] == "fresh_inventory_not_observed"
    assert result["snapshot"] is None and not browser.exports


def test_network_pagination_reaches_profile_on_second_list_page():
    class PaginatedBrowser(Browser):
        async def call_tool(self, name, arguments):
            if name == "list_network_requests" and arguments["pageIdx"] == 0:
                self.calls.append((name, arguments))
                return tool_result("reqid=99 GET https://mail.example.test/ [200]\nNext page: 1")
            return await super().call_tool(name, arguments)
    result = sync(PaginatedBrowser())
    assert result["collection"]["visible_count_matches"]


def test_timeout_returns_no_previous_wallet_and_does_not_echo_errors(monkeypatch):
    from dealforge.providers.cvs import account_sync
    real_limit = account_sync._time_limit
    monkeypatch.setattr(account_sync, "_time_limit", lambda seconds: real_limit(0.01))
    class HangingBrowser(Browser):
        async def call_tool(self, name, arguments):
            await asyncio.sleep(10)
    result = sync(HangingBrowser())
    assert result["reason"] == "sync_timed_out" and result["snapshot"] is None


def test_timeout_during_details_preserves_inventory_and_can_sync_again():
    class TimeoutCollector(AccountCollector):
        async def _definitions(self, page, limit, deadline, query=""):
            raise asyncio.TimeoutError
    async def run():
        collector = TimeoutCollector(Browser(), poll_seconds=0)
        result = await collector.sync(detail_limit=1)
        assert result["reason"] == "sync_time_budget_reached"
        assert result["definition_coverage"]["missing_records"] == 2
        assert result["snapshot"]["rewards_complete"]
        assert (await collector.sync())["collection"]["visible_count_matches"]
    asyncio.run(run())


def test_sync_does_not_duplicate_inventory_in_wallet_summary():
    result = sync(Browser())
    assert "account" not in result["wallet"]
    assert result["wallet"]["warning_counts"]


def test_schema_change_does_not_produce_empty_successful_wallet():
    browser = Browser()
    browser.profile = {"statusCode": "0000", "data": {"SECRET-ACCOUNT": {}}}
    result = sync(browser)
    assert result["reason"] == "response_schema_changed"
    assert result["snapshot"] is None and "SECRET" not in json.dumps(result)


def test_malformed_definition_retains_valid_inventory(monkeypatch):
    monkeypatch.setitem(DEFINITION, "data", None)
    result = sync(Browser(), detail_limit=1)
    assert result["snapshot"]["rewards_complete"]
    assert "selected_definition_unavailable" in result["review"]
    assert result["definition_coverage"]["matched_records"] == 0


def test_account_binding_detects_a_different_card_before_returning_inventory():
    async def run():
        browser = Browser()
        collector = AccountCollector(browser, poll_seconds=0)
        first = await collector.sync()
        assert first["account_binding"]["status"] == "first_seen"
        assert (await collector.sync())["account_identity_verified"]
        browser.profile["data"]["getEcCustomerProfile"]["xtraCard"]["xtraCardNbr"] = "888888888888888"
        changed = await collector.sync()
        assert changed["reason"] == "account_changed_for_label" and changed["snapshot"] is None
    asyncio.run(run())


def test_definition_cache_never_replaces_fresh_card_state(monkeypatch):
    monkeypatch.setitem(DEFINITION["data"]["extracareCouponDefinition"]["extracareCouponDefinitions"][0], "activeInd", False)
    async def run():
        browser = Browser()
        collector = AccountCollector(browser, poll_seconds=0)
        fresh = await collector.sync(detail_limit=1)
        assert fresh["definition_coverage"]["cached_records"] == 0
        cached = await collector.sync()
        assert cached["definition_coverage"]["cached_records"] == 1
        assert cached["snapshot"]["rewards"][0]["state"] == "on_card"
        browser.profile["data"]["getEcCustomerProfile"]["couponCategories"]["extrabucks"][0].update(
            loadableInd="Y", redeemableInd="N")
        refreshed = await collector.sync()
        assert refreshed["snapshot"]["rewards"][0]["state"] == "available"
        assert refreshed["definition_coverage"]["cached_records"] == 1
    asyncio.run(run())


def test_search_scope_and_detail_state_exclude_header_and_product_controls():
    snapshot = '''uid=1 banner
  uid=2 search
    uid=3 textbox
    uid=4 button "Submit search"
uid=5 main
  uid=6 search
    uid=7 textbox
    uid=8 button "Submit search"
  uid=9 heading "Savings"
'''
    assert _rewards_search(snapshot) == ("7", "8")
    assert _rewards_search(snapshot.split('uid=5 main')[0]) is None
    detail = 'uid=1 main\n uid=2 button "Send to card offer"\n uid=3 region "Eligible Products"\n uid=4 StaticText "On card"'
    assert _detail_state(detail) == "available"
    assert _detail_state(detail.replace('uid=2 button "Send to card offer"', 'uid=2 StaticText "On card"')) == "on_card"
    assert _detail_state(detail.replace('uid=3 region', 'uid=9 StaticText "On card"\n uid=3 region')) is None


def test_native_search_reaches_a_coupon_outside_initial_cards_and_preserves_inventory_count():
    controls = 'uid=9_0 main\n uid=9_1 search\n  uid=9_2 textbox\n  uid=9_3 button "Submit search"\n'
    class SearchBrowser(Browser):
        async def call_tool(self, name, arguments):
            if name == "fill":
                self.calls.append((name, arguments))
                assert arguments["uid"] == "9_2" and arguments["value"] == "soap"
                return tool_result()
            if name == "click" and arguments["uid"] == "9_3":
                self.calls.append((name, arguments))
                self.snapshot = controls + INVENTORY[INVENTORY.index(' uid=1_9 heading'):]
                # Filtered count is deliberately different from full inventory.
                self.snapshot += 'uid=8 tab "All" selected\nuid=9 StaticText "1 deals and rewards"'
                return tool_result()
            if name == "take_snapshot" and not self.detail and not self.snapshot.startswith(controls):
                self.snapshot = controls + INVENTORY.split(' uid=1_9 heading')[0]
            return await super().call_tool(name, arguments)
    browser = SearchBrowser()
    result = sync(browser, detail_query="soap", detail_limit=1)
    assert result["collection"]["reported_visible_count"] == 2
    assert result["collection"]["visible_count_matches"]
    assert any(name == "click" and args["uid"] == "1_11" for name, args in browser.calls)


def test_bridge_reconnects_on_next_request_and_contexts_stay_in_owner_task():
    from contextlib import asynccontextmanager
    async def run():
        opened = []
        @asynccontextmanager
        async def factory():
            owner = asyncio.current_task()
            opened.append(owner)
            index = len(opened)
            class Collector:
                async def sync(self, **options):
                    assert asyncio.current_task() is owner
                    return {"status": "browser_unavailable" if index == 1 else "partial"}
            try:
                yield Collector()
            finally:
                assert asyncio.current_task() is owner
        service = await ChromeAccountService(factory).start()
        try:
            assert not opened  # Tool discovery need not connect Chrome.
            assert (await service.sync())["status"] == "browser_unavailable"
            assert (await service.sync())["status"] == "partial"
            assert (await service.sync())["status"] == "partial"
            assert len(opened) == 2
        finally:
            await service.close()
    asyncio.run(run())


def test_bridge_startup_error_is_safe_and_retryable():
    from contextlib import asynccontextmanager
    async def run():
        @asynccontextmanager
        async def broken():
            raise RuntimeError("SECRET-STARTUP")
            yield
        service = await ChromeAccountService(broken).start()
        try:
            for _ in range(2):
                result = await service.sync()
                assert result["reason"] == "browser_bridge_unavailable"
                assert "SECRET" not in json.dumps(result)
        finally:
            await service.close()
    asyncio.run(run())


def test_cancelled_queued_request_does_not_touch_browser():
    from contextlib import asynccontextmanager
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []
        @asynccontextmanager
        async def factory():
            class Collector:
                async def sync(self, **options):
                    calls.append(options)
                    entered.set()
                    await release.wait()
                    return {"status": "partial"}
            yield Collector()
        service = await ChromeAccountService(factory).start()
        try:
            first = asyncio.create_task(service.sync(account_ref="first"))
            await entered.wait()
            second = asyncio.create_task(service.sync(account_ref="cancelled"))
            await asyncio.sleep(0)
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            release.set()
            await first
            await service.sync(account_ref="last")
            assert calls == [{"account_ref": "first"}, {"account_ref": "last"}]
        finally:
            await service.close()
    asyncio.run(run())


def test_account_switch_during_details_suppresses_snapshot_and_cache():
    class SwitchCollector(AccountCollector):
        async def _definitions(self, page, limit, deadline, query=""):
            self.session.profile_id += 1
            self.session.profile["data"]["getEcCustomerProfile"]["xtraCard"]["xtraCardNbr"] = "888888888888888"
            return DEFINITION["data"]["extracareCouponDefinition"]["extracareCouponDefinitions"], []
    collector = SwitchCollector(Browser(), poll_seconds=0)
    result = asyncio.run(collector.sync(detail_limit=1))
    assert result["reason"] == "account_changed_for_label" and result["snapshot"] is None
    assert not list(collector.state.directory.glob("definitions/*.json"))
