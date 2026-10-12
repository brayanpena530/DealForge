"""CVS-only catalog collection using normal UI in the existing Chrome profile."""

from __future__ import annotations

import asyncio
from datetime import datetime
import re
from urllib.parse import urlsplit

from dealforge.providers.cvs.account_sync import SyncProblem, _time_limit
from dealforge.shopper_settings import shopping_zip, shopping_store
from dealforge.providers.cvs.catalog import (
    DEFAULT_ZIP, PICKUP_POLICY, current_store_control, nodes, parse_products,
    parse_stores, product_identity, rank_products, region, store_dialog, normalized,
    store_search_observed, store_candidates,
)

SHOP = "https://www.cvs.com/shop"


def _controls(items, role, name):
    found = [n for n in items if n.role == role and n.name == name]
    return found[0] if len(found) == 1 else None


def _options(zip_code, store_id, timeout_seconds):
    return (isinstance(zip_code, str) and re.fullmatch(r'\d{5}', zip_code)
            and (store_id is None or isinstance(store_id, str) and re.fullmatch(r'\d{1,8}', store_id))
            and type(timeout_seconds) is int and 10 <= timeout_seconds <= 180)


def _failure(problem):
    result = problem.to_dict()
    result.pop("snapshot", None)
    result.update(products=[], stores=[], in_store_prices_verified=False)
    return result


