# Reusable CVS agent integration

Reassessed 2026-10-09 against `origin/main` at `35954c4`; account investigation
continued 2026-10-10.
Status: wallet validation, JSON CLI, live Chrome account collection and the
`sync_cvs_account` MCP wrapper are implemented. Live collection and targeted detail enrichment have been verified. Personal
account counts, store preferences and troubleshooting observations are omitted.

## Goal

An agent accepts an arbitrary shopping list and produces an explainable CVS
purchase plan using the shopper's ExtraCare offers and available rewards.
Reuse the same capability on every trip, refreshing prices and account data
before making recommendations. The shopper should not have to reconstruct
their coupon wallet each time.

Start with in-store purchases. Online prices, coupon eligibility and reward
redemption can differ; do not mix channels in one plan.

## Current repository state

| Capability | State |
| --- | --- |
| Shared `Promotion`, `PromoSource`, Decimal money | Implemented |
| CVS weekly-ad parser | Implemented in `providers/cvs/sale_story.py` |
| CVS public-data research and historical flyer | Available in `cvs-data-sources.md` and test fixtures |
| Live CVS ad collection | Public ZIP-scoped search implemented and live-verified October 10, 2026; group candidates remain partial |
| Product search/detail and store context | Implemented; live search and exact SKU/online-price lookup verified October 10, 2026 |
| Coupon/product matching | Fresh observed SKU membership, family and text candidates; checkout eligibility remains unknown |
| Store shelf prices | No automated source verified; web/pickup prices explicitly online |
| Personal coupons and ExtraBucks wallet | Fresh read-only inventory, card continuity, targeted details and local text cache implemented |
| CVS checkout optimizer | Bounded one-checkout coupon/certificate selection with configurable return-day wallet scenarios and explicit uncertainty implemented |
| Price resolution / basket evaluation | Dated exact-SKU evidence and conditional single-basket calculator implemented; no POS validation yet |
| Agent API / MCP server | Eleven CVS tools plus JSON CLI, including preparation, optimization and receipt comparison |

H-E-B already resolves products, collects offers, matches deals and builds a
priced store route. Its optimizer is retailer-specific and accounts per unit;
it does not enforce buying the advertised quantity. Reuse parsing patterns,
Decimal arithmetic and explainable results, but do not apply H-E-B pricing
rules to CVS.

Baseline verification: `uv run --no-project --with pytest --with beautifulsoup4
python -m pytest -q` produced **218 passed, 3 failed**. All failures are in
`test_heb_browser_parse.py`: the authored search fixture uses
`data-testid="product-card"`, while the updated search-card selectors expect
`data-component="product-card"`, `data-qe-id="productCard"` or
`data-testid="productCardContainer"`. This is an existing fixture/selector
mismatch; it does not establish whether the live H-E-B page currently works.

## Proposed agent contract

Keep domain logic in Python provider modules. Expose structured JSON through a
CLI first, with a thin MCP wrapper over the same functions after the workflow
works. Agents should call tools rather than scrape pages or do checkout math
independently. No agent framework dependency is required in the pricing engine.

| Proposed operation | Purpose |
| --- | --- |
| `sync_cvs_account(account_ref)` | Read personal coupons and reward certificates; return snapshot ID, freshness and connection status |
| `list_cvs_offers(store, shopping_at, query=None)` | Fetch applicable public offers with dates, terms and source references |
| `plan_cvs_trip(items, store, account_ref, objective, constraints)` | Resolve products and compare eligible checkout combinations |
| `recheck_cvs_plan(plan_id)` | Refresh prices, offers, limits and wallet state; explain changes before shopping |

`items` must contain quantities, product queries and optional exact product
links or IDs. Optional constraints include brand/size restrictions,
substitution permission, maximum cash spend, maximum transactions and an
explicit budget for extra items. A ZIP can find candidates, but store identity
and store prices must be resolved before a plan claims exact totals.

Tool results should use consistent statuses such as `ready`, `needs_login`,
`blocked_access`, `needs_input`, `partial` and `stale`. Missing information must remain unknown,
with a reason and a bounded next action. Do not turn unknown prices or rewards
into zero. Return alternatives when product resolution is ambiguous.

Each plan should report at least:

