"""Read-only CVS browser transport with a dedicated persistent Playwright profile.

Uses normal navigation, accessibility snapshots and observed response bodies.
No MCP bridge, credential entry, JavaScript evaluation or request replay.
"""

from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

from dealforge.providers.cvs.account_state import default_state_directory
from dealforge.providers.cvs.account_sync import (
    AccountCollector, DEFINITION_PATH, HOME, PROFILE_PATH, SyncProblem, _cvs_url,
    _visible_count,
)
from dealforge.providers.cvs.catalog import product_identity

SHOP = "https://www.cvs.com/shop"
MAX_BODY = 10_000_000


@dataclass(frozen=True)
class BrowserSettings:
    profile: Path
    channel: str = "chrome"
    headless: bool = False
    launch_mode: str = "native_cdp"

    @classmethod
    def from_environment(cls, *, interactive=False):
        profile = Path(os.environ.get("DEALFORGE_CVS_PROFILE", default_state_directory() / "playwright-profile")).expanduser().resolve()
        # Authenticated browser state must never land in a project checkout.
        for start in (Path.cwd().resolve(), Path(__file__).resolve().parent):
            for parent in (start, *start.parents):
                if (parent / ".git").exists() or (parent / "pyproject.toml").exists():
                    if profile == parent or parent in profile.parents:
                        raise ValueError("The dedicated CVS profile must be outside the repository")
                    break
        channel = os.environ.get("DEALFORGE_CVS_CHANNEL", "chrome")
        headless = os.environ.get("DEALFORGE_CVS_HEADLESS", "false").lower()
        mode = os.environ.get("DEALFORGE_CVS_LAUNCH", "native_cdp" if channel == "chrome" else "managed")
        if channel not in {"chrome", "chromium"} or headless not in {"true", "false"} or mode not in {"managed", "native_cdp"}:
            raise ValueError("Use chrome/chromium and headless true/false")
        if mode == "native_cdp" and channel != "chrome":
            raise ValueError("native_cdp requires the Chrome channel")
        # Require a dedicated directory, never Chrome's personal User Data tree.
        chrome_root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Google" / "Chrome" / "User Data"
        chrome_root = chrome_root.resolve()
        if profile == chrome_root or chrome_root in profile.parents or profile in chrome_root.parents:
            raise ValueError("Use a separate DealForge browser profile")
        return cls(profile, channel, False if interactive else headless == "true", mode)


@asynccontextmanager
async def profile_lock(profile):
    """OS lock across CLI/MCP processes; process exit releases a stale lock."""
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    file = (profile / ".dealforge.lock").open("a+b")
    locked = False
    try:
        if file.seek(0, 2) == 0:
            file.write(b"0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise SyncProblem("needs_input", "playwright_profile_in_use",
                "Close the other DealForge CVS browser task, then retry.") from None
        yield
    finally:
        if locked:
            file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        file.close()


def aria_nodes(snapshot):
    """Convert Playwright's YAML tree into the existing parser's node contract."""
    import yaml
    if len(snapshot) > 2_000_000:
        raise ValueError("Accessibility snapshot is too large")
    tree = yaml.safe_load(snapshot)
    result = []
    def visit(values, depth=1):
        if depth > 100 or len(result) > 5000:
            raise ValueError("Accessibility snapshot is too large")
        for value in values if isinstance(values, list) else []:
            if isinstance(value, dict):
                if len(value) != 1:
                    raise ValueError("Unexpected accessibility node")
                label, children = next(iter(value.items()))
            elif isinstance(value, str):
                label, children = value, None
            else:
                raise ValueError("Unexpected accessibility node")
            if not isinstance(label, str) or label.startswith("/"):
                continue
            match = re.fullmatch(r'(\w+)(?: ("(?:\\.|[^"\\])*"))?((?: \[[^\]]+\])*)', label)
            if not match:
                raise ValueError("Unexpected accessibility role")
            role = match[1]
            name = json.loads(match[2]) if match[2] else ""
            attrs = match[3]
            leaf = children if isinstance(children, (str, int, float)) else None
            if not name and leaf is not None:
                name = str(leaf)
            # Text containers have no accessible name; preserve their text as
            # both the paragraph label and child StaticText for price parsing.
            if role == "paragraph" and not name and isinstance(children, list):
                name = " ".join(str(c["text"]) for c in children if isinstance(c, dict) and "text" in c)
            url = next((c["/url"] for c in children if isinstance(c, dict) and "/url" in c), None) if isinstance(children, list) else None
            result.append({"role": "StaticText" if role == "text" else role,
                "name": name, "attributes": attrs, "depth": depth, "url": url})
            if isinstance(children, list):
                visit(children, depth + 1)
            elif leaf is not None and role != "text":
                result.append({"role": "StaticText", "name": str(leaf), "attributes": "", "depth": depth + 1, "url": None})
    visit(tree)
    return result


