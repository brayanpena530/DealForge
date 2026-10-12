"""Read-only account sync through an existing Chrome DevTools MCP session.

Only normal CVS page navigation and More details links are used. No credential
handling, request replay, coupon activation, cart updates or checkout occurs.
Raw tool results stay local and are never returned to the caller.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Protocol
from urllib.parse import urlsplit

from dealforge.providers.cvs.account_response import normalize_profile_response, record_id, _identity
from dealforge.providers.cvs.account_state import AccountState, AccountChanged, description_signature
from dealforge.providers.cvs.wallet import summarize_wallet

HOME = "https://www.cvs.com/extracare/home"
# Observed page operations, not public APIs. Unknown routes fail closed.
PROFILE_PATH = "/retail/client/experience/v2/load/2b3038e6-7c9c-4a0d-8f72-798fe598cfe1"
DEFINITION_PATH = "/retail/client/experience/v2/load/92629a24-f6bf-4657-96eb-2a01eb735ef4"


class SyncProblem(Exception):
    def __init__(self, status: str, reason: str, next_action: str):
        super().__init__(reason)
        self.status, self.reason, self.next_action = status, reason, next_action

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason,
                "next_action": self.next_action, "snapshot": None,
                "live_sync_available": True, "balance_is_checkout_verified": False}


class ToolSession(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


def _text(result: Any) -> str:
    if result.isError:
        raise SyncProblem("browser_unavailable", "browser_tool_failed",
                          "Check Chrome's debugging permission and the CVS tab, then retry.")
    return "\n".join(c.text for c in result.content if c.type == "text")


def _cvs_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in {"www.cvs.com", "cvs.com"}
                and parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except ValueError:
        return False


def _pages(text: str) -> dict[int, str]:
    # Never expose titles or unrelated pages. Parse only the final parenthesized URL.
    pages = {}
    for line in text.splitlines():
        match = re.match(r"(\d+): .*\((https?://[^\s)]+)\)(?:\s.*)?$", line)
        if match and _cvs_url(match[2]):
            pages[int(match[1])] = match[2]
    return pages


def _page_problem(url: str, snapshot: str) -> SyncProblem | None:
    # Inspect text internally; never echo it in errors. Don't mistake header Sign in
    # links for an expired session when the signed-in inventory is present.
    lower = snapshot.lower()
    if any(phrase in lower for phrase in ("access denied", "problem with your network address",
                                          "verify you are human", "unusual traffic")):
        return SyncProblem("blocked_access", "cvs_access_blocked",
                           "Resolve the access message in Chrome before retrying.")
    if re.search(r"/(?:account/)?(?:login|signin|sign-in)(?:/|$)", urlsplit(url).path, re.I) or (
        ('textbox "password' in lower or 'textbox "email' in lower)
        and 'tab "all"' not in lower
    ):
        return SyncProblem("needs_login", "cvs_sign_in_required",
                           "Sign in to CVS in this Chrome window and open Coupons & Rewards.")
    return None


def _visible_count(snapshot: str) -> int | None:
    # Require All to be selected. Count can be one text node or split across two.
    if not re.search(r'tab "All"[^\n]*\bselected\b', snapshot):
        return None
    values = re.findall(r'StaticText "([^"\n]*)"', snapshot)
    matches = re.findall(r"\b([\d,]+)\s+deals and rewards\b", " ".join(values), re.I)
    if len(matches) != 1 or not re.fullmatch(r"\d{1,6}|\d{1,3}(?:,\d{3})+", matches[0]):
        return None
    return int(matches[0].replace(",", ""))


def _detail_links(snapshot: str) -> list[tuple[str, str]]:
    # Card text provides a traversal key, never an offer identity. Only these
    # snapshot-provided links can be clicked; activation buttons are excluded.
    links = []
    current = []
    for line in snapshot.splitlines():
        if re.search(r'heading .*level="3"', line):
            current = [line.split('heading ', 1)[-1]]
        elif 'link "More details' in line:
            match = re.search(r'uid=(\S+) link "More details[^\n]*url="([^"\n]+)"', line)
            if match and _cvs_url(match[2]) and urlsplit(match[2]).path == "/extracare/home":
                links.append((match[1], " ".join(current)))
        elif 'StaticText "' in line:
            current.append(line.split('StaticText ', 1)[-1])
    return links


def _rewards_search(snapshot: str) -> tuple[str, str] | None:
    """Find the observed search inside main, excluding the site's header search."""
    in_main = False
    branch = []
    search_indent = None
    for line in snapshot.splitlines():
        if re.search(r'uid=\S+ main(?:\s|$)', line):
            in_main = True
        if not in_main or 'contentinfo' in line:
            continue
        indent = len(line) - len(line.lstrip())
        if search_indent is not None and indent <= search_indent:
            break
        if re.search(r'uid=\S+ search(?:\s|$)', line):
            search_indent = indent
        elif search_indent is not None:
            branch.append(line)
    text = '\n'.join(branch)
    box = re.findall(r'uid=(\S+) textbox(?:\s|$)', text)
    button = re.findall(r'uid=(\S+) button "Submit search"', text)
    return (box[0], button[0]) if len(box) == len(button) == 1 else None