1. Exact products, quantities, eligibility evidence and source dates.
2. Coupons to use and coupons still requiring activation.
3. Cash due, including whether tax is known or only estimated.
4. Existing rewards redeemed, new rewards expected, remaining rewards and
   any value forfeited through redemption.
5. A short checkout checklist and any conditions needing store confirmation.

## Personal account access

The authenticated Chrome route is now implemented and live-tested. A card
number alone does not provide access to the wallet. The historical connection
investigation below explains this choice; the agent guide describes current setup.

The shopper links their card to CVS.com/app and signs in interactively. Read
the rewards and coupon views from that session. Inspect the rendered data
first; investigate same-session JSON responses only if the selected browser
integration supports them. Use a DOM reader if necessary. Do not assume
private upstream endpoints from the older research still exist.

For initial browser inspection in this T3 workspace, use its collaborative
preview tools. For the reusable adapter, support an injected browser/session
interface so the provider is not tied to one agent host. A user-maintained,
local snapshot is a useful development fallback, not the finished automated
experience. Detect session expiry and return `needs_login`.

### Account access and privacy

Use an interactively signed-in dedicated browser. Treat authentication and access
denials separately, avoid retry loops, and never bypass site security checks.
Personal account observations, credentials, sessions, network diagnostics and
store preferences belong outside version control. Browser reconnects can require
debugging approval; keep the client alive for one workflow.

### Recommended connection experiment