class PlaywrightSession:
    """Small compatibility boundary; tools here are Python methods, not MCP."""

    def __init__(self, context):
        self.context = context
        self.pages = {}
        self.responses = OrderedDict()
        self.pending = set()
        self.references = {}
        self.page_sequence = self.request_sequence = self.snapshot_sequence = 0
        self.bytes_retained = 0
        self.context.on("page", self.attach)
        for page in context.pages:
            self.attach(page)

    def attach(self, page):
        if page in self.pages.values():
            return
        self.page_sequence += 1
        identifier = self.page_sequence
        self.pages[identifier] = page
        page.on("response", lambda response: self.observe(identifier, response))

    def observe(self, page_id, response):
        if not _cvs_url(response.url) or urlsplit(response.url).path not in {PROFILE_PATH, DEFINITION_PATH}:
            return
        page = self.pages.get(page_id)
        if page is None or not _cvs_url(page.url):
            return
        self.request_sequence += 1
        request_id = self.request_sequence
        row = {"page": page_id, "url": response.url, "status": response.status, "body": None, "size": 0}
        self.responses[request_id] = row
        while len(self.responses) > 50:
            _, removed = self.responses.popitem(last=False)
            self.bytes_retained -= removed["size"]
        if response.status == 200:
            task = asyncio.create_task(self.capture(response, request_id))
            row["task"] = task
            self.pending.add(task)
            task.add_done_callback(self.pending.discard)

    async def capture(self, response, request_id):
        try:
            length = response.headers.get("content-length")
            if length and int(length) > MAX_BODY:
                return
            body = await response.body()
            row = self.responses.get(request_id)
            if row is None or len(body) > MAX_BODY:
                return
            data = json.loads(body)
            while self.responses and self.bytes_retained + len(body) > 20_000_000:
                oldest, removed = self.responses.popitem(last=False)
                self.bytes_retained -= removed["size"]
                if oldest == request_id:
                    return
            row["body"], row["size"] = data, len(body)
            self.bytes_retained += len(body)
        except (Exception, asyncio.CancelledError):
            # Raw response/error details must never reach agent logs.
            return

    def page(self, identifier):
        page = self.pages.get(identifier)
        if page is None or page.is_closed() or not _cvs_url(page.url):
            raise SyncProblem("browser_unavailable", "cvs_tab_closed_or_changed",
                "Open CVS in the dedicated DealForge browser, then retry.")
        return page

    async def response_json(self, identifier, request_id, path):
        self.page(identifier)
        row = self.responses.get(request_id)
        if not row or row["page"] != identifier or row["status"] != 200 or urlsplit(row["url"]).path != path:
            raise SyncProblem("partial", "response_no_longer_available", "Retry a fresh CVS account sync.")
        if row.get("task"):
            await row["task"]
        if row["body"] is None:
            raise SyncProblem("partial", "response_body_unavailable", "Retry a fresh CVS account sync.")
        return row["body"]

    async def snapshot(self, identifier):
        page = self.page(identifier)
        raw = await page.locator("body").aria_snapshot(timeout=10000)
        self.snapshot_sequence += 1
        counts = Counter()
        refs, lines = {}, [f'uid=pw_root RootWebArea "CVS" url={json.dumps(page.url)}']
        for i, node in enumerate(aria_nodes(raw)):
            role, name = node["role"], node["name"]
            uid = f"pw{self.snapshot_sequence}_{i}"
            attrs = ""
            for attribute, value in re.findall(r'\[([\w-]+)(?:=([^\]]+))?\]', node["attributes"]):
                if value in (None, "true", ""):
                    attrs += " " + attribute
                elif value != "false":
                    attrs += f" {attribute}={json.dumps(value)}"
            url = urljoin(page.url, node["url"]) if node["url"] else None
            if url:
                attrs += " url=" + json.dumps(url)
            if role in {"link", "button", "textbox", "combobox"}:
                key = (role, name)
                locator = page.get_by_role(role, name=name, exact=True).nth(counts[key])
                counts[key] += 1
                refs[uid] = (locator, role, name, url)
                if role in {"textbox", "combobox"}:
                    if await locator.get_attribute("type") != "password":
                        if await locator.get_attribute("readonly") is not None:
                            attrs += " readonly"
                        # Only search field values enter internal snapshots.
                        if "search" in name.lower() or role == "combobox":
                            attrs += " value=" + json.dumps(await locator.input_value())
            lines.append("  " * node["depth"] + f"uid={uid} {role}" + (" " + json.dumps(name) if name else "") + attrs)
            # ARIA snapshots collapse a text-only named leaf. Existing catalog
            # parsers expect its displayed text as well as the semantic role.
            if name and role in {"heading", "link", "generic"}:
                lines.append("  " * (node["depth"] + 1) + f"uid={uid}_text StaticText {json.dumps(name)}")
        self.page(identifier)  # Suppress results if a human changed domains.
        self.references[identifier] = refs
        return "\n".join(lines)

    async def call_tool(self, name, arguments):
        text = ""
        if name == "list_pages":
            text = "\n".join(f"{i}: CVS ({p.url})" for i, p in self.pages.items() if not p.is_closed() and _cvs_url(p.url))
        elif name == "new_page":
            if arguments.get("url") != SHOP:
                raise ValueError("Only the CVS catalog tab can be created")
            page = await self.context.new_page()
            self.attach(page)
            await page.goto(SHOP, wait_until="domcontentloaded", timeout=20000)
        else:
            identifier = arguments["pageId"]
            page = self.page(identifier)
            if name == "navigate_page":
                url = arguments["url"]
                if url not in {HOME, SHOP} and product_identity(url) is None:
                    raise ValueError("Only observed CVS account/catalog navigation is supported")
                self.references.pop(identifier, None)
                await page.goto(url, wait_until="domcontentloaded", timeout=arguments.get("timeout", 20000))
            elif name == "take_snapshot":
                text = await self.snapshot(identifier)
            elif name == "list_network_requests":
                rows = [(i, r) for i, r in self.responses.items() if r["page"] == identifier]
                text = "\n".join(f"reqid={i} POST {r['url']} [{r['status']}]" for i, r in rows)
            elif name in {"click", "fill"}:
                ref = self.references.get(identifier, {}).get(arguments["uid"])
                if not ref:
                    raise ValueError("The observed browser control is stale")
                locator, role, label, url = ref
                if name == "click":
                    safe_link = (role == "link" and _cvs_url(url or "") and (
                        label.startswith("More details") and urlsplit(url).path == "/extracare/home"
                        or label == "Savings" and urlsplit(url).path == "/"))
                    safe_button = role == "button" and (label in {"Close", "Submit Search", "Submit search"} or label.startswith("Current store:"))
                    if not (safe_link or safe_button):
                        raise ValueError("Only read-only CVS navigation/search controls are supported")
                    await locator.click(timeout=10000)
                else:
                    if role not in {"textbox", "combobox"} or ("search" not in label.lower() and label):
                        raise ValueError("Only observed search fields can be filled")
                    if await locator.get_attribute("type") == "password":
                        raise ValueError("Credential fields must be handled by the shopper")
                    await locator.fill(arguments["value"], timeout=10000)
            elif name == "press_key":
                if arguments["key"] != "End":
                    raise ValueError("Only presentation scrolling is supported")
                await page.keyboard.press("End")
            else:
                raise ValueError("Unsupported browser operation")
        return SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text=text)])

    async def close(self):
        tasks = list(self.pending)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.responses.clear()
        self.references.clear()