def _detail_state(snapshot: str) -> str | None:
    # Product carousels/footer may have unrelated buttons. Only the offer header
    # preceding Eligible Products supplies an account activation observation.
    main = re.split(r'uid=\S+ main(?:\s|$)', snapshot, maxsplit=1)
    if len(main) != 2:
        return None
    header = re.split(r'uid=\S+ (?:region "Eligible Products"|contentinfo)', main[1], maxsplit=1)[0]
    on_card = bool(re.search(r'StaticText "On card"', header))
    available = bool(re.search(r'button "Send to card(?:"|\s)', header))
    return ("on_card" if on_card else "available") if on_card != available else None


def _detail_headline(snapshot: str) -> str | None:
    main = re.split(r'uid=\S+ main(?:\s|$)', snapshot, maxsplit=1)
    if len(main) != 2:
        return None
    header = re.split(r'uid=\S+ (?:region "Eligible Products"|contentinfo)', main[1], maxsplit=1)[0]
    headings = re.findall(r'heading "([^"\n]+)" level="1"', header)
    return re.sub(r'\b\d{10,}\b', '[masked]', headings[0]) if len(headings) == 1 else None


def _network_rows(text: str, path: str) -> list[tuple[int, int | None]]:
    rows = []
    for line in text.splitlines():
        match = re.match(r"reqid=(\d+) (?:GET|POST) (https://\S+)", line)
        if match and _cvs_url(match[2]) and urlsplit(match[2]).path == path:
            status = re.search(r"\[(\d{3})\]", line)
            rows.append((int(match[1]), int(status[1]) if status else None))
    return rows


