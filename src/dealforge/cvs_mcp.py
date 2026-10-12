"""CVS-only agent MCP server. Start with ``dealforge-cvs-mcp``."""

from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from dealforge.providers.cvs.account_sync import chrome_collector, ChromeAccountService
from dealforge.providers.cvs.browser_backend import configured_collector, connection_advice


@asynccontextmanager
async def lifespan(server):
    service = await ChromeAccountService(lambda: configured_collector(chrome_factory=chrome_collector),
        next_action=connection_advice()).start()
    try:
        yield service
    finally:
        await service.close()


server = FastMCP("DealForge CVS", lifespan=lifespan, instructions=(
    "Refresh at the start of every shopping task. Keep returned account data private. "
    "All current sync results are partial: inspect collection and definition coverage; "
    "unknown issuer/channel/eligibility never proves savings. The caller's opaque "
    "account_ref binds to the first observed card on this device; matched confirms "
    "continuity, not ownership. The default backend uses existing Chrome with "
    "interactive debugging approval; DEALFORGE_CVS_BROWSER=playwright uses a "
    "dedicated persistent browser instead. Run dealforge-cvs login for its human "
    "sign-in. This server never activates offers or makes purchases."
    " Catalog prices are online prices, including pickup; a selected store does "
    "not verify shelf prices. Text matches and partial coupon carousels do not prove "
    "checkout eligibility. Use exact SKU identity and inspect price evidence."
    " Start shopping lists with plan_cvs_trip; resolve its product, price and "
    "source-bound term tasks before treating a conditional estimate as a trip plan."
))


@server.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                          "idempotentHint": True, "openWorldHint": True})
async def sync_cvs_account(ctx: Context, account_ref: str = "cvs-personal",
                           page_id: int | None = None, detail_limit: int = 0,
                           detail_query: str = "",
                           timeout_seconds: int = 90,
                           detail_queries: list[str] | None = None,
                           include_offer_rules: bool = False) -> dict[str, Any]:
    """Refresh the configured browser's CVS wallet and coupons. Returns personal data.

    Default: open one ExtraCare tab and allow Chrome debugging. With the
    Playwright backend, sign in using dealforge-cvs login instead. No card number/password
    needed. account_ref is a stable opaque local label, not a card identity.
    detail_limit (0â€“25) bounds normal More details visits; 0 syncs inventory only.
    detail_query optionally filters visits by brand/item text, e.g. "Colgate".
    detail_queries accepts 1-10 filters sharing the visit budget, instead of detail_query.
    include_offer_rules returns all description candidates, including unenriched ones.
    Results remain partial until eligibility/terms/issuer/channel are validated.
    Missing terms do not prove savings. This never activates offers or buys items.
    """
    return await ctx.request_context.lifespan_context.sync(
        account_ref=account_ref, page_id=page_id, detail_limit=detail_limit, detail_query=detail_query,
        timeout_seconds=timeout_seconds, detail_queries=detail_queries, include_offer_rules=include_offer_rules)


@server.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False})
def assess_cvs_offer_eligibility(lines: list[dict[str, Any]], account_sync: dict[str, Any],
                                 store_id: str, account_ref: str = "cvs-personal",
                                 confirmations: list[dict[str, Any]] | None = None,
                                 channel: str = "in_store", store_timezone: str | None = None,
                                 as_of: str | None = None, allow_fixture: bool = False) -> dict[str, Any]:
    """Assess relevant account offers and return source-bound confirmation tasks.

    Scans observed rules in batches, selecting up to 25 relevant coupons. Fresh
    exact SKU memberships suggest unit_ids; text/family matches stay candidates.
    Literal restrictions and conflicting confirmations block unsupported rules.
    Purchase limits never establish remaining uses. Does not assume stacking,
    activate offers or prove checkout eligibility. Pure local, no browser access.
    """
    from dealforge.providers.cvs.offer_assessment import assess_offers
    try:
        result = assess_offers(lines, account_sync=account_sync, store_id=store_id,
            account_ref=account_ref, confirmations=confirmations, channel=channel,
            store_timezone=store_timezone, as_of=as_of, allow_fixture=allow_fixture)
        return {k: v for k, v in result.items() if k != "confirmation_requests"}
    except (ValueError, TypeError, AttributeError, KeyError):
        return {"status": "invalid_input", "reason": "invalid_offer_assessment_evidence", "offers": []}


