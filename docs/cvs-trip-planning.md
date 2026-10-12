# CVS trip planning for agents

Start each shopping task with `plan_cvs_trip`. It refreshes the signed-in account,
checks the selected store using private local ZIP/store preferences, collects product choices and relevant
offer details, resolves price evidence, and runs the existing one-checkout
coupon/credit optimizer. Return scenarios come from the local planning profile or explicit input.
The tool is read-only; the shopper handles sign-in and any coupon activation.

## Shopping-list input

```json
{
  "items": [
    {"line_id": "toothpaste", "quantity": 2,
     "query": "Colgate toothpaste", "brand": "Colgate", "offer_query": "Colgate"},
    {"line_id": "soap", "quantity": 1, "query": "Dove bar soap", "brand": "Dove"}
  ],
  "zip_code": "00000",
  "channel": "in_store",
  "detail_limit": 10,
  "timeout_seconds": 180
}
```

There are 1–10 items, each with a unique `line_id`, quantity 1–100, and a query or
CVS product URL. Queries return up to five candidates per item. They do not select
the first result. Inspect candidates with `get_cvs_product`, then repeat the
planner with the chosen **exact SKU URL** in each item's `product_url`. Verify
brand, size and variant against the original list before selecting it. Family
URLs, changed variants and unverified selected SKUs remain product-choice tasks.
The planner never substitutes a different item or recommends an incomplete basket.

`store_id` is optional. The tool observes the current selected store, requires
its ZIP to match, and pins that store throughout catalog collection. It does not
select a store on the shopper's behalf. A specified different store returns a
store-selection task.

`offer_query` filters normal More details visits; it defaults to brand, then query.
Up to ten distinct filters share `detail_limit` (0–25 visits), with a common
collection deadline. General whole-purchase offers are also retained as review
candidates even when their text shares no item words. Description matching and
filtered visits can miss other relevant offers; coverage is disclosed.

## Prices and account confirmations

Each selected item can carry `price_evidence` in the schema documented in
[the basket calculation guide](cvs-basket-calculation.md). In-store prices need
fresh exact-SKU evidence for the same store, such as a caller-attested shelf
label or an applicable unit-price weekly ad. A web price never fills this gap.
Known prices for part of the list do not produce a whole-list cash estimate.
Fresh observed online prices can be used for pickup/online, when visible.

The planner refreshes the account at the beginning and after catalog collection.
A matched device binding is required. `first_seen`, changed accounts, sign-in
failures and other unavailable account states cannot produce a recommendation.
The account response is compact: collection/binding/definition diagnostics and
individual rewards, rather than another copy of every coupon in the account.

The `eligibility` result contains relevant coupon assessments and `preparation`
drafts for selected coupons and individual reward certificates. Exact, fresh
Eligible Products membership supplies **suggested** physical `unit_ids`. It does
not mark product eligibility, stacking or terms verified. Text/family matches and
absence from a partial carousel do not establish eligibility or ineligibility.
`eligible_spend_before_coupons` is a diagnostic comparison; it does not settle the
offer's pre/post-coupon qualification basis. Observed quantities/spend below a
threshold indicate incomplete evidence, not an exhaustive product exclusion.

Use each draft's `source_fingerprint` to submit fresh confirmations in the
planner's `confirmations` argument, following
[the credit optimization schema](cvs-credit-optimization.md). Fingerprints now
bind restriction text, extracted term candidates and observed SKU membership,
as well as account records and basket identities. Reconfirm after those facts
change. Inventory amount, expiration and activation remain immutable.

Literal term extraction recognizes a bounded set of explicit issuer/channel
statements, quantity/spend expressions, qualification basis, and purchase limits.
Per-purchase limits never imply an account's remaining uses. Unsupported stacking
prohibitions and discount caps remain pending, even if a caller marks a verification
flag true. Clear trial/travel-size name conflicts block affected units. Other
category/size restrictions and policy prose still require source review; extraction
is not a complete CVS policy interpreter. Parsed candidates never automatically
grant savings or silently default missing rules.

Read-only live checks verified account/catalog/assessment orchestration and
withheld a cash recommendation when in-store price evidence was missing.
Account counts, selected store and offer details are intentionally not published.
This does not validate checkout savings.

Up to 25 relevant coupons enter preparation, with explicitly selected confirmations
taking priority. The assessment compares all observed rule candidates in batches
and reports truncation. Pending rules are excluded and disclosed. Additional
earning campaigns can be passed as fully evidenced `reward_offers`; weekly-ad
groups do not become earning rules automatically. Set `extra_care_ready=true`
only when the qualifying linked card will be used at checkout.

## Results and retry workflow

| Field | Meaning |
| --- | --- |
| `items`, `lines` | Product choices and selected exact basket observations |
| `account`, `detail_collection` | Fresh account/definition diagnostics and bounded query scope |
| `price_resolution`, `eligibility` | Price gaps, literal terms, suggested units, fingerprints and pending checks |
| `recommended`, `optimization` | Conditional recommendation, lowest-cash/no-credit alternatives, expiration and wallet scenarios |
| `next_actions` | Product/store/price/account/term/card tasks needed to improve the plan |

Resolve `next_actions`, then repeat the same list with chosen SKU URLs, fresh price
evidence and confirmations. Each call refreshes again. Stable source facts allow
fresh confirmations to remain valid; changed activation, terms, membership or
basket identities require new fingerprints. Price freshness is checked separately.

All plans have `checkout_verified=false` and `execution_ready=false`. A
`conditional_estimate` applies only to the modeled, confirmed rule scope. `partial`
discloses excluded rules or incomplete inventory; `needs_input` can retain useful
completed stages while withholding the recommendation. Taxes and fees remain
excluded. Stock and POS acceptance need separate verification. Compare the next
actual purchase using `reconcile_cvs_receipt`; synthetic tests do not validate POS.

## CLI and lower-level assessment

Keep shopping files outside the repository:

```sh
dealforge-cvs plan --browser playwright --input /path/outside/repo/cvs-trip.json
dealforge-cvs eligibility --input /path/outside/repo/cvs-eligibility.json
```

The CLI uses the same planner. MCP `assess_cvs_offer_eligibility` provides the pure
assessment separately, using normalized `lines`, `account_sync`, `store_id`, and
optional confirmations. It has no browser access and supports explicitly opted-in
synthetic fixtures. The live planner accepts no imported account snapshot and has
no fixture override. Reload the project session after MCP configuration changes
to discover both new tools.
