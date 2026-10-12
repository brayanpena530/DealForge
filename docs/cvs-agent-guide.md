# CVS account inventory for agents

Agents can refresh personal rewards and coupons through `sync_cvs_account`
(MCP) or `dealforge-cvs sync --browser playwright` (JSON CLI), using a dedicated
persistent Chrome profile controlled directly by Playwright. The existing Chrome
MCP backend remains available. Product search/detail, store discovery and coupon/product matching are
implemented with synthetic coverage. Product search, exact SKU/price lookup
and store discovery were live-verified on October 10, 2026. Fresh public ad
search, price evidence resolution and a conditional single-basket calculator
are now available, together with a bounded one-checkout credit optimizer and
normalized receipt comparison. Automatic shelf-price collection, complete rule
extraction, real POS validation and multiple-checkout planning remain unresolved. Account sync and
catalog results remain `partial`.

## Product and price workflow

For a whole shopping list, start with `plan_cvs_trip`; see
[the trip planning guide](cvs-trip-planning.md). It combines these lower-level
tools and returns product choices, pending terms/prices and conditional credit
alternatives. `assess_cvs_offer_eligibility` also exposes the pure local assessment.

ZIP, store, timezone and shopping-cadence preferences are private local settings.
Each lookup reads the selected store and can pin a requested `store_id`.
The ZIP `00000` and store `10001` below are fictional placeholders, not live defaults.

| Tool | Purpose |
| --- | --- |
| `find_cvs_stores(zip_code="00000")` | Read verified store identities and nearby address candidates without changing the selection; missing IDs/ZIPs stay null |
| `search_cvs_products(query, brand="", size="", store_id=None)` | Ranked candidates, product-family/SKU IDs, match gaps and observed online prices |
| `get_cvs_product(product_url, store_id=None)` | Read a product page and distinguish requested SKU from independently observed selected SKU |
| `match_cvs_products(products, offer_rules)` | Local comparison of up to 25 products and 25 selected rules; distinguishes observed SKU membership from family/text candidates |
| `search_cvs_weekly_ad(query, zip_code="00000")` | Current public ad groups, dates and parsed promotion candidates; no Chrome connection needed |
| `resolve_cvs_prices(lines, channel, store_id)` | Exact-SKU, fresh price evidence for `in_store`, `pickup` or `online`; returns per-item unit IDs |
| `evaluate_cvs_basket(lines, channel, store_id, ...)` | Conditional one-basket calculation using explicit rules; reports discounts, old rewards used/forfeited and future rewards separately |
| `prepare_cvs_purchase(lines, account_sync, store_id, ...)` | Bind account records to this basket and produce rule drafts with explicit confirmation gaps |
| `optimize_cvs_purchase(lines, account_sync, store_id, ...)` | Bounded coupon/certificate search with recommended, lowest-cash and no-credit alternatives, using configured return-day scenarios |
| `reconcile_cvs_receipt(plan, receipt)` | Compare a normalized receipt with a plan, including product and certificate identities; fixtures never claim real validation |

Catalog calls use a separate background CVS tab and share the account bridge's
queue/lock. They preserve Coupons & Rewards and do not switch variants, change
the selected store, activate coupons or modify a cart. If the selected store
does not match the requested ZIP/store ID, they return `needs_input` with store
candidates. Human interaction or another collector can still change context;
an observed store change suppresses the price result.