@server.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
async def plan_cvs_trip(ctx: Context, items: list[dict[str, Any]], account_ref: str = "cvs-personal",
                         zip_code: str | None = None, store_id: str | None = None,
                         channel: str = "in_store", confirmations: list[dict[str, Any]] | None = None,
                         reward_offers: list[dict[str, Any]] | None = None,
                         profile: dict[str, Any] | None = None, extra_care_ready: bool = False,
                         detail_limit: int = 10, store_timezone: str | None = None,
                         max_candidates: int = 1000, timeout_seconds: int = 180) -> dict[str, Any]:
    """Plan an entire CVS shopping list with fresh account/store/product evidence.

    Each of 1-10 items: line_id, quantity, query OR product_url, optional brand,
    size, offer_query and price_evidence. Queries return product choices; pass a
    chosen exact SKU URL on the next call. Refreshes account, verifies selected
    store, visits relevant offer details (shared budget), resolves prices and
    optimizes credits/coupons with locally configured return scenarios. Confirm terms using
    returned source_fingerprints; repeat with confirmations. Unknown shelf prices
    and incomplete product choices suppress a whole-list recommendation. Pending
    offers are disclosed/excluded from conditional estimates. Never changes store,
    activates coupons, buys items or recommends an incomplete basket. One overall
    30-180 second collection budget. Read docs/cvs-trip-planning.md for examples.
    """
    from dealforge.providers.cvs.planning import plan_trip
    try:
        return await plan_trip(ctx.request_context.lifespan_context, items=items,
            account_ref=account_ref, zip_code=zip_code, store_id=store_id, channel=channel,
            confirmations=confirmations, reward_offers=reward_offers, profile=profile,
            extra_care_ready=extra_care_ready, detail_limit=detail_limit,
            store_timezone=store_timezone, max_candidates=max_candidates,
            timeout_seconds=timeout_seconds)
    except (ValueError, TypeError, AttributeError, KeyError):
        return {"status": "invalid_input", "reason": "invalid_trip_request_or_evidence", "recommended": None}


@server.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
async def find_cvs_stores(ctx: Context, zip_code: str | None = None, timeout_seconds: int = 90) -> dict[str, Any]:
    """Find CVS store identities in a ZIP using Chrome's store selector.

    Returns public addresses, evidenced store IDs and the currently selected
    store. ZIP-search candidates can omit IDs/ZIPs, which remain null. Does
    not change the selected store. ZIP/store defaults come from private local preferences.
    """
    return await ctx.request_context.lifespan_context.find_stores(zip_code=zip_code, timeout_seconds=timeout_seconds)


@server.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
async def search_cvs_products(ctx: Context, query: str, zip_code: str | None = None,
                              store_id: str | None = None, brand: str = "", size: str = "",
                              limit: int = 10, timeout_seconds: int = 90) -> dict[str, Any]:
    """Search CVS products and rank candidates against query/brand/size.

    Uses a separate catalog tab, preserving Coupons & Rewards. Requires a
    selected store in the requested ZIP; store_id can pin that store. Results
    carry product IDs, observed SKU IDs, match gaps and dated ONLINE prices.
    Pickup uses online pricing. In-store shelf prices remain unknown. A text
    match never proves an exact SKU or coupon eligibility. No cart changes.
    """
    return await ctx.request_context.lifespan_context.search_products(query=query, zip_code=zip_code,
        store_id=store_id, brand=brand, size=size, limit=limit, timeout_seconds=timeout_seconds)


@server.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
async def get_cvs_product(ctx: Context, product_url: str, zip_code: str | None = None,
                          store_id: str | None = None, timeout_seconds: int = 90) -> dict[str, Any]:
    """Inspect an exact CVS product link and its observed variant/online price.

    A product-family URL does not prove which variant is selected. The result
    reports whether the selected SKU was observed and agrees with the requested
    SKU. No SKU switch, cart action or purchase. Shelf prices remain unknown.
    """
    return await ctx.request_context.lifespan_context.get_product(product_url=product_url, zip_code=zip_code,
        store_id=store_id, timeout_seconds=timeout_seconds)