class AccountCollector:
    """Serialize syncs sharing one browser session; return only normalized data."""

    def __init__(self, session: ToolSession, *, poll_seconds: float = 0.5,
                 state: AccountState | None = None):
        self.session = session
        self.poll_seconds = poll_seconds
        self.lock = asyncio.Lock()
        self.state = state if state is not None else AccountState()
        self.binding_status = "unavailable"
        self.namespace = None
        self.ui_states = {}
        self.cached_definitions = 0
        self.cache_observations = {}
        self.profile_id = None
        self.catalog = None

    def _catalog(self):
        if self.catalog is None:
            from dealforge.providers.cvs.catalog_sync import CatalogCollector
            self.catalog = CatalogCollector(self)
        return self.catalog

    async def find_stores(self, **options):
        async with self.lock:
            return await self._catalog().find_stores(**options)

    async def search_products(self, **options):
        async with self.lock:
            return await self._catalog().search_products(**options)

    async def get_product(self, **options):
        async with self.lock:
            return await self._catalog().get_product(**options)

    def _bind(self, payload: Any, account_ref: str) -> None:
        data = payload.get("data") if isinstance(payload, dict) else None
        profile = data.get("getEcCustomerProfile") if isinstance(data, dict) else None
        card = profile.get("xtraCard") if isinstance(profile, dict) else None
        number = card.get("xtraCardNbr") if isinstance(card, dict) else None
        try:
            self.binding_status, self.namespace = self.state.bind(account_ref, number)
        except AccountChanged:
            raise SyncProblem("needs_input", "account_changed_for_label",
                              "Sign in to the previously linked card, or use a distinct account_ref for this card.") from None
        except (OSError, ValueError):
            self.binding_status, self.namespace = "unavailable", None

    def _cached(self, payload: dict, account_ref: str) -> tuple[list[dict], list[str]]:
        definitions, review = [], []
        if self.namespace is None:
            return definitions, ["account_binding_unavailable"]
        categories = payload["data"]["getEcCustomerProfile"]["couponCategories"]
        now = datetime.now().astimezone()
        for category in ("extrabucks", "personalizedDeals", "otherDeals"):
            rows = categories.get(category)
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict) or row.get("viewableInd") == "N" or row.get("cpnSuppressCd") == "Y":
                    continue
                try:
                    identifier = record_id(account_ref, row, category == "extrabucks")
                    cached = self.state.read_definition(self.namespace, identifier,
                        signature=description_signature(row), now=now)
                except (OSError, ValueError, TypeError):
                    review.append("definition_cache_unavailable")
                    continue
                if cached:
                    definitions.append({"cmpgnId": row["cmpgnId"], "cpnNbr": row["cpnNbr"], **cached["definition"]})
                    self.cache_observations[identifier] = cached["observed_at"]
                    self.cached_definitions += 1
        return definitions, review

    def _save_definitions(self, payload: dict, definitions: list[dict], account_ref: str) -> list[str]:
        if self.namespace is None:
            return []
        # Conflicting catalog responses must not create a cache entry.
        indexed = {}
        for definition in definitions:
            try:
                identity = _identity(definition, False)
            except ValueError:
                continue
            if identity in indexed and indexed[identity] != definition:
                indexed[identity] = None
            else:
                indexed.setdefault(identity, definition)
        categories = payload["data"]["getEcCustomerProfile"]["couponCategories"]
        review = []
        for category in ("extrabucks", "personalizedDeals", "otherDeals"):
            rows = categories.get(category)
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict) or row.get("viewableInd") == "N" or row.get("cpnSuppressCd") == "Y":
                    continue
                try:
                    definition = indexed.get(_identity(row, False))
                    if definition:
                        self.state.write_definition(self.namespace, record_id(account_ref, row, category == "extrabucks"),
                            definition, signature=description_signature(row), observed_at=datetime.now().astimezone())
                except (OSError, ValueError, TypeError):
                    review.append("definition_cache_write_failed")
        return review

    async def _call(self, name: str, arguments: dict[str, Any]) -> str:
        return _text(await self.session.call_tool(name, arguments))

    async def _pages(self) -> dict[int, str]:
        return _pages(await self._call("list_pages", {}))

    async def _guard(self, page: int) -> str:
        url = (await self._pages()).get(page)
        if url is None:
            raise SyncProblem("browser_unavailable", "cvs_tab_closed_or_changed",
                              "Open the signed-in CVS Coupons & Rewards tab in Chrome.")
        return url

    async def _snapshot(self, page: int) -> str:
        url = await self._guard(page)
        text = await self._call("take_snapshot", {"pageId": page})
        problem = _page_problem(url, text)
        if problem:
            raise problem
        return text

    async def _network(self, page: int, path: str) -> list[tuple[int, int | None]]:
        await self._guard(page)
        rows = []
        # Scan a bounded number of network-list pages, without preserved requests.
        for index in range(5):
            text = await self._call("list_network_requests", {
                "pageId": page, "resourceTypes": ["xhr", "fetch"],
                "pageSize": 200, "pageIdx": index, "includePreservedRequests": False,
            })
            rows.extend(_network_rows(text, path))
            if not re.search(r"Next page", text, re.I):
                break
        return rows

    async def _body(self, page: int, reqid: int, path: str) -> Any:
        # Recheck the observed request and domain before exporting. Never read
        # unrelated profile endpoints. Both bodies use temporary files to keep
        # request payloads out of the tool's inline text.
        if path not in (PROFILE_PATH, DEFINITION_PATH):
            raise SyncProblem("partial", "unsupported_response_route", "Inspect the CVS account adapter.")
        await self._guard(page)
        if reqid not in {rid for rid, status in await self._network(page, path) if status == 200}:
            raise SyncProblem("partial", "response_no_longer_available", "Retry a fresh account sync.")
        with tempfile.TemporaryDirectory(prefix="dealforge-cvs-") as folder:
            file = Path(folder) / "response.network-response"
            await self._call("get_network_request", {
                "pageId": page, "reqid": reqid, "responseFilePath": str(file),
                "requestFilePath": str(Path(folder) / "request.network-request"),
            })
            if not file.exists() or file.stat().st_size > 10_000_000:
                raise SyncProblem("partial", "response_body_unavailable", "Retry a fresh account sync.")
            try:
                return json.loads(file.read_text(encoding="utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                raise SyncProblem("partial", "response_schema_changed", "Inspect the CVS account adapter.") from None

    async def _navigate(self, page: int) -> None:
        await self._guard(page)
        await self._call("navigate_page", {"pageId": page, "type": "url", "url": HOME,
                                          "timeout": 20000, "handleBeforeUnload": "dismiss"})

    async def _restore_inventory(self, page: int) -> None:
        # A timed-out More details visit can leave the SPA on an offer. Never
        # navigate a login, access-blocked, changed or unrelated page.
        try:
            if urlsplit(await self._guard(page)).path != "/extracare/home":
                return
            snapshot = await self._snapshot(page)
            controls = _rewards_search(snapshot)
            filtered = bool(controls and re.search(r'uid=' + re.escape(controls[0]) + r' textbox[^\n]*value="[^"\n]+"', snapshot))
            if _visible_count(snapshot) is None or filtered:
                await self._navigate(page)
        except Exception:
            pass

    def _result(self, draft, limit: int, review=(), query: str = "", include_offer_rules=False) -> dict[str, Any]:
        result = draft.to_dict()
        result["live_sync_available"] = True
        result["account_identity_verified"] = self.binding_status == "matched"
        result["account_binding"] = {"status": self.binding_status,
                                    "meaning": "continuity_on_this_device", "ownership_verified": False}
        result["review"] = sorted(set(result["review"]) | set(review))
        matched = len(draft.definitions_observed)
        total = draft.collection["collected_records"]
        result["definition_coverage"] = {"requested_limit": limit,
            "matched_records": matched, "missing_records": total - matched,
            "cached_records": self.cached_definitions}
        observed = set(draft.definitions_observed)
        result["offer_rules"] = [dict(rule) for rule in draft.rule_candidates if (
            include_offer_rules or (query.casefold() in rule["description_text"].casefold() if query else rule["offer_id"] in observed))]
        for rule in result["offer_rules"]:
            rule["cached_definition_observed_at"] = self.cache_observations.get(rule["offer_id"])
        wallet = summarize_wallet(draft.snapshot, as_of=datetime.now().astimezone())
        # The snapshot is already returned above. Don't duplicate every offer or
        # repeat hundreds of equivalent warnings in an agent's tool context.
        wallet.pop("account")
        counts = Counter(re.sub(r"\b(Reward|Coupon) cvs-[a-f0-9]+", r"\1", warning)
                         for warning in wallet["warnings"])
        wallet["warnings"] = list(counts)
        wallet["warning_counts"] = dict(counts)
        result["wallet"] = wallet
        return result

    async def _wait_inventory(self, page: int, deadline: float, old_ids: set[int]):
        payload = None
        observed_at = None
        while asyncio.get_running_loop().time() < deadline:
            snapshot = await self._snapshot(page)
            rows = await self._network(page, PROFILE_PATH)
            fresh = [(rid, status) for rid, status in rows if rid not in old_ids]
            if any(status in (401, 403) for _, status in fresh):
                status = "needs_login" if any(s == 401 for _, s in fresh) else "blocked_access"
                raise SyncProblem(status, "cvs_account_request_denied",
                                  "Check the CVS sign-in or access message in Chrome before retrying.")
            success = [rid for rid, status in fresh if status == 200]
            if success and payload is None:
                payload = await self._body(page, success[-1], PROFILE_PATH)
                self.profile_id = success[-1]
                observed_at = datetime.now().astimezone()
            count = _visible_count(snapshot)
            if payload is not None and count is not None:
                return payload, count, snapshot, observed_at
            await asyncio.sleep(self.poll_seconds)
        raise SyncProblem("partial", "fresh_inventory_not_observed",
                          "Check Coupons & Rewards finished loading, then retry. The CVS adapter may need updating.")

    async def _search(self, page: int, snapshot: str, query: str) -> bool:
        controls = _rewards_search(snapshot)
        if not controls:
            return False
        await self._guard(page)
        await self._call("fill", {"pageId": page, "uid": controls[0], "value": query})
        controls = _rewards_search(await self._snapshot(page))
        if not controls:
            return False
        await self._guard(page)
        await self._call("click", {"pageId": page, "uid": controls[1]})
        await asyncio.sleep(self.poll_seconds)
        return True

    async def _check_continuity(self, page: int, account_ref: str) -> None:
        rows = await self._network(page, PROFILE_PATH)
        newer = [(rid, code) for rid, code in rows if self.profile_id is not None and rid > self.profile_id]
        if any(code in (401, 403) for _, code in newer):
            raise SyncProblem("needs_login" if any(code == 401 for _, code in newer) else "blocked_access",
                              "cvs_account_request_denied", "Check CVS in Chrome, then retry.")
        successful = [rid for rid, code in newer if code == 200]
        if successful:
            payload = await self._body(page, successful[-1], PROFILE_PATH)
            self._bind(payload, account_ref)
            self.profile_id = successful[-1]

    async def _definitions(self, page: int, limit: int, deadline: float,
                           query: str = "", visited: set | None = None) -> tuple[list[dict], list[str]]:
        definitions, review = [], []
        visited = visited if visited is not None else set()
        attempts = 0
        idle_scrolls = 0
        searched = False
        while attempts < limit and asyncio.get_running_loop().time() < deadline:
            snapshot = await self._snapshot(page)
            if query and not searched:
                searched = await self._search(page, snapshot, query)
                if searched:
                    snapshot = await self._snapshot(page)
            links = [(uid, key) for uid, key in _detail_links(snapshot)
                     if key not in visited and query.casefold() in key.casefold()]
            if not links:
                # Infinite scrolling is presentation-only. Stop if it makes no progress.
                idle_scrolls += 1
                if idle_scrolls > 3:
                    review.append("detail_links_exhausted")
                    break
                await self._guard(page)
                await self._call("press_key", {"pageId": page, "key": "End"})
                await asyncio.sleep(self.poll_seconds)
                continue
            idle_scrolls = 0
            uid, key = links[0]
            visited.add(key)
            old = {rid for rid, _ in await self._network(page, DEFINITION_PATH)}
            # Snapshot UID was observed above, never supplied by the agent.
            await self._guard(page)
            await self._call("click", {"pageId": page, "uid": uid})
            attempts += 1
            received = False
            detail_deadline = min(deadline, asyncio.get_running_loop().time() + 12)
            while asyncio.get_running_loop().time() < detail_deadline:
                detail_snapshot = await self._snapshot(page)  # Detect login/access interruption.
                fresh = [rid for rid, status in await self._network(page, DEFINITION_PATH)
                         if rid not in old and status == 200]
                if fresh:
                    payload = await self._body(page, fresh[-1], DEFINITION_PATH)
                    if isinstance(payload, dict) and payload.get("statusCode") == "0000":
                        data = payload.get("data")
                        data = data.get("extracareCouponDefinition") if isinstance(data, dict) else None
                        records = data.get("extracareCouponDefinitions") if isinstance(data, dict) else None
                        if isinstance(records, list) and records and all(isinstance(r, dict) for r in records):
                            definitions.extend(records)
                            detail_snapshot = await self._snapshot(page)
                            state = _detail_state(detail_snapshot)
                            headline = _detail_headline(detail_snapshot)
                            if len(records) == 1 and headline:
                                records[0]["observed_ui_headline"] = headline
                            if len(records) == 1:
                                from dealforge.providers.cvs.catalog import eligible_products
                                records[0]["observed_eligible_products"] = eligible_products(detail_snapshot)
                                records[0]["eligibility_observed_at"] = datetime.now().astimezone().isoformat()
                            if len(records) == 1 and state:
                                try:
                                    self.ui_states[_identity(records[0], False)] = state
                                except ValueError:
                                    review.append("definition_identity_unavailable")
                            received = True
                    break
                await asyncio.sleep(self.poll_seconds)
            if not received:
                review.append("selected_definition_unavailable")
            # Savings is the observed SPA return link. Its href is misleading;
            # click only by the snapshot's exact accessible name.
            snapshot = await self._snapshot(page)
            back = re.search(r'uid=(\S+) link "Savings" url="https://www.cvs.com/"', snapshot)
            if not back:
                review.append("savings_return_link_unavailable")
                break
            await self._guard(page)
            await self._call("click", {"pageId": page, "uid": back[1]})
            searched = False
            while asyncio.get_running_loop().time() < deadline:
                if _visible_count(await self._snapshot(page)) is not None:
                    break
                await asyncio.sleep(self.poll_seconds)
        if attempts < limit and asyncio.get_running_loop().time() >= deadline:
            review.append("definition_time_budget_reached")
        return definitions, review

    async def sync(self, *, account_ref: str = "cvs-personal", page_id: int | None = None,
                   detail_limit: int = 0, detail_query: str = "",
                   timeout_seconds: int = 90, detail_queries: list[str] | None = None,
                   include_offer_rules: bool = False) -> dict[str, Any]:
        if (not isinstance(account_ref, str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", account_ref)
                or re.search(r"\d{10,}", account_ref)
                or type(detail_limit) is not int or not 0 <= detail_limit <= 25
                or not isinstance(detail_query, str) or len(detail_query) > 100
                or any(ord(c) < 32 for c in detail_query)
                or type(include_offer_rules) is not bool
                or (detail_queries is not None and (not isinstance(detail_queries, list)
                    or not 1 <= len(detail_queries) <= 10 or bool(detail_query)
                    or any(not isinstance(q, str) or not q.strip() or len(q) > 100
                           or any(ord(c) < 32 for c in q) for q in detail_queries)))
                or type(timeout_seconds) is not int or not 10 <= timeout_seconds <= 180
                or (page_id is not None and (type(page_id) is not int or page_id < 1))):
            return SyncProblem("invalid_input", "invalid_sync_options",
                               "Use an opaque account label, detail_limit 0–25 and timeout_seconds 10–180.").to_dict()
        async with self.lock:
            self.binding_status, self.namespace = "unavailable", None
            self.ui_states, self.cache_observations = {}, {}
            self.cached_definitions = 0
            self.profile_id = None
            page = None
            draft = None
            try:
                async with _time_limit(timeout_seconds):
                    pages = await self._pages()
                    candidates = [pid for pid, url in pages.items()
                                  if urlsplit(url).path.startswith("/extracare/")]
                    if page_id is not None:
                        if page_id not in pages:
                            raise SyncProblem("needs_input", "cvs_tab_not_found",
                                              "Open a CVS tab in the connected Chrome window.")
                        page = page_id
                    elif len(candidates) == 1:
                        page = candidates[0]
                    else:
                        raise SyncProblem("needs_input", "cvs_tab_missing_or_ambiguous",
                                          "Keep one CVS ExtraCare tab open, or provide its page_id.")
                    old_ids = {rid for rid, _ in await self._network(page, PROFILE_PATH)}
                    deadline = asyncio.get_running_loop().time() + timeout_seconds
                    await self._navigate(page)
                    payload, count, snapshot, observed_at = await self._wait_inventory(page, deadline, old_ids)
                    draft = normalize_profile_response(payload, account_ref=account_ref,
                                                       observed_at=observed_at, reported_count=count)
                    self._bind(payload, account_ref)
                    # Retain only coupon categories for the later identity join; discard
                    # card, preferences, redundant lists and all other profile fields.
                    profile = payload["data"]["getEcCustomerProfile"]
                    payload = {"statusCode": "0000", "data": {"getEcCustomerProfile": {
                        "couponCategories": profile["couponCategories"]}}}
                    del profile
                    cached, review = self._cached(payload, account_ref)
                    definitions = []
                    if detail_limit:
                        try:
                            queries = list(dict.fromkeys(q.strip() for q in detail_queries)) if detail_queries else [detail_query.strip()]
                            visited = set()
                            for index, query in enumerate(queries):
                                budget = detail_limit // len(queries) + (index < detail_limit % len(queries))
                                if not budget:
                                    review.append("detail_query_budget_exhausted")
                                    continue
                                if detail_queries:
                                    found, detail_review = await self._definitions(page, budget, deadline, query, visited)
                                else:
                                    found, detail_review = await self._definitions(page, budget, deadline, query)
                                definitions.extend(found)
                                review.extend(detail_review)
                        except SyncProblem as problem:
                            if problem.status in ("needs_login", "blocked_access", "needs_input"):
                                raise
                            review.append(problem.reason)
                    if detail_limit:
                        await self._check_continuity(page, account_ref)
                    if definitions:
                        review.extend(self._save_definitions(payload, definitions, account_ref))
                    if definitions or cached:
                        # A fresh observation overrides cached text for that catalog ID.
                        fresh_identities = {_identity(d, False) for d in definitions}
                        for category, rows in payload["data"]["getEcCustomerProfile"]["couponCategories"].items():
                            for row in rows if isinstance(rows, list) else []:
                                if isinstance(row, dict):
                                    try:
                                        if _identity(row, False) in fresh_identities:
                                            self.cache_observations.pop(record_id(account_ref, row, category == "extrabucks"), None)
                                    except ValueError:
                                        pass
                        self.cached_definitions = len(self.cache_observations)
                        cached = [d for d in cached if _identity(d, False) not in fresh_identities]
                        draft = normalize_profile_response(payload, account_ref=account_ref,
                            observed_at=observed_at, reported_count=count, definitions=cached + definitions,
                            ui_states=self.ui_states)
                    del payload
                    return self._result(draft, detail_limit, review, detail_query.strip(), include_offer_rules)
            except SyncProblem as problem:
                return problem.to_dict()
            except asyncio.TimeoutError:
                if draft is not None:
                    result = self._result(draft, detail_limit, ["definition_collection_interrupted"], detail_query.strip(), include_offer_rules)
                    result["reason"] = "sync_time_budget_reached"
                    return result
                return SyncProblem("browser_unavailable", "sync_timed_out",
                                   "Check Chrome's Allow debugging prompt and retry when CVS is loaded.").to_dict()
            except ValueError:
                return SyncProblem("partial", "response_schema_changed",
                                   "Inspect the CVS account adapter; no valid snapshot was produced.").to_dict()
            except Exception:
                return SyncProblem("browser_unavailable", "browser_connection_failed",
                                   "Keep Chrome open with debugging allowed and retry the CVS sync.").to_dict()
            finally:
                if page is not None and draft is not None and detail_limit:
                    try:
                        await asyncio.wait_for(self._restore_inventory(page), timeout=5)
                    except asyncio.TimeoutError:
                        pass


@asynccontextmanager
async def _time_limit(seconds: int):
    # asyncio.timeout is 3.11+, while the project supports Python 3.10.
    task = asyncio.current_task()
    expired = False
    def cancel():
        nonlocal expired
        expired = True
        task.cancel()
    handle = asyncio.get_running_loop().call_later(seconds, cancel)
    try:
        yield
    except asyncio.CancelledError:
        if expired:
            if hasattr(task, "uncancel"):
                task.uncancel()
            raise asyncio.TimeoutError from None
        raise
    finally:
        handle.cancel()


class ChromeAccountService:
    """Own task-bound SDK contexts and reconnect on the next request after failure.

    Startup is lazy, so agents can discover the tool while Chrome is closed.
    One worker serializes browser access. Cancellation never crosses an SDK
    context's owning task; no raw bridge errors escape to the agent.
    """

    def __init__(self, factory, *, next_action=None):
        self.factory = factory
        self.next_action = next_action or "Keep Chrome open with debugging allowed, then retry. Check Node/npx is installed."
        self.queue = asyncio.Queue()
        self.worker = None

    async def start(self):
        self.worker = asyncio.create_task(self._run())
        return self

    async def close(self):
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        while not self.queue.empty():
            _, future = self.queue.get_nowait()
            if not future.done():
                future.cancel()

    async def sync(self, **options):
        return await self._request("sync", options)

    async def find_stores(self, **options):
        return await self._request("find_stores", options)

    async def search_products(self, **options):
        return await self._request("search_products", options)

    async def get_product(self, **options):
        return await self._request("get_product", options)

    async def _request(self, operation, options):
        future = asyncio.get_running_loop().create_future()
        await self.queue.put(((operation, options), future))
        return await future

    async def _run(self):
        current = None
        try:
            while True:
                current = await self.queue.get()
                if current[1].cancelled():
                    continue
                try:
                    async with self.factory() as collector:
                        while True:
                            (operation, options), future = current
                            if not future.cancelled():
                                result = await getattr(collector, operation)(**options)
                                if not future.done():
                                    future.set_result(result)
                                if result.get("status") == "browser_unavailable" or "browser_tool_failed" in result.get("review", []):
                                    break
                            current = await self.queue.get()
                except SyncProblem as problem:
                    future = current[1]
                    if not future.done():
                        future.set_result(problem.to_dict())
                except Exception:
                    future = current[1]
                    if not future.done():
                        future.set_result(SyncProblem("browser_unavailable", "browser_bridge_unavailable",
                            self.next_action).to_dict())
        finally:
            if current is not None and not current[1].done():
                current[1].cancel()


@asynccontextmanager
async def chrome_collector():
    """Launch the pinned official MCP bridge; keep it alive for repeated syncs."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    args = ["-y", "chrome-devtools-mcp@1.10.1", "--autoConnect", "--no-usage-statistics",
            "--no-performance-crux", "--no-source-maps", "--no-javascript-evaluation",
            "--categoryEmulation=false", "--categoryPerformance=false", "--categoryMemory=false"]
    params = StdioServerParameters(command="cmd.exe" if os.name == "nt" else "npx",
                                  args=["/c", "npx.cmd", *args] if os.name == "nt" else args)
    # Suppress bridge stderr (may contain URLs or tool errors). Return safe errors
    # through sync results instead. No debug logs or raw response files persist.
    with open(os.devnull, "w") as errors:
        async with stdio_client(params, errlog=errors) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=180)) as session:
                await session.initialize()
                yield AccountCollector(session)
