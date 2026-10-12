"""Pure CVS catalog observations and conservative product matching.

Web/pickup prices are online prices. Store identity and local availability do
not establish a shelf price. Exact SKU identity is distinct from a text match.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
import re
import unicodedata
from urllib.parse import parse_qs, unquote, urlencode, urlsplit, urlunsplit

DEFAULT_ZIP = None  # ZIP preferences are supplied locally, never published.
PICKUP_POLICY = "https://www.cvs.com/content/store-pickup"


@dataclass(frozen=True)
class Node:
    uid: str
    role: str
    name: str
    attributes: str
    indent: int


def nodes(snapshot: str) -> list[Node]:
    result = []
    skip = None
    for line in snapshot.splitlines():
        indent = len(line) - len(line.lstrip())
        if skip is not None and indent > skip:
            continue
        skip = None
        match = re.match(r'\s*uid=(\S+) (\w+)(?: ("(?:\\.|[^"\\])*"))?(.*)$', line)
        if not match:
            continue
        if match[2] == "Iframe":
            skip = indent
            continue
        try:
            name = json.loads(match[3]) if match[3] else ""
        except json.JSONDecodeError:
            continue
        result.append(Node(match[1], match[2], name, match[4], indent))
    return result


def subtree(items: list[Node], index: int) -> list[Node]:
    start = items[index]
    end = index + 1
    while end < len(items) and items[end].indent > start.indent:
        end += 1
    return items[index:end]


def region(items: list[Node], role: str, name: str | None = None) -> list[Node]:
    branches = [subtree(items, i) for i, n in enumerate(items) if n.role == role and (name is None or n.name == name)]
    return branches[0] if len(branches) == 1 else []


def node_url(node: Node) -> str | None:
    match = re.search(r'\burl=("(?:\\.|[^"\\])*")', node.attributes)
    try:
        return json.loads(match[1]) if match else None
    except json.JSONDecodeError:
        return None


def cvs_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and parts.hostname in {"www.cvs.com", "cvs.com"}
                and parts.port in (None, 443) and not parts.username and not parts.password)
    except (ValueError, TypeError):
        return False


def product_identity(url: str) -> dict | None:
    if not isinstance(url, str) or not cvs_url(url):
        return None
    parts = urlsplit(url)
    match = re.fullmatch(r'/shop/[a-zA-Z0-9_-]+-prodid-(\d{1,12})/?', parts.path)
    if not match:
        return None
    values = parse_qs(parts.query, keep_blank_values=True).get("skuId", [])
    if values and (len(values) != 1 or not re.fullmatch(r'\d{1,12}', values[0])):
        return None
    sku = values[0] if values else None
    canonical = urlunsplit(("https", "www.cvs.com", parts.path.rstrip("/"), urlencode({"skuId": sku}) if sku else "", ""))
    return {"product_id": match[1], "sku_id": sku, "url": canonical}


def normalized(text: str) -> str:
    return " ".join(re.findall(r'[a-z0-9]+(?:\.[0-9]+)?', unicodedata.normalize("NFKC", text).casefold()))


def current_store_control(snapshot: str) -> Node | None:
    controls = [n for n in nodes(snapshot) if n.role == "button" and n.name.startswith("Current store:")]
    return controls[0] if len(controls) == 1 else None


def store_dialog(snapshot: str) -> list[Node]:
    return region(nodes(snapshot), "dialog", "Change your CVS Store")


def parse_stores(snapshot: str) -> list[dict]:
    control = current_store_control(snapshot)
    selected = control.name.split(":", 1)[1].split(",", 1)[0].strip() if control else None
    result = {}
    for node in store_dialog(snapshot):
        url = node_url(node)
        if node.role != "link" or not url or not cvs_url(url):
            continue
        path = unquote(urlsplit(url).path)
        identity = re.search(r'/storeid=(\d{1,8})/?$', path, re.I)
        postal = re.search(r'-(\d{5})/storeid=', path, re.I)
        address = re.fullmatch(r'Select details for (.+)', node.name)
        if identity and postal and address:
            row = {"store_id": identity[1], "address": address[1], "zip_code": postal[1],
                   "url": urlunsplit(("https", "www.cvs.com", urlsplit(url).path, "", "")),
                   "selected": selected is not None and normalized(selected) == normalized(address[1])}
            if identity[1] in result and result[identity[1]] != row:
                raise ValueError("Conflicting store identity")
            result[identity[1]] = row
    return list(result.values())


def store_search_observed(snapshot: str, zip_code: str) -> bool:
    # Playwright can aggregate the result heading with adjacent filter text.
    # Accept its explicit ZIP summary, but reject mixed/stale ZIP summaries.
    observed = {value for n in store_dialog(snapshot)
                for value in re.findall(r'\b\d+ results for\s+["“]?(\d{5})["”]?(?!\w)', n.name)}
    return observed == {zip_code}


def store_candidates(snapshot: str, zip_code: str) -> list[dict]:
    """ZIP-search cards omit IDs and ZIPs; preserve that missing evidence."""
    if not store_search_observed(snapshot, zip_code):
        return []
    known = parse_stores(snapshot)
    addresses = {normalized(s["address"]) for s in known}
    result = []
    for n in store_dialog(snapshot):
        match = re.fullmatch(r'Select store at (.+)', n.name) if n.role == "button" else None
        if match and normalized(match[1]) not in addresses:
            addresses.add(normalized(match[1]))
            result.append({"store_id": None, "address": match[1], "zip_code": None,
                           "url": None, "selected": False, "search_zip_code": zip_code,
                           "identity_verified": False})
    return result


def price_observation(texts: list[str]) -> dict:
    # Currency-only text avoids unit prices, savings badges and reward amounts.
    texts = ["$" + texts[i + 1].strip() if text.strip() == "$" and i + 1 < len(texts)
             and re.fullmatch(r'\d{1,8}(?:\.\d{1,2})?', texts[i + 1].strip()) else text
             for i, text in enumerate(texts)]
    prices = {f"{Decimal(m[1]):.2f}" for text in texts
              if (m := re.fullmatch(r'\$(\d{1,8}(?:\.\d{1,2})?)', text.strip()))}
    is_range = len(prices) > 1 and any(text.strip() == "to" for text in texts)
    return {"amount": next(iter(prices)) if len(prices) == 1 else None,
            "currency": "USD", "channel": "online", "in_store_verified": False,
            "displayed_price_range": {"minimum": min(prices, key=Decimal), "maximum": max(prices, key=Decimal)} if is_range else None,
            "review": ["variant_price_range"] if is_range else (["ambiguous_displayed_price"] if len(prices) > 1 else ([] if prices else ["price_not_observed"]))}


def rank_products(products: list[dict], query: str, *, brand: str = "", size: str = "") -> list[dict]:
    requested = set(normalized(query).split())
    ranked = []
    for product in products:
        name = normalized(product["name"])
        tokens = set(name.split())
        missing = sorted(requested - tokens)
        constraints = []
        for field, value in (("brand", brand), ("size", size)):
            if value and not re.search(r'(?:^| )' + re.escape(normalized(value)) + r'(?: |$)', name):
                constraints.append(field + "_not_matched")
        ranked.append({**product, "match": {"kind": "text_candidate", "missing_query_tokens": missing,
                     "constraint_gaps": constraints, "exact_product_match": False,
                     "coupon_eligibility_verified": False}})
    return sorted(ranked, key=lambda p: (len(p["match"]["constraint_gaps"]), len(p["match"]["missing_query_tokens"])))


def parse_products(snapshot: str) -> list[dict]:
    """Read product links and their card-local text; never bind adjacent card prices."""
    items = region(nodes(snapshot), "main")
    marker = next((i for i, n in enumerate(items) if n.role in {"generic", "StaticText"} and n.name == "Start of product results"), None)
    if marker is not None:
        # Current CVS search exposes sibling links/text rather than card wrappers.
        # Repeated image/title links share a block; a different product starts the
        # next block. Reviews are not product URLs and cannot split the block.
        blocks = []
        start = None
        key = None
        for index in range(marker + 1, len(items)):
            node = items[index]
            if node.name == "End of product results":
                if start is not None:
                    blocks.append((items[start:index], product_identity(node_url(items[start]) or "")))
                start = None
                break
            identity = product_identity(node_url(node) or "") if node.role == "link" else None
            if identity and (identity["product_id"], identity["sku_id"]) != key:
                if start is not None:
                    blocks.append((items[start:index], product_identity(node_url(items[start]) or "")))
                start, key = index, (identity["product_id"], identity["sku_id"])
        if start is not None:
            blocks.append((items[start:], product_identity(node_url(items[start]) or "")))
        return [_product_row(branch, identity, branch[0].name) for branch, identity in blocks]
    result = {}
    for index, node in enumerate(items):
        url = node_url(node)
        identity = product_identity(url) if url else None
        if node.role != "link" or not identity or not node.name:
            continue
        # Find the smallest ancestor whose branch has only this product identity
        # and a displayed price. Its indentation is the card boundary.
        branch = subtree(items, index)
        for parent in range(index - 1, -1, -1):
            if items[parent].indent >= node.indent:
                continue
            candidate = subtree(items, parent)
            if node not in candidate:
                continue
            identities = {(p["product_id"], p["sku_id"]) for n in candidate
                          if n.role == "link" and (p := product_identity(node_url(n) or ""))}
            if identities != {(identity["product_id"], identity["sku_id"])}:
                break
            branch = candidate
            if price_observation([n.name for n in branch if n.role == "StaticText"])["amount"] is not None:
                break
        key = (identity["product_id"], identity["sku_id"])
        title = next((n.name for n in branch if n.role == "heading"), node.name)
        row = _product_row(branch, identity, title)
        if key in result:
            # Image and title links can point at the same product. Preserve only
            # one observation, and suppress conflicting money instead of choosing.
            if result[key]["price"]["amount"] != row["price"]["amount"]:
                result[key]["price"]["amount"] = None
                result[key]["price"]["review"] = ["conflicting_product_price"]
        else:
            result[key] = row
    return list(result.values())


def _product_row(branch: list[Node], identity: dict, fallback_name: str) -> dict:
    title = next((n.name for n in branch if n.role == "heading"), fallback_name)
    texts = [n.name for n in branch if n.role == "StaticText"]
    # Prices precede coupon controls on the observed search cards. A coupon's
    # dollar value or a later advertisement must not supply the product price.
    stop = next((i for i, n in enumerate(branch) if n.role == "button" and
                 (n.name.startswith("Send to card:") or n.name == "Coupon information")), len(branch))
    price = price_observation([n.name for n in branch[:stop] if n.role == "StaticText"])
    stock = {"out_of_stock" if t.startswith("Out of stock at ") else "in_stock"
             for t in texts if t.startswith(("Out of stock at ", "In stock at "))}
    availability = next(iter(stock)) if len(stock) == 1 else "unknown"
    return {**identity, "name": title, "price": price, "in_store_price": None,
            "availability": availability, "availability_scope": "selected_store_web_indicator",
            "variant_selection_required": identity["sku_id"] is None,
            "variant_choices_text": [n.name for n in branch if n.role == "textbox" and 'readonly' in n.attributes]}


def eligible_products(snapshot: str) -> list[dict]:
    items = region(nodes(snapshot), "region", "Eligible Products")
    result = {}
    for node in items:
        identity = product_identity(node_url(node) or "") if node.role == "link" else None
        if identity:
            result[(identity["product_id"], identity["sku_id"])] = {**identity, "name": node.name,
                "evidence": "observed_offer_eligible_products_carousel", "eligibility_checkout_verified": False}
    return list(result.values())


def clean_eligible_products(values) -> list[dict]:
    result = []
    for row in values if isinstance(values, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            continue
        identity = product_identity(row.get("url", ""))
        if identity is None or identity["product_id"] != row.get("product_id") or identity["sku_id"] != row.get("sku_id"):
            continue
        result.append({**identity, "name": re.sub(r'\b\d{10,}\b', '[masked]', " ".join(row["name"].split())),
                       "evidence": "observed_offer_eligible_products_carousel", "eligibility_checkout_verified": False})
    return result


def parse_product_detail(snapshot: str, expected: dict) -> dict | None:
    all_nodes = nodes(snapshot)
    roots = [n for n in all_nodes if n.role == "RootWebArea"]
    observed = product_identity(node_url(roots[0]) or "") if roots else None
    if not observed or observed["product_id"] != expected["product_id"]:
        return None
    items = region(all_nodes, "main")
    headings = [n for n in items if n.role == "heading" and re.search(r'\blevel="1"', n.attributes)]
    if len(headings) != 1 or any(re.search(r'\bbusy\b', n.attributes) for n in items[:items.index(headings[0]) + 1]):
        return None
    # The image Carousel precedes pricing. Coupon and fulfillment sections end
    # the price area; later sponsored products cannot contribute money.
    header = []
    for n in items[items.index(headings[0]):]:
        if (n.role == "region" and n.name != "Carousel"
                or n.role == "heading" and 'level="1"' not in n.attributes
                or n.role == "StaticText" and ("available coupons" in n.name.casefold()
                    or n.name in {"ExtraBucks Rewards"})
                or n.role == "button" and n.name.startswith(("Send to card", "Details"))):
            break
        header.append(n)
    if any(re.search(r'\bbusy\b', n.attributes) for n in header):
        return None
    # The selected item number is in Details, after recommendation carousels.
    # Exclude those subtrees without discarding the following Details section.
    detail_nodes = []
    skip = None
    for n in items:
        if skip is not None and n.indent > skip:
            continue
        skip = None
        if n.role == "region" and n.name != "Carousel":
            skip = n.indent
            continue
        detail_nodes.append(n)
    sku_values = {m[1] for n in detail_nodes if n.role == "StaticText"
                  and (m := re.search(r'\b(?:Item|SKU)\s*#\s*(\d{1,12})\s*$', n.name, re.I))}
    selected_sku = next(iter(sku_values)) if len(sku_values) == 1 else None
    mismatch = bool(expected["sku_id"] and selected_sku and expected["sku_id"] != selected_sku)
    price = price_observation([n.name for n in header if n.role == "StaticText"])
    if mismatch:
        price["amount"] = None
        price["review"].append("selected_sku_disagrees_with_requested_sku")
    verified = selected_sku is not None and not mismatch
    resolved = dict(expected)
    if selected_sku:
        parts = urlsplit(expected["url"])
        resolved = product_identity(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode({"skuId": selected_sku}), "")))
    return {**resolved, "requested_sku_id": expected["sku_id"], "name": headings[0].name,
            "price": price, "in_store_price": None,
            "availability": "out_of_stock" if any(n.role == "button" and n.name.startswith("Pickup ")
                and "Out of stock" in n.name for n in detail_nodes) else "unknown",
            "availability_scope": "selected_store_web_indicator",
            "selected_sku_verified": verified,
            "match": {"kind": "observed_sku" if verified else "product_family_url",
                      "exact_product_match": verified, "coupon_eligibility_verified": False,
                      "constraint_gaps": ["selected_sku_mismatch"] if mismatch else ([] if verified else ["selected_sku_not_verified"])}}
