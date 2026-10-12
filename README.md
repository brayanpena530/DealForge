# DealForge
An agent-friendly optimization engine for coupons, promotions, and loyalty rewards.

## CVS account sync for agents

Shopper ZIP, store, timezone and return-day preferences are loaded from a private
file outside the repository: `%LOCALAPPDATA%/DealForge/preferences.json` on Windows
or `$XDG_CONFIG_HOME/DealForge/preferences.json` (default `~/.config`) elsewhere.
`DEALFORGE_PREFERENCES_FILE` can select another external file. Explicit tool/CLI
options override local defaults; no live ZIP/store is built into the project.
See [private configuration](docs/private-configuration.md). Example ZIP `00000`
and store IDs in documentation/tests are fictional placeholders.

The project's MCP server uses [Playwright with a dedicated Chrome profile](docs/cvs-playwright.md),
without Chrome DevTools MCP. Real sign-in, account sync and session reuse after
a browser restart are verified. Install Chrome and `python -m pip install -e ".[cvs]"`,
then sign in directly in the browser opened by `dealforge-cvs login`.

Refresh account inventory and catalog data as JSON:

```sh
dealforge-cvs sync --browser playwright
dealforge-cvs sync --browser playwright --detail-query Colgate --detail-limit 3
dealforge-cvs search "Colgate total toothpaste" --browser playwright --zip 00000
dealforge-cvs stores --browser playwright --zip 00000
dealforge-cvs ad cereal --zip 00000
dealforge-cvs evaluate --input tests/fixtures/cvs_basket_example.json
dealforge-cvs optimize --input tests/fixtures/cvs_optimizer_example.json
dealforge-cvs plan --browser playwright --input /path/outside/repo/cvs-trip.json
# Direct server launches: set DEALFORGE_CVS_BROWSER=playwright first.
dealforge-cvs-mcp  # Thirteen CVS account, catalog and purchase-planning tools
```

The collector checks a fresh visible count, preserves unknown eligibility and
returns explicit login/access/connection failures. Results remain partial; it
binds account labels to the observed card on this device, searches relevant
coupon details, and returns evidenced purchase requirements. Cached terms are
labeled; balances and activation state require fresh observations. The MCP
wrapper reconnects on the next call after a browser failure. A local basket
calculator separates coupons, existing rewards consumed and future rewards,
with explicit terms and unresolved checks. A bounded one-checkout optimizer
compares coupon combinations and individual reward certificates, showing cash
due and wallet outcomes for configurable return-day scenarios. It requires
fresh account continuity, exact prices and confirmed terms; results remain
conditional estimates. It never activates offers. The project's `.codex/config.toml`
enables the CVS-only MCP wrapper via `uv`; a fresh host session may be needed.
The existing Chrome MCP backend remains available with `--browser chrome_mcp`;
it requires Node/npx and signed-in Chrome with remote debugging enabled.
Local snapshot reading is also available with
`dealforge-cvs wallet --snapshot /path/outside/repo/cvs-account.json`.
Keep personal data outside the repository, without credentials or session material.

Product search/detail are live-verified in the signed-in Chrome session; coupon
matching has synthetic coverage. Web/pickup prices are labeled
online and never substituted for unknown in-store shelf prices. Store context
comes from private local preferences and is checked on every lookup.

Public weekly-ad search has been live-verified; the location is kept private. Ad groups need exact SKU and
store applicability checks. In-store price resolution accepts separate caller
evidence; automatic shelf-price collection remains unverified. Basket calculations
have synthetic coverage and remain conditional estimates before tax/fees.
Real receipt validation is planned for the shopper's next CVS purchase.

`plan_cvs_trip` combines the shopping-list workflow: fresh account/store/product
collection, source-bound offer eligibility, price gaps and credit optimization.
It returns exact product choices and actionable missing evidence before estimating
a complete basket. See [the trip planning guide](docs/cvs-trip-planning.md) for
shopping-list inputs, confirmations and remaining eligibility limitations.

## CVS status and development handoff

The core reusable, read-only workflow is built: account/session reuse, store and
product discovery, offer assessment, price evidence resolution, and one-checkout
coupon/credit optimization. Individual certificates, expiration, forfeited value,
and configurable return-day scenarios are supported. Agents still need exact product
choices, confirmed terms and appropriate price evidence for a reliable estimate.

Two implementation gaps remain beyond tool validation:

1. **Automatic in-store prices:** no reliable automatic shelf-price source is
   implemented. Use fresh, exact-SKU/store shelf labels, store quotes or applicable
   ads. Online prices cannot replace missing in-store evidence.
2. **More complete coupon-rule support:** some category/size exclusions, stacking
   restrictions and discount caps need further interpretation or model support.
   Unconfirmed or unsupported offers stay pending and are excluded from savings.

The next development priority is broader coupon-rule support. Automatic shelf
pricing needs a reliable source first. Multiple-checkout optimization, suggested
substitutions and hosted browser access are optional future enhancements.

For the next agent's testing and implementation handoff:

1. Read [the browser setup](docs/cvs-playwright.md) and
   [trip planning contract](docs/cvs-trip-planning.md), then reload the project
   session to expose `plan_cvs_trip` and `assess_cvs_offer_eligibility`.
2. Test a shopping list with the signed-in dedicated profile and selected store
   using private local preferences. Check product-choice tasks, account continuity, missing shelf
   prices and unsupported terms; none should silently produce assumed savings.
3. Add exact product choices, fresh price evidence and source-bound confirmations;
   inspect the recommended, lowest-cash and no-credit alternatives before tax.
4. Continue coupon-rule support with regression cases for observed restrictions.
   Validate the next real purchase using `reconcile_cvs_receipt` and refresh the
   wallet afterward; synthetic tests do not prove POS acceptance.

Current validation: 363 CVS tests pass, including both browser transport/restart
integrations. The full suite has 550 passes and three existing H-E-B parser
failures. Read-only live checks verified count reconciliation and correctly withheld
cash recommendations when shelf prices were missing. Account-specific observations
are kept outside the repository. Real receipt validation remains pending the next purchase.

See [the CVS agent guide](docs/cvs-agent-guide.md) for the JSON contract and
[price/rule schemas and a basket example](docs/cvs-basket-calculation.md),
[credit optimization and receipt comparison](docs/cvs-credit-optimization.md), plus
[the integration design](docs/research/cvs-agent-integration.md) for the remaining work.