class CatalogCollector:
    def __init__(self, browser):
        self.browser = browser
        self.page = None

    async def _tab(self):
        pages = await self.browser._pages()
        if self.page in pages and urlsplit(pages[self.page]).path.startswith(("/shop", "/search")):
            return self.page
        before = set(pages)
        await self.browser._call("new_page", {"url": SHOP, "background": True, "timeout": 20000})
        pages = await self.browser._pages()
        added = [pid for pid, url in pages.items() if pid not in before and urlsplit(url).path == "/shop"]
        if len(added) != 1:
            raise SyncProblem("browser_unavailable", "catalog_tab_not_observed", "Retry the CVS catalog lookup.")
        self.page = added[0]
        return self.page

    async def _navigate(self, page, url):
        await self.browser._guard(page)
        await self.browser._call("navigate_page", {"pageId": page, "type": "url", "url": url,
                                                   "timeout": 20000, "handleBeforeUnload": "dismiss"})

    async def _click(self, page, node):
        await self.browser._guard(page)
        await self.browser._call("click", {"pageId": page, "uid": node.uid})

    async def _wait(self, page, deadline, predicate):
        while asyncio.get_running_loop().time() < deadline:
            snapshot = await self.browser._snapshot(page)
            if predicate(snapshot):
                return snapshot
            await asyncio.sleep(self.browser.poll_seconds)
        raise SyncProblem("partial", "catalog_page_not_ready", "Wait for CVS to finish loading, then retry.")

    async def _stores(self, page, zip_code, deadline, *, search=False):
        snapshot = await self._wait(page, deadline, lambda s: current_store_control(s) is not None)
        await self._click(page, current_store_control(snapshot))
        snapshot = await self._wait(page, deadline, lambda s: bool(parse_stores(s)))
        stores = parse_stores(snapshot)
        if search or not any(s["selected"] and s["zip_code"] == zip_code for s in stores):
            branch = store_dialog(snapshot)
            box = _controls(branch, "textbox", "Search by Zip Code, City, State, or Landmark")
            submit = _controls(branch, "button", "Submit Search")
            if not box or not submit:
                raise SyncProblem("partial", "store_search_controls_changed", "Select a CVS store in Chrome, then retry.")
            await self.browser._guard(page)
            await self.browser._call("fill", {"pageId": page, "uid": box.uid, "value": zip_code})
            snapshot = await self.browser._snapshot(page)
            submit = _controls(store_dialog(snapshot), "button", "Submit Search")
            if not submit:
                raise SyncProblem("partial", "store_search_controls_changed", "Select a CVS store in Chrome, then retry.")
            await self._click(page, submit)
            # The current card remains above search results. Wait for additional
            # matching stores, never mistake the old selected card for results.
            snapshot = await self._wait(page, deadline, lambda s: store_search_observed(s, zip_code)
                or len(parse_stores(s)) > len(stores)
                or any(n.name in {"No stores found", "No results found"} for n in store_dialog(s)))
            stores = parse_stores(snapshot) + store_candidates(snapshot, zip_code)
        close = _controls(store_dialog(snapshot), "button", "Close")
        if not close:
            raise SyncProblem("partial", "store_dialog_close_unavailable", "Close the store selector in Chrome, then retry.")
        await self._click(page, close)
        return [s for s in stores if s["zip_code"] in (zip_code, None)]

    async def find_stores(self, *, zip_code=DEFAULT_ZIP, timeout_seconds=90):
        try:
            zip_code = shopping_zip("cvs", zip_code)
        except ValueError:
            return _failure(SyncProblem("invalid_input", "zip_required", "Pass a ZIP or configure local preferences."))
        if not _options(zip_code, None, timeout_seconds):
            return _failure(SyncProblem("invalid_input", "invalid_catalog_options", "Use a five-digit ZIP and timeout 10–180."))
        try:
            async with _time_limit(timeout_seconds):
                deadline = asyncio.get_running_loop().time() + timeout_seconds
                page = await self._tab()
                await self._navigate(page, SHOP)
                stores = await self._stores(page, zip_code, deadline, search=True)
                return {"status": ("partial" if any(s["store_id"] is None for s in stores)
                                   else "ready") if stores else "needs_input", "zip_code": zip_code,
                        "observed_at": datetime.now().astimezone().isoformat(), "stores": stores,
                        "store_selection_required": not any(s["selected"] for s in stores),
                        "in_store_prices_verified": False}
        except SyncProblem as problem:
            return _failure(problem)
        except asyncio.TimeoutError:
            return _failure(SyncProblem("browser_unavailable", "catalog_sync_timed_out", "Check Chrome's debugging prompt and retry."))
        except Exception:
            return _failure(SyncProblem("browser_unavailable", "catalog_connection_failed", "Keep Chrome open with debugging allowed, then retry."))

    async def search_products(self, *, query, zip_code=DEFAULT_ZIP, store_id=None,
                              brand="", size="", limit=10, timeout_seconds=90):
        try:
            zip_code, store_id = shopping_zip("cvs", zip_code), shopping_store("cvs", store_id)
        except ValueError:
            return _failure(SyncProblem("invalid_input", "invalid_local_preferences", "Pass ZIP/store options or configure local preferences."))
        if (not _options(zip_code, store_id, timeout_seconds)
                or any(not isinstance(t, str) or len(t) > 100 or any(ord(c) < 32 for c in t) for t in (query, brand, size))
                or not query.strip() or type(limit) is not int or not 1 <= limit <= 25):
            return _failure(SyncProblem("invalid_input", "invalid_catalog_options", "Use a product query, five-digit ZIP, limit 1–25 and timeout 10–180."))
        return await self._lookup(query=query.strip(), zip_code=zip_code, store_id=store_id,
                                  brand=brand.strip(), size=size.strip(), limit=limit, timeout_seconds=timeout_seconds)

    async def get_product(self, *, product_url, zip_code=DEFAULT_ZIP, store_id=None, timeout_seconds=90):
        try:
            zip_code, store_id = shopping_zip("cvs", zip_code), shopping_store("cvs", store_id)
        except ValueError:
            return _failure(SyncProblem("invalid_input", "invalid_local_preferences", "Pass ZIP/store options or configure local preferences."))
        identity = product_identity(product_url)
        if identity is None or not _options(zip_code, store_id, timeout_seconds):
            return _failure(SyncProblem("invalid_input", "invalid_product_url", "Use an exact HTTPS CVS /shop/...-prodid-... product link."))
        return await self._lookup(identity=identity, zip_code=zip_code, store_id=store_id, timeout_seconds=timeout_seconds)

    async def _lookup(self, *, zip_code, store_id, timeout_seconds, query="", brand="", size="", limit=1, identity=None):
        try:
            async with _time_limit(timeout_seconds):
                deadline = asyncio.get_running_loop().time() + timeout_seconds
                page = await self._tab()
                await self._navigate(page, SHOP)
                stores = await self._stores(page, zip_code, deadline)
                selected = [s for s in stores if s["selected"] and (store_id is None or s["store_id"] == store_id)]
                if len(selected) != 1:
                    result = _failure(SyncProblem("needs_input", "requested_store_not_selected",
                        "Select one of the returned CVS stores in Chrome, then retry with its store_id."))
                    result.update(stores=stores, zip_code=zip_code)
                    return result
                store = selected[0]
                if identity:
                    await self._navigate(page, identity["url"])
                    from dealforge.providers.cvs.catalog import parse_product_detail
                    snapshot = await self._wait(page, deadline, lambda s:
                        parse_product_detail(s, identity) is not None and current_store_control(s) is not None)
                    products = [parse_product_detail(snapshot, identity)]
                else:
                    snapshot = await self.browser._snapshot(page)
                    branch = region(nodes(snapshot), "search", "Search CVS")
                    box = _controls(branch, "combobox", "Search CVS or ask a question")
                    if not box:
                        raise SyncProblem("partial", "product_search_controls_changed", "Inspect the CVS catalog adapter.")
                    await self.browser._guard(page)
                    await self.browser._call("fill", {"pageId": page, "uid": box.uid, "value": query})
                    snapshot = await self.browser._snapshot(page)
                    submit = _controls(region(nodes(snapshot), "search", "Search CVS"), "button", "Submit Search")
                    if not submit:
                        raise SyncProblem("partial", "product_search_controls_changed", "Inspect the CVS catalog adapter.")
                    await self._click(page, submit)
                    def search_ready(text):
                        if current_store_control(text) is None:
                            return False
                        main = region(nodes(text), "main")
                        query_observed = any(n.role == "paragraph" and n.name == "Results for " + query
                            or n.role == "heading" and 'level="1"' in n.attributes and normalized(n.name) == normalized(query) for n in main)
                        return query_observed and (bool(parse_products(text)) or any("no results" in n.name.casefold() for n in main))
                    snapshot = await self._wait(page, deadline, search_ready)
                    products = rank_products(parse_products(snapshot), query, brand=brand, size=size)[:limit]
                # A human changing stores mid-lookup invalidates the context.
                current = current_store_control(snapshot)
                if not current or not current.name.startswith("Current store: " + store["address"] + ","):
                    raise SyncProblem("needs_input", "store_changed_during_lookup", "Check the CVS store selection, then retry.")
                observed_at = datetime.now().astimezone().isoformat()
                for product in products:
                    product["price"].update(observed_at=observed_at, source="cvs_rendered_page", source_url=product["url"])
                    product["store_context"] = store
                return {"status": "partial", "query": query or None, "zip_code": zip_code,
                        "store": store, "observed_at": observed_at, "products": products,
                        "returned_count": len(products), "products_complete": False,
                        "in_store_prices_verified": False,
                        "review": ["online_prices_are_not_shelf_prices", "coupon_eligibility_requires_verification"],
                        "price_policy_url": PICKUP_POLICY}
        except SyncProblem as problem:
            return _failure(problem)
        except asyncio.TimeoutError:
            return _failure(SyncProblem("browser_unavailable", "catalog_sync_timed_out", "Check Chrome's debugging prompt and retry."))
        except Exception:
            return _failure(SyncProblem("browser_unavailable", "catalog_connection_failed", "Keep Chrome open with debugging allowed, then retry."))