class PlaywrightCollector(AccountCollector):
    async def _body(self, page, reqid, path):
        if path not in {PROFILE_PATH, DEFINITION_PATH}:
            raise ValueError("Unsupported response route")
        return await self.session.response_json(page, reqid, path)

    @staticmethod
    def describe(result):
        result["browser_backend"] = "playwright"
        result["session_storage"] = "dedicated_persistent_profile"
        # Preserve precise failure reasons, replace Chrome-MCP-specific advice.
        if result.get("status") in {"needs_login", "blocked_access", "browser_unavailable"}:
            result["next_action"] = ("Run dealforge-cvs login and sign in in the dedicated browser, then retry."
                if result["status"] == "needs_login" else "Check the dedicated DealForge CVS browser and retry; no Chrome debugging approval is needed.")
        return result

    async def sync(self, **options):
        return self.describe(await super().sync(**options))

    async def find_stores(self, **options):
        return self.describe(await super().find_stores(**options))

    async def search_products(self, **options):
        return self.describe(await super().search_products(**options))

    async def get_product(self, **options):
        return self.describe(await super().get_product(**options))


@asynccontextmanager
async def playwright_collector(*, settings=None):
    from playwright.async_api import async_playwright
    settings = settings or BrowserSettings.from_environment()
    async with profile_lock(settings.profile):
        async with async_playwright() as playwright:
            async with browser_context(playwright, settings) as context:
                session = PlaywrightSession(context)
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    session.attach(page)
                    await page.goto(HOME, wait_until="domcontentloaded", timeout=30000)
                    yield PlaywrightCollector(session)
                finally:
                    await session.close()