Every observed price carries `amount` as a decimal string or null, `currency`,
`channel`, source URL and `observed_at`. Current web prices use `channel=online`
and `in_store_verified=false`, with `in_store_price=null`. Store identity does
not make this a shelf price. CVS states that pickup receives online pricing and
online/in-store prices may differ in its [pickup information](https://www.cvs.com/content/store-pickup).
Its [shopping FAQ](https://www.cvs.com/retail/help/help-subtopic-online-shopping-faqs)
also directs shoppers to local stores for merchandise/pricing information.
No automated shelf-price source has yet been verified. The price resolver
accepts separately evidenced shelf labels, receipts, store quotes or applicable
unit-price ads. These are caller attestations. Ambiguous, missing or
conflicting displayed prices stay null, rather than choosing the cheapest.
Multi-variant search cards expose `displayed_price_range` and leave `amount=null`;
resolve the selected SKU through `get_cvs_product` before comparing costs.

Search results stay `text_candidate` even when every query token matches. Brand
and size constraints expose gaps rather than authorizing substitutions. A family
ID can cover variants; `selected_sku_verified` requires independent product-page
evidence. A requested/observed SKU disagreement suppresses the price. Availability
is reported only for an observed selected-store indicator. Shipping stock cannot
establish pickup availability.

Relevant account detail visits now attach `eligible_products_observed` from the
offer's Eligible Products carousel, with an observation timestamp. This list is
partial, is never cached and does not establish checkout eligibility. Matching
prioritizes the same product/SKU pair with fresh (24-hour default) membership
evidence, then weaker family/text candidates. Missing entries do not establish
ineligibility. Activation, quantities, spend basis, issuer, channel, exclusions,
limits and stacking still require checks before savings can be calculated.

```sh
dealforge-cvs stores --zip 00000
dealforge-cvs search "Colgate total toothpaste" --zip 00000 --store-id 10001 --brand Colgate
dealforge-cvs product "https://www.cvs.com/shop/example-prodid-123?skuId=456" --zip 00000
```

The last URL is a structural example, not a real product. Use URLs returned by
search or account eligibility observations. MCP tool discovery/protocol and
synthetic store/product/price/matching tests pass. Personal catalog observations
and selected-store details are not published. The live check exposed flat search cards and split
currency nodes; regression tests cover those layouts, image carousels, loading
placeholders and exclusion of coupon/sponsored prices. ZIP-search cards currently
omit store IDs and postal codes; those address candidates retain null identity
fields and the search ZIP separately. A ZIP search can include nearby ZIPs.
All 363 CVS tests pass, including account-to-plan, browser transport/restart,
price/ad/checkout and receipt regressions. The full suite has 550 passes and three existing HEB parser failures,
separate from CVS. Real receipt validation is deferred to the shopper's next purchase.

## Agent workflow

For fresh ads, price evidence, calculator rule schemas and a complete synthetic
basket request, read [the basket calculation guide](cvs-basket-calculation.md).
The evaluator does not infer missing terms from account/ad candidates.
For rule confirmations, wallet scenarios, bounded selection and the next-purchase
receipt workflow, read [the credit optimization guide](cvs-credit-optimization.md).
`prepare_cvs_purchase` produces fingerprints tied to current account records and
basket identities. Confirm their outstanding terms, then call
`optimize_cvs_purchase`; unresolved rules are disclosed and excluded. A fresh
matched account binding is required for real account input. Compare the actual
next purchase with `reconcile_cvs_receipt` and refresh the wallet afterward.

1. Refresh with `sync_cvs_account()` at the start of each shopping task.
2. Inspect the snapshot's individual rewards and coupon text. Use a distinct,
   stable opaque `account_ref` per account. The first observed card creates a
   device-local binding (`first_seen`); later `matched` results confirm continuity
   on this device, not ownership or that it is the shopper's intended card.
   A different card under that label returns `account_changed_for_label` with
   no snapshot. `account_identity_verified=true` means continuity only.
3. Fetch relevant details with, for example, `detail_query="Colgate"` and
   `detail_limit=3`. Every call refreshes inventory. The query filters card text;
   it does not establish product eligibility. `definitions_observed` identifies
   records enriched with definition terms. Targeted visits use the observed
   rewards search in the page's main section, including offers beyond the first
   rendered cards. The inventory count is captured before filtering.
4. Check collection counts, completeness flags, `definition_coverage` and `review`.
   Inspect `offer_rules` for evidenced discount, minimum spend/quantity and
   exclusions. Rules stay `partial` and expose conflicts and unknowns, including
   product membership, stacking and spend qualification basis. Missing terms or
   unknown availability must not become confirmed savings.
5. Resolve exact products, store prices and checkout rules before recommending
   a purchase. Sync never sends offers to card, changes a cart or buys anything.

An observed-response normalizer is also implemented in
`providers/cvs/account_response.py`. It accepts a successful
`getEcCustomerProfile` response, an opaque account reference, an aware
observation timestamp and an optional independently observed page count.
It returns a `ProfileInventoryDraft`, not a live sync result. Use
`normalize_profile_response(...).to_dict()` for its JSON-compatible output.
`providers/cvs/account_sync.py` now collects this response automatically.

The draft copies only selected coupon/reward fields, hashes source identities,
and ignores account details, club preferences, summary floats and duplicate raw
lists. It filters explicitly hidden/suppressed records and reconciles the
collected count against the visible UI count. Missing identities, changed
categories and conflicting records prevent a complete-collection claim.
All drafts remain `partial`, with `terms_complete=false`; channel and issuer
remain unknown. Observed definitions can enrich terms, but cannot overwrite
account activation, certificate value or expiration. Repository tests use
synthetic data; live checks are separate and never stored as fixtures.

## Browser connection

The [Playwright backend](cvs-playwright.md) is now configured for the project's
MCP server. It launches ordinary Chrome with a dedicated persistent profile and
attaches directly, without Chrome DevTools MCP or approval prompts. Real sign-in,
fresh inventory, targeted details, card continuity and a full browser
restart passed on October 12. Use `dealforge-cvs login` for interactive sign-in,
and `--browser playwright` or `DEALFORGE_CVS_BROWSER=playwright` for standalone
CLI/MCP processes. The host must reload the updated project configuration.

The existing Chrome MCP option is described below and remains available as a
fallback. It uses the shopper's already-open, debugging-enabled Chrome window.

The project's [Codex configuration](../.codex/config.toml) defines the
`cvs-account` MCP server for a trusted project. It connects to existing Chrome
with `--autoConnect`; keep Chrome running, enable **Allow remote debugging for
this browser instance** at `chrome://inspect/#remote-debugging`, and keep the
signed-in CVS Coupons & Rewards tab open in that same browser/profile. Accept
Chrome's connection prompt if shown. The address Chrome displays is a browser
debugging endpoint, not an MCP server URL to paste into the host.

The server exposes eleven CVS-specific tools: account sync, three catalog
tools, local matching, public ad search, price resolution, basket evaluation,
purchase preparation, credit optimization and receipt comparison.
The earlier `cvs-chrome` inspection entry
is disabled. Internally, the wrapper checks the CVS domain before page actions
and exports only the two observed account/definition routes. It never evaluates
browser JavaScript or replays private requests. One bridge stays alive across
calls, with browser access serialized in an owning worker task. The MCP server
starts without opening Chrome; its first sync connects lazily. After a connection
failure, the next sync creates a fresh bridge. It never loops over permission
prompts or retries a blocked account indefinitely. Separately running
collectors and human interaction are not serialized.

Loading changed MCP configuration may require a fresh host session; T3 hot reload
has not been verified. Global MCP settings were not changed. The bridge's raw
capabilities are not directly exposed to agents by this wrapper.

Signed-in rewards access and fresh inventory reconciliation have been verified.
Personal account counts, balances, offer selections and browser troubleshooting
details are intentionally omitted. Keep one MCP client alive to avoid repeated
connections and debugging prompts.

Response-body inspection on October 10 verified the following private page
responses, collected through the account's normal UI:

| Response | Use |
| --- | --- |
| `data.getEcCustomerProfile.couponCategories` | Categorized `extrabucks`, `personalizedDeals`, `otherDeals`; includes more records than the initial rendered cards |
| `data.extracareCouponDefinition.extracareCouponDefinitions` | A selected offer's terms, description and purchase-requirement fields, loaded by More details |

Both came from `/retail/client/experience/v2/load/<operation-id>` routes. Other
experience responses contain sensitive profile data and must be ignored. In
the observed profile, excluding records with `viewableInd=N` reconciled the
categorized collection exactly with the visible count. This is one account
observation, not proof that all future responses are complete or unpaginated.

The profile uses `cmpgnId`, `cpnNbr` and, for individual certificates,
`cpnSeqNbr`. Raw certificate/sequence numbers must not be returned as public
record IDs. DOM UIDs are temporary element locators. Sync compares a fresh count
on each run. Bounded detail collection can reach definitions beyond the initial rendered batch.
These routes are not a documented public consumer API. Private account observations
are not repository fixtures; fresh count reconciliation is required on each run.

The profile's `redeemableInd=Y, loadableInd=N` matched an on-card reward;
`redeemableInd=N, loadableInd=Y` matched rewards available to send. The generic
definition for an on-card reward still had `loadableInd=Y` and `activeInd=false`.
Definition flags therefore cannot supply card state. An unambiguous On card or
Send to card observation in the selected offer's header can fill an unknown
state after joining its fresh definition identity. A conflict with account flags
returns unknown state and a review reason. Activation state is never cached.
The source `mfrInd` and
rendered MFR label disagreed for an observed coupon; `redeemEligibleChannelCd=B`
was the only channel value observed. Keep issuer and channel unknown until
validated. Reward partial-redemption behavior also remains unknown.

Relevant definitions and fresh UI state can enrich explicit discount/quantity
candidates. Cached terms never establish current activation or eligibility.
Only a keyed account hash is persisted. Synthetic CVS tests cover account switches, search boundaries,
cache expiry/privacy, rule conflicts, bridge recovery and MCP serialization.

When exporting responses via Chrome DevTools MCP, use `.network-response`:
the server replaces a different requested file extension. Read and delete
temporary exports locally, without outputting headers, cookies or unrelated
account fields. Do not put full live responses into repository fixtures.

## Run

Install Node/npx and the optional CVS dependency for live sync:

```sh
python -m pip install -e ".[cvs]"
dealforge-cvs sync
dealforge-cvs sync --detail-query Colgate --detail-limit 3 --timeout-seconds 90
dealforge-cvs-mcp
```

The project MCP configuration starts
`uv run --no-project --with-editable ".[cvs]" dealforge-cvs-mcp`. It requires
`uv` on the host's PATH and the project directory as its working directory.
The MCP protocol and live call were tested with the official Python SDK 1.26.0;
the supported dependency range is 1.x.

Sync options: `account_ref` is an opaque label (default `cvs-personal`),
`page_id` selects a CVS tab when several ExtraCare tabs are open, `detail_limit`
is 0â€“25 (default 0, inventory only), `detail_query` filters detail visits, and
`timeout_seconds` is 10â€“180 (default 90). CLI flags use hyphens. Each run requires
a fresh profile after navigation and an independently observed count with All
selected. Network-list pages and detail scrolling are bounded. Unexpected
operation routes fail closed. Response exports are parsed locally and deleted.
An interrupted detail visit triggers a bounded attempt to return to Savings.

Sync returns one JSON envelope with `snapshot`, `wallet`, collection checks,
definition coverage and review reasons. Extract only its `snapshot` object to
use the local snapshot reader below. Sync does not persist account snapshots.
It stores a random local HMAC key, an opaque account binding and allowlisted
description/terms text under `%LOCALAPPDATA%/DealForge/cvs` on Windows, or
`$XDG_STATE_HOME/DealForge/cvs` (default `~/.local/state/DealForge/cvs`) elsewhere.
No raw card number, source coupon identity, certificate sequence, credentials,
activation flags, reward balances or raw response is cached. This cache is local
personal data, not encrypted; Windows uses inherited user-directory permissions.
Definition text expires after 24 hours and is invalidated when source descriptions
or expiry change. `cached_records` and `cached_definition_observed_at` distinguish
cached terms from fresh observations. Every sync still requires a fresh profile;
cached terms never supply a balance or activation state after login/access failure.
Keep any saved personal output outside the repository.

After `python -m pip install -e .`:

```sh
dealforge-cvs wallet --snapshot /path/outside/repo/cvs-account.json
```

`python -m dealforge.cvs` exposes the same interface. Standard output contains
one JSON result; validation failures return `invalid_input` and exit code 2.
Omitting `--snapshot` returns `needs_input`. The **wallet** command never updates
its input file or contacts CVS.

A deterministic demonstration uses synthetic data, not the shopper's account:

```sh
python -m dealforge.cvs wallet --snapshot tests/fixtures/cvs_account_example.json --as-of 2026-10-09T11:00:00-05:00
```

The example returns `partial` and identifies its fixture source. Do not present
its dollar values as the shopper's rewards. Actual personal snapshots belong
outside the repository, without credentials or full card numbers.

## Snapshot version 1

Use the [synthetic example](../tests/fixtures/cvs_account_example.json) as a
structural reference. Required top-level fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1` |
| `account_ref` | Nonempty opaque reference, e.g. `cvs-personal`; not a card number |
| `observed_at` | ISO timestamp with UTC offset, e.g. `2026-10-09T10:00:00-05:00` |
| `source` | `manual`, `browser` or `fixture`; use the actual collection method |
| `status` | `ready`, `partial`, `needs_login` or `blocked_access` |
| `rewards_complete`, `coupons_complete` | Booleans; true only if each collection was actually completed |
| `rewards`, `coupons` | Arrays; an empty array only proves absence when collection is complete |

Each reward needs a unique `reward_id` and nonempty `raw_terms`.
Optional/unknown fields remain explicit in the normalized result:

- `kind`: `extrabucks`, `membership_bonus`, `gift_card`, `store_credit` or `unknown`.
- `amount`: nonnegative decimal string with at most 12 integer digits and two
  decimal places, or null; floats are rejected.
- `expires_on`: `YYYY-MM-DD` or null when unknown. Null does not mean never expires.
- `state`: `available`, `on_card`, `redeemed`, `unavailable` or `unknown`.
- `channel`: `in_store`, `online`, `both` or `unknown`.
- `partial_redemption`: boolean or null, taken from actual terms; do not infer
  whether unused value survives redemption.

Each coupon needs a unique `coupon_id`, nonempty `headline` and nonempty
`raw_terms`. Its other fields are `expires_on`, `state`, `channel`, and
`issuer` (`mfr_coupon`, `store_coupon` or null). Preserve the complete terms;
the separate `offer_rules` field extracts explicit purchase requirements but
does not verify product eligibility or checkout savings.
IDs should originate from source offer identities; duplicates are rejected
rather than silently adding value twice.

Unsupported fields are rejected. This provides a narrow data contract, not
automatic redaction: collectors must exclude sensitive identifiers and session
material from all allowed fields too.

## Result handling

| Result status | Agent action |
| --- | --- |
| `ready` | Snapshot is fresh and basic inventory fields are known; checkout still requires product eligibility verification |
| `partial` | Complete collection or verify missing terms; fixture data also has this status |
| `stale` | Refresh before recommending a purchase |
| `needs_login` | Ask for sign-in in a supported connected browser |
| `blocked_access` | Report the access problem; do not retry indefinitely |
| `browser_unavailable` | Fix Chrome/debugging permission or reopen the tab before retrying |
| `needs_input` / `invalid_input` | Follow the returned `next_action` or correct the schema |

`known_unexpired_face_value_by_kind` is a partial inventory by reward type, not
a spendable balance. Expired, redeemed and unavailable rewards are excluded.
Unknown value, expiry, type or availability never becomes a zero-value fact.
Totals are suppressed entirely when stale or when login/access is unresolved.
`balance_is_checkout_verified` is always false in this foundation.

The live wallet summary omits duplicate account records and groups equivalent
warnings in `warning_counts`. Sync failures include a safe reason and next action,
with `snapshot=null`. Login/access loss during details suppresses the account
result. A detail timeout after valid inventory can retain that partial snapshot,
with an interruption reason and explicit missing definitions. A failed fresh
profile capture never substitutes an old snapshot.

The default freshness limit is 24 hours (a product default, not CVS policy).
Use `--max-age-hours` to tighten it. `--as-of` must carry the shopping location's
offset; otherwise the CLI uses the machine's local time. Expiry comparison uses
that local calendar date, includes rewards expiring today and flags them.
An observation timestamp later than the evaluation time is rejected.

The CLI echoes normalized coupons and reward terms to its caller. Treat that
result as personal account data; do not publish it or put it into test fixtures.