@server.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False})
def match_cvs_products(products: list[dict[str, Any]], offer_rules: list[dict[str, Any]]) -> dict[str, Any]:
    """Match up to 25 product observations to 25 selected account offer rules.

    Pure local comparison; no browser access. Uses fresh observed eligible SKU
    memberships first, then weaker family/text candidates. Missing carousel
    entries never prove ineligibility. All results require checkout-rule checks.
    """
    from dealforge.providers.cvs.matching import match_products_to_offers
    try:
        return match_products_to_offers(products, offer_rules)
    except (ValueError, TypeError, AttributeError):
        return {"status": "invalid_input", "reason": "invalid_matching_observations", "matches": [],
                "next_action": "Pass up to 25 normalized product observations and 25 selected offer rules."}


@server.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
async def search_cvs_weekly_ad(query: str, zip_code: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Search current public CVS weekly-ad candidates in a ZIP, without Chrome.

    Dated ad group IDs are NOT product SKUs. Returns parsed sale/coupon/reward
    candidates; missing footnotes, SKU membership and store applicability need
    verification. Ignores online/ecommerce prices in the public feed. No account
    access, coupon activation or purchases. See docs/cvs-agent-guide.md.
    """
    from dealforge.providers.cvs.weekly_ad import fetch_weekly_ad
    return await fetch_weekly_ad(query, zip_code=zip_code, limit=limit)


@server.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False})
def resolve_cvs_prices(lines: list[dict[str, Any]], channel: str, store_id: str,
                       as_of: str | None = None) -> dict[str, Any]:
    """Resolve exact-SKU basket prices with dates, store and channel evidence.

    Each line: line_id, quantity, product (get_cvs_product observation), optional
    price_evidence. Online catalog prices work for pickup; in_store requires
    caller-confirmed shelf_label/receipt/store_quote or applicable unit-price ad.
    Never selects the cheapest conflicting price. Returns unit_ids for rules.
    See docs/cvs-agent-guide.md for the evidence schema. Pure local calculation.
    """
    from dealforge.providers.cvs.pricing import resolve_prices
    try:
        return resolve_prices(lines, channel=channel, store_id=store_id, as_of=as_of)
    except (ValueError, TypeError, AttributeError, KeyError):
        return {"status": "invalid_input", "reason": "invalid_price_evidence", "lines": [],
                "next_action": "Use exact product observations and the price schema in docs/cvs-agent-guide.md."}


@server.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False})
def evaluate_cvs_basket(lines: list[dict[str, Any]], channel: str, store_id: str,
                        coupons: list[dict[str, Any]] | None = None,
                        redemptions: list[dict[str, Any]] | None = None,
                        reward_offers: list[dict[str, Any]] | None = None,
                        extra_care_ready: bool = False, reward_value: str = "0.80",
                        as_of: str | None = None) -> dict[str, Any]:
    """Model one CVS basket: discounts, old rewards spent, new rewards earned.

    Uses the same raw lines as resolve_cvs_prices and resolves them again. Rules
    explicitly name unit_ids, validity timestamps, activation, channels, issuer,
    limits, minimum quantity/spend and verified eligibility/stacking terms.
    Account offer_rules and ad candidates are NOT complete calculator rules.
    Unknown inputs suppress complete totals; modeled subtotals are diagnostic.
    Fixed coupons precede reward spending, then percent coupons. Taxes/fees are
    excluded. New rewards never pay today's bill; certificate waste is explicit.
    All results are conditional estimates based on caller attestations. No cart
    changes or automatic coupon selection. Schema/examples: docs/cvs-agent-guide.md.
    """
    from dealforge.providers.cvs.checkout import evaluate_basket
    try:
        return evaluate_basket(lines, channel=channel, store_id=store_id, coupons=coupons,
            redemptions=redemptions, reward_offers=reward_offers, extra_care_ready=extra_care_ready,
            reward_value=reward_value, as_of=as_of)
    except (ValueError, TypeError, AttributeError, KeyError):
        return {"status": "invalid_input", "reason": "invalid_basket_or_rules", "totals": None,
                "next_action": "Use normalized prices and explicit rule evidence in docs/cvs-agent-guide.md."}


@server.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False})
def prepare_cvs_purchase(lines: list[dict[str, Any]], account_sync: dict[str, Any], store_id: str,
                          account_ref: str = "cvs-personal", confirmations: list[dict[str, Any]] | None = None,
                          channel: str = "in_store",
                          store_timezone: str | None = None, as_of: str | None = None,
                          allow_fixture: bool = False) -> dict[str, Any]:
    """Bind selected coupon/reward terms to a fresh CVS sync and exact basket.

    Requires matched device continuity and expected account_ref. First call
    without confirmations returns reward drafts and pending source_fingerprints;
    include {id: coupon_id} confirmations to request coupon drafts. Then attest
    verified terms with each fingerprint. Balance/activation/expiry cannot be
    overridden. Available offers still require activation outside this tool.
    Unknown facts remain pending. Simulation fixtures require explicit opt-in.
    Schema: docs/cvs-credit-optimization.md. Never opens Chrome or changes offers.
    """
    from dealforge.providers.cvs.purchase_rules import prepare_purchase
    try:
        return prepare_purchase(lines, account_sync=account_sync, account_ref=account_ref,
            confirmations=confirmations, channel=channel, store_id=store_id,
            store_timezone=store_timezone, as_of=as_of, allow_fixture=allow_fixture)
    except (ValueError, TypeError, AttributeError, KeyError):
        return {"status": "invalid_input", "reason": "invalid_account_or_rule_confirmations"}


@server.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False})
def optimize_cvs_purchase(lines: list[dict[str, Any]], account_sync: dict[str, Any], store_id: str,
                           account_ref: str = "cvs-personal", confirmations: list[dict[str, Any]] | None = None,
                           reward_offers: list[dict[str, Any]] | None = None,
                           channel: str = "in_store",
                           store_timezone: str | None = None, profile: dict[str, Any] | None = None,
                           max_candidates: int = 1000, extra_care_ready: bool = False,
                           as_of: str | None = None, allow_fixture: bool = False) -> dict[str, Any]:
    """Choose one-checkout coupons and individual credits for an exact CVS list.

    Rebinds prepare_cvs_purchase confirmations to a fresh account and prices.
    Uses local return scenarios, with generic 7/30-day fallbacks. Profile can set future_eligible_spend,
    cash_budget BEFORE TAX, new_reward_value, return_days. Unknown future spend
    uses labeled face-value upper bounds. New rewards need unique campaign_id
    and earned_valid_from/to, earned_channels, earned_terms_verified for valuation.
    Search explores subsets, orders and physical item assignments within a budget;
    reports completeness, recommended, lowest-cash and no-credit plans. Pending
    rules are excluded and disclosed. No extras, second checkout, activation,
    browser access or purchase. All plans remain conditional before tax/fees.
    Schema and synthetic example: docs/cvs-credit-optimization.md.
    """
    from dealforge.providers.cvs.optimizer import optimize_purchase
    try:
        return optimize_purchase(lines, account_sync=account_sync, account_ref=account_ref,
            confirmations=confirmations, reward_offers=reward_offers, channel=channel, store_id=store_id,
            store_timezone=store_timezone, profile=profile, max_candidates=max_candidates,
            extra_care_ready=extra_care_ready, as_of=as_of, allow_fixture=allow_fixture)
    except (ValueError, TypeError, AttributeError, KeyError):
        return {"status": "invalid_input", "reason": "invalid_optimizer_evidence", "recommended": None}


@server.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False})
def reconcile_cvs_receipt(plan: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    """Compare a saved optimizer plan to caller-normalized receipt evidence.

    Receipts need source=shopper_receipt or fixture, verified_by_caller, store_id,
    channel, transaction_at, exact-SKU items and pre-tax totals. No raw personal
    identifiers needed. Missing fields stay partial; differences are explicit.
    Fixture comparisons never claim real receipt validation, and matching one
    receipt does not prove future POS behavior. Never connects to Chrome.
    """
    from dealforge.providers.cvs.reconciliation import reconcile_receipt
    try:
        return reconcile_receipt(plan, receipt)
    except (ValueError, TypeError, AttributeError, KeyError):
        return {"status": "invalid_input", "reason": "invalid_receipt_or_plan"}


def main():
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