@asynccontextmanager
async def browser_context(playwright, settings):
    if settings.launch_mode == "native_cdp":
        from dealforge.providers.cvs.native_browser import native_context
        async with native_context(playwright, settings) as context:
            yield context
    else:
        try:
            context = await playwright.chromium.launch_persistent_context(
                str(settings.profile), channel="chrome" if settings.channel == "chrome" else None,
                headless=settings.headless, accept_downloads=False, viewport={"width":1280,"height":900})
        except Exception:
            raise SyncProblem("browser_unavailable", "playwright_launch_failed",
                "Install Chrome, or set DEALFORGE_CVS_CHANNEL=chromium and run python -m playwright install chromium.") from None
        try:
            yield context
        finally:
            await context.close()


async def login_and_verify(*, account_ref="cvs-personal", timeout_seconds=600, announce=None):
    """Human sign-in, fresh sync, close, then fresh sync in a new browser process."""
    if type(timeout_seconds) is not int or not 30 <= timeout_seconds <= 1800:
        return {"status":"invalid_input","reason":"login_timeout_must_be_30_to_1800"}
    settings = BrowserSettings.from_environment(interactive=True)
    if not isinstance(account_ref, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", account_ref) or re.search(r"\d{10,}", account_ref):
        return {"status":"invalid_input","reason":"invalid_account_label"}
    first = None
    async with playwright_collector(settings=settings) as collector:
        if announce:
            announce("The dedicated CVS browser is open. Sign in and open Coupons & Rewards. No password or card number is needed in chat.")
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            pages = await collector._pages()
            for identifier, url in pages.items():
                if not urlsplit(url).path.startswith("/extracare/"):
                    continue
                try:
                    snapshot = await collector._snapshot(identifier)
                    if _visible_count(snapshot) is not None:
                        first = await collector.sync(account_ref=account_ref, page_id=identifier, timeout_seconds=45)
                        if first.get("snapshot") is not None:
                            break
                except SyncProblem:
                    pass
            if first and first.get("snapshot") is not None:
                break
            if not any(not p.is_closed() for p in collector.session.pages.values()):
                return {"status":"needs_login","reason":"login_browser_closed","browser_backend":"playwright"}
            await asyncio.sleep(2)
        if not first or first.get("snapshot") is None:
            return {"status":"needs_login","reason":"login_not_completed","browser_backend":"playwright"}
    if announce:
        announce("Account sync succeeded. Restarting the dedicated browser to check saved-session reuse.")
    async with playwright_collector(settings=settings) as collector:
        second = await collector.sync(account_ref=account_ref, timeout_seconds=60)
    verified = second.get("snapshot") is not None and second.get("account_identity_verified") is True
    return {"status":"ready" if verified else second.get("status", "partial"),
        "browser_backend":"playwright", "launch_mode":settings.launch_mode,
        "session_persistence_verified":verified,
        "initial_sync_status":first["status"], "restart_sync_status":second["status"],
        "account_binding":second.get("account_binding"),
        "collection":second.get("collection"), "reason":second.get("reason"),
        "review":["account_terms_and_checkout_remain_unverified", "headless_and_hosted_access_not_tested"]}
