"""CVS transport boundaries and synthetic session reuse; no live account data."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("yaml")
from dealforge.providers.cvs.account_state import AccountState
from dealforge.providers.cvs.account_sync import HOME, PROFILE_PATH, DEFINITION_PATH, SyncProblem, _visible_count
from dealforge.providers.cvs.browser_backend import browser_backend, configured_collector
from dealforge.providers.cvs.catalog import nodes, eligible_products
from dealforge.providers.cvs.playwright_browser import (
    BrowserSettings, PlaywrightCollector, PlaywrightSession, aria_nodes, profile_lock,
)
from test_cvs_account_sync import PROFILE


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "local"))
    for key in ("DEALFORGE_CVS_PROFILE", "DEALFORGE_CVS_CHANNEL", "DEALFORGE_CVS_HEADLESS", "DEALFORGE_CVS_BROWSER", "DEALFORGE_CVS_LAUNCH"):
        monkeypatch.delenv(key, raising=False)


def test_profile_is_separate_and_interactive_login_overrides_headless(monkeypatch):
    monkeypatch.setenv("DEALFORGE_CVS_HEADLESS", "true")
    settings = BrowserSettings.from_environment()
    assert settings.profile.name == "playwright-profile" and settings.headless
    assert not BrowserSettings.from_environment(interactive=True).headless
    assert Path.cwd() not in settings.profile.parents


@pytest.mark.parametrize("key,value", [
    ("DEALFORGE_CVS_PROFILE", ".auth"),
    ("DEALFORGE_CVS_CHANNEL", "stealth"),
    ("DEALFORGE_CVS_HEADLESS", "yes"),
    ("DEALFORGE_CVS_LAUNCH", "invalid"),
])
def test_invalid_profile_and_browser_settings_fail_closed(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        BrowserSettings.from_environment()


def test_personal_chrome_profile_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("DEALFORGE_CVS_PROFILE", str(tmp_path / "local" / "Google" / "Chrome" / "User Data" / "Default"))
    with pytest.raises(ValueError):
        BrowserSettings.from_environment()


def test_native_mode_requires_chrome(monkeypatch):
    monkeypatch.setenv("DEALFORGE_CVS_LAUNCH", "native_cdp")
    assert BrowserSettings.from_environment().launch_mode == "native_cdp"
    monkeypatch.setenv("DEALFORGE_CVS_CHANNEL", "chromium")
    with pytest.raises(ValueError):
        BrowserSettings.from_environment()


def test_native_browser_uses_separate_profile_and_loopback_without_automation_flags(tmp_path):
    from dealforge.providers.cvs.native_browser import native_arguments
    args = native_arguments(tmp_path/"profile", 45678)
    assert f"--user-data-dir={tmp_path/'profile'}" in args
    assert "--remote-debugging-address=127.0.0.1" in args
    assert "--remote-debugging-port=45678" in args
    assert "--enable-automation" not in args
    assert not any("password" in arg or "disable-web-security" in arg for arg in args)


@pytest.mark.parametrize("port", [0, 65536, "9222"])
def test_native_browser_rejects_invalid_debugging_ports(tmp_path, port):
    from dealforge.providers.cvs.native_browser import native_arguments
    with pytest.raises(ValueError):
        native_arguments(tmp_path/"profile", port)


def test_profile_lock_blocks_parallel_tasks_and_releases_after_exit(tmp_path):
    async def run():
        async with profile_lock(tmp_path / "profile"):
            with pytest.raises(SyncProblem, match="playwright_profile_in_use"):
                async with profile_lock(tmp_path / "profile"):
                    pass
        async with profile_lock(tmp_path / "profile"):
            pass
    asyncio.run(run())


def test_backend_selection_is_explicit_and_never_falls_back(monkeypatch):
    assert browser_backend() == "chrome_mcp"
    monkeypatch.setenv("DEALFORGE_CVS_BROWSER", "invalid")
    with pytest.raises(SyncProblem, match="invalid_browser_backend"):
        browser_backend()
    monkeypatch.setenv("DEALFORGE_CVS_BROWSER", "playwright")
    assert browser_backend() == "playwright"


def test_playwright_backend_routes_to_its_factory(monkeypatch):
    import dealforge.providers.cvs.playwright_browser as module
    monkeypatch.setenv("DEALFORGE_CVS_BROWSER", "playwright")
    @asynccontextmanager
    async def fake():
        yield "playwright-only"
    monkeypatch.setattr(module, "playwright_collector", fake)
    async def run():
        async with configured_collector(chrome_factory=lambda: pytest.fail("Chrome MCP was launched")) as collector:
            assert collector == "playwright-only"
    asyncio.run(run())


class Locator:
    def __init__(self, raw="", *, kind="button"):
        self.raw, self.kind, self.calls = raw, kind, []
    async def aria_snapshot(self, **options):
        return self.raw
    def nth(self, value):
        return self
    async def get_attribute(self, name):
        return None
    async def input_value(self):
        return ""
    async def click(self, **options):
        self.calls.append("click")
    async def fill(self, value, **options):
        self.calls.append(("fill", value))


class Page:
    def __init__(self, raw="", url=HOME):
        self.url, self.raw = url, raw
        self.events, self.controls = {}, {}
    def on(self, name, callback):
        self.events[name] = callback
    def is_closed(self):
        return False
    def locator(self, selector):
        assert selector == "body"
        return Locator(self.raw)
    def get_by_role(self, role, name, exact):
        return self.controls.setdefault((role, name), Locator(kind=role))


def session(page):
    context = SimpleNamespace(pages=[page], on=lambda *args: None)
    return PlaywrightSession(context)


def test_aria_preserves_count_hierarchy_link_identity_and_selected_state():
    raw = '''- main:
  - tablist:
    - tab "All" [selected]
  - paragraph: 2 deals and rewards
  - heading "$10 reward" [level=3]
  - link "More details reward":
    - /url: /extracare/home#
  - region "Eligible Products":
    - link "Soap":
      - /url: /shop/soap-prodid-100?skuId=200
'''
    browser = session(Page(raw))
    snapshot = asyncio.run(browser.snapshot(1))
    assert _visible_count(snapshot) == 2
    assert any(n.role == "heading" and 'level="3"' in n.attributes for n in nodes(snapshot))
    assert eligible_products(snapshot)[0]["sku_id"] == "200"
    assert any(n.role == "link" and 'url="https://www.cvs.com/extracare/home"' in n.attributes for n in nodes(snapshot))


def test_snapshot_refuses_coupon_activation_cart_and_login_controls():
    browser = session(Page('- main:\n  - button "Send to card"\n  - button "Add to cart"\n  - button "Sign in"'))
    async def run():
        await browser.snapshot(1)
        for uid in browser.references[1]:
            with pytest.raises(ValueError, match="read-only"):
                await browser.call_tool("click", {"pageId":1, "uid":uid})
    asyncio.run(run())
    assert all(not c.calls for c in browser.pages[1].controls.values())


def test_observed_search_is_allowed_but_old_references_expire():
    browser = session(Page('- main:\n  - search:\n    - textbox\n    - button "Submit search"'))
    async def run():
        await browser.snapshot(1)
        old = list(browser.references[1])
        await browser.call_tool("fill", {"pageId":1,"uid":old[0],"value":"soap"})
        await browser.call_tool("click", {"pageId":1,"uid":old[1]})
        await browser.snapshot(1)
        with pytest.raises(ValueError, match="stale"):
            await browser.call_tool("click", {"pageId":1,"uid":old[1]})
    asyncio.run(run())


@pytest.mark.parametrize("url", ["https://mail.example.test/SECRET", "https://www.cvs.com/account/profile", "http://www.cvs.com/extracare/home"])
def test_navigation_refuses_unrelated_domains_and_account_routes(url):
    browser = session(Page())
    with pytest.raises(ValueError):
        asyncio.run(browser.call_tool("navigate_page", {"pageId":1,"url":url}))


def test_only_allowlisted_response_bodies_are_read_and_never_replayed():
    browser = session(Page())
    read = []
    def response(url, data, status=200):
        async def body():
            read.append(url)
            return json.dumps(data).encode()
        return SimpleNamespace(url=url, status=status, headers={}, body=body)
    async def run():
        browser.observe(1,response("https://www.cvs.com/account/profile",{"SECRET":"unrelated"}))
        browser.observe(1,response("https://mail.example.test"+PROFILE_PATH,{"SECRET":"mail"}))
        browser.observe(1,response("https://www.cvs.com"+PROFILE_PATH,deepcopy(PROFILE)))
        data = await browser.response_json(1,1,PROFILE_PATH)
        assert data["statusCode"] == "0000"
        assert len(browser.responses) == 1
        with pytest.raises(SyncProblem):
            await browser.response_json(1,1,DEFINITION_PATH)
        browser.pages[1].url = "https://mail.example.test"
        with pytest.raises(SyncProblem):
            await browser.response_json(1,1,PROFILE_PATH)
        await browser.close()
        assert not browser.responses
    asyncio.run(run())
    assert read == ["https://www.cvs.com"+PROFILE_PATH]


def test_large_responses_are_not_read():
    browser = session(Page())
    async def body():
        pytest.fail("Large response body was read")
    async def run():
        browser.observe(1,SimpleNamespace(url="https://www.cvs.com"+PROFILE_PATH,
            status=200,headers={"content-length":"10000001"},body=body))
        with pytest.raises(SyncProblem):
            await browser.response_json(1,1,PROFILE_PATH)
        await browser.close()
    asyncio.run(run())


@pytest.mark.skipif(os.environ.get("DEALFORGE_TEST_PLAYWRIGHT") != "true", reason="opt in to installed-browser synthetic integration")
@pytest.mark.parametrize("mode", ["managed", "native_cdp"])
def test_real_browser_synthetic_sync_and_persistent_restart(tmp_path, mode):
    """Real Chrome, entirely intercepted CVS pages/responses, fictional cookies."""
    from playwright.async_api import async_playwright
    from dealforge.providers.cvs.playwright_browser import browser_context
    from dealforge.providers.cvs.native_browser import native_context
    html = '''<main><div role="tablist"><button role="tab" aria-selected="true">All</button></div>
        <p>2 deals and rewards</p><h3>$10 reward</h3><a href="/extracare/home#">More details reward</a>
        <script>fetch("'''+PROFILE_PATH+'''");</script></main>'''
    async def route(request):
        if request.request.url == HOME:
            await request.fulfill(content_type="text/html", body=html)
        elif request.request.url == "https://www.cvs.com"+PROFILE_PATH:
            await request.fulfill(json=deepcopy(PROFILE))
        else:
            await request.abort()
    async def run():
        async with async_playwright() as playwright:
            results = []
            for number in range(2):
                settings = BrowserSettings(tmp_path/"profile", "chrome", True, mode)
                manager = (native_context(playwright,settings,startup_url="about:blank") if mode == "native_cdp"
                    else browser_context(playwright,settings))
                async with manager as context:
                    await context.route("**/*",route)
                    if number == 0:
                        await context.add_cookies([{"name":"synthetic_session","value":"fictional","domain":"www.cvs.com","path":"/","secure":True,"expires":time.time()+3600}])
                    else:
                        assert any(c["name"] == "synthetic_session" for c in await context.cookies())
                    boundary = PlaywrightSession(context)
                    page = context.pages[0]
                    await page.goto(HOME)
                    collector = PlaywrightCollector(boundary,state=AccountState(tmp_path/"state"),poll_seconds=0.01)
                    results.append(await collector.sync(account_ref="synthetic-account",timeout_seconds=10))
                    await boundary.close()
            assert all(r["collection"]["visible_count_matches"] for r in results)
            assert results[0]["account_binding"]["status"] == "first_seen"
            assert results[1]["account_identity_verified"] is True
            assert all(r["browser_backend"] == "playwright" for r in results)
            assert "SECRET" not in json.dumps(results)
    asyncio.run(run())