Current research did not identify a documented public consumer API for reading
personal ExtraCare rewards and coupons. This is a search finding, not proof
that CVS has no partner integrations. Product-data services are a different
category: [ReefAPI's published CVS endpoints](https://reefapi.com/cvs-api)
cover product search/detail/batch, not access to the shopper's account dashboard.
No paid catalog service is needed to test personal account access.

Start with Google's Chrome DevTools MCP connected to the Chrome session that
already works. Google's [configuration guide](https://developer.chrome.com/docs/devtools/agents/get-started/configuration)
documents Chrome 144+ `--autoConnect` with remote debugging enabled via
`chrome://inspect/#remote-debugging` and an interactive connection prompt.
This machine has Chrome `154.0.8037.98`, Node `24.16.0`, and `npx.cmd` `11.13.0`.
The scoped [`../../.codex/config.toml`](../../.codex/config.toml) now enables
the CVS-only `sync_cvs_account` wrapper and disables the earlier general
inspection entry. Its internal bridge pins version 1.10.1 and disables telemetry
and JavaScript evaluation. The collector verifies CVS targets and allows only
observed profile/definition response routes. No global MCP settings were changed.

The [official MCP configuration reference](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/configuration.md)
documents the connection and telemetry flags.
[Codex's MCP guide](https://learn.chatgpt.com/docs/extend/mcp) documents
project `.codex/config.toml` configuration in trusted projects. A fresh agent
session may be required to load its tools; T3 hot reload has not been verified.
`npx.cmd` works here without changing PowerShell execution policy. The current
session's connection test used an SDK client rather than newly registered
tools, so it does not prove that the host has loaded the project configuration.

The first test is a read-only list of pages and inspection of the CVS rewards
view. Allocate roughly 10 minutes for connector setup and 20–30 minutes for
initial account-data inspection, assuming Chrome grants the connection and CVS
continues to serve the page. Successful manual login does not establish that
automated inspection will succeed.

Inspect the account page's normal network responses for coupon and reward
JSON. If complete data is present, normalize those responses into the existing
wallet contract. If not, extract rendered cards and expanded terms, handling
pagination and lazy loading. Distinguish real offer IDs from DOM positions;
verify counts against the page before marking collection complete. Do not
assume older bundle references reveal a usable current endpoint.

The live rewards-page test observed successful POST requests to CVS's
`/retail/client/experience/v2/load/<request-id>` and
`/apix/client/experience/v2/load/<request-id>` routes. Only the network request
list was inspected; body schemas and which response contains rewards versus
coupons were initially unknown. On October 10, body inspection identified
`data.getEcCustomerProfile.couponCategories` as the categorized inventory and
`data.extracareCouponDefinition.extracareCouponDefinitions` as a separate offer
definition loaded by the More details link. The profile arrays included the
full visible collection for the observed account after filtering explicitly
hidden records; a fresh page count is still required to establish completeness
on each run. Definition data has more terms and purchase-requirement fields,
but its generic activation flags cannot replace account-specific state.

`providers/cvs/account_response.py` implements a pure draft normalizer for
these observed fields. It excludes unrelated profile data, hashes source
identities, reconciles visible counts and quarantines conflicting records.
Definitions enrich text only. All results remain partial with explicit
term/channel/issuer limitations. The live collector and MCP wrapper now use this
normalizer. The original account-foundation checks passed **120 tests**, including real stdio protocol
with a synthetic browser. Live checks separately reconciled the full account
response and joined selected definitions. The full suite reports **299 passed,
3 failed**, with the same baseline H-E-B fixture/selector failures described
above. See the agent guide for the verified schema and remaining work.
No private endpoint has been replayed or established as a supported API.
Personal balances and coupon contents are excluded from repository notes.

Private same-session endpoints, if found, are implementation details rather
than a supported CVS API. Prefer observed page responses initially; consider
same-session read requests only after their semantics and authentication are
understood. Keep tokens and session cookies out of results and fixtures.

The reusable `sync_cvs_account` wrapper exposes freshness, completeness and
login/access states. It excludes activation and purchase operations and
serializes calls sharing its bridge. Separate processes and human interaction
still require coordination. Device-local keyed account binding is now implemented
and verifies continuity, not ownership or intended-card selection.

The general DevTools connector can access the selected Chrome profile's open
windows; it is not a CVS-only or inherently read-only permission boundary.
Use a dedicated CVS Chrome profile for regular operation, with a human-managed
login and locally retained session. A restricted wrapper supplies the domain
and action boundaries. Agents can consume the resulting inventory without
needing general browser control.

[Playwright MCP](https://playwright.dev/mcp/configuration/browser-extension)
also supports connecting to a running browser by channel or through an
extension that reuses existing tabs. It is a fallback if the DevTools route
does not work. Traditional remote-debugging command-line switches require a
nondefault data directory in Chrome 136+, as described by
[Chrome's debugging change](https://developer.chrome.com/blog/remote-debugging-port).
Do not copy the user's default profile or cookies to launch an automation
profile. A separately signed-in persistent profile is another implementation
option after proving CVS access in that browser.

## Models needed beyond `Promotion`

Keep `Promotion` as a parsed mechanism. Wrap it with offer and eligibility
metadata rather than treating an ad string as a complete checkout rule.

| Proposed model | Required information |
| --- | --- |
| Product / basket line | Product ID, brand, size, quantity, store, channel, regular/sale price, price timestamp |
| Offer | Stable ID, reward campaign/group ID, issuer, raw terms, dates, eligible products, exclusions, quantity/spend rules, qualification basis, redemption limits |
| Personal coupon | Offer ID, availability/activation state, applicable account, remaining uses and source |
| Reward certificate | Stable ID, type, amount, expiration, activation state, channel restrictions and partial-redemption behavior |
| Account snapshot | Opaque account reference, observed-at time, completeness, coupons, rewards and known campaign progress |

Model ExtraBucks, membership bonus rewards and genuine gift/store credit as
different types. Do not infer which kind of balance the shopper means by
"in-store credit." A single wallet total loses expiration, certificate
denominations and usage restrictions.

## Checkout rules and uncertainties

CVS's [coupon policy](https://www.cvs.com/extracare-cvs/couponpolicy) and its
[published policy PDF](https://www.cvs.com/bizcontent/general/help/coupon-policy.pdf)
support combining qualifying store and manufacturer coupons, but individual
terms still apply. Percent-off coupons target non-sale/non-promotional items.
Two $3-off-$15 transaction coupons require $30 of qualifying purchases. Match
the product's brand, size and quantity; cap discounts at eligible value.

The parser's `stackable=True` and its comment about one offer per issuer are
insufficient as a complete solver constraint. Model item coupons, transaction
coupons and reward redemption separately, with shared spend allocation where
required. Deduplicate advertised digital coupons against personal coupons by
identity; advertising a coupon does not prove it is available to this account.

Offer-specific facts still need observation: which subtotal qualifies for an
ExtraBucks campaign, whether qualifying purchases accumulate across trips,
remaining household limits, and rounding or redemption order. Record those
as explicit rules with evidence. Do not assume a universal pre/post-coupon
threshold, a near-threshold tolerance, or a universal percent-coupon ordering.

[CVS's ExtraCare terms](https://www.cvs.com/extracare/free/terms) distinguish
earned rewards from immediate discounts and describe redemption on a
subsequent transaction. Track when each reward becomes usable; do not spend a
newly earned reward inside the transaction that earns it.

[CVS's member overview](https://www.cvs.com/extracare/overview) describes linking
the account, viewing rewards and sending offers to card. It also says unused
value from a monthly $10 bonus reward is lost if an in-store purchase is below
$10. Preserve the observed terms for each reward rather than applying that
behavior indiscriminately to every kind of credit.

Public flyer data in this repo is historical (September 2026), useful for
regressions only. Fresh data collection and validity checks are required for
actual recommendations. An ad group can cover several products with different
prices; it is not automatically an exact purchasable SKU.

## Optimization objective

The implemented one-checkout milestone is specified in
[the credit optimization strategy](../cvs-credit-optimization.md): individual
certificate selection, expiry-aware wallet outcomes, bounded search and
explainable alternatives. The standalone calculator retains full-face-value
treatment of existing rewards. The optimizer additionally compares usable
opening and ending wallet value under configurable return-day return scenarios, with a shared
future eligible-spend budget when supplied. It exposes bounded-search coverage
and never claims globally optimal or checkout-verified savings.

Offer two clear choices: lowest cash due today, and lowest estimated economic
cost. Display both metrics regardless of which ranks the plans.

Economic cost should include the value of existing rewards consumed and
subtract a conservative value for future rewards earned. Otherwise spending
an existing reward looks like newly created savings. For a simple plan:

```
cash due = eligible checkout subtotal - usable existing rewards + tax
economic cost = cash due + value of rewards consumed - value of rewards earned
```

Value can differ by certificate expiry and the shopper's expected future use.
Forfeited certificate value counts as consumed value. Never value new rewards
as cash without explaining that assumption. A plan must also respect product
eligibility, per-coupon limits and reward-specific redemption rules.

Default to the requested list with one transaction and no unrequested extras.
Show threshold-filling extras as an optional alternative only when permitted;
compare their incremental cost against incremental reward value. Avoid buying
unneeded products merely to maximize a displayed discount percentage.

For small lists, start by enumerating bounded legal combinations using a
deterministic checkout evaluator. Keep the evaluator separate from the search
strategy. Introduce a larger solver only if real basket sizes require it.

## Delivery sequence

1. **Account and offer foundation.** Define the snapshot/offer models, inspect
   the shopper's linked account, add a read-only account adapter and fresh ad
   collection. Validate with redacted fixtures and clear login/freshness states.
2. **One-transaction planner.** Resolve exact products, enforce quantities and
   eligibility, calculate coupons/reward redemption, and expose JSON CLI
   results. Test shared thresholds, duplicate coupons, sale exclusions, expired
   rewards, household caps and unknown prices. Check one real checkout against
   its receipt when available.
3. **Reusable agent tools.** Wrap the provider in MCP, add rechecks and a short
   agent usage guide. Refresh account data at the start of each shopping task;
   never reuse a plan after its source offers expire.
4. **Optional transaction sequencing.** Compare one checkout against a bounded
   two-checkout alternative, respecting reward availability and campaign
   progress. Report the extra checkout effort and avoid duplicate rewards.

Linked-account access, fresh inventory and targeted detail traversal beyond the
first card batch work. Store discovery, catalog lookup and matching are implemented
with synthetic coverage. Catalog discovery and exact identity checks have been live-verified without
publishing personal store/product observations. Web/pickup pricing remains
distinct from shelf-price evidence. Public ad search is available. A price resolver accepts explicitly
attested shelf/receipt/store/ad evidence; a conditional evaluator models one
basket with separate discounts, reward spending/waste and future rewards.
See [the calculation contract](../cvs-basket-calculation.md). Synthetic tests
cover shared thresholds, manufacturer overlap, sale exclusions, limits,
unknown inputs and conservation of cents. Preparation now binds fresh account
facts to basket-specific confirmations, and bounded selection compares coupon
assignments and certificate orders. A normalized receipt comparator is implemented;
its first real-world validation is deferred to the shopper's next CVS purchase.
All 283 CVS tests pass; the full suite has 462 passes and the three baseline HEB
parser failures. Automatic complete rule extraction, broader product layouts,
automatic shelf prices and multiple-checkout planning remain dependencies. No particular shopping list
is required to improve these reusable capabilities.
