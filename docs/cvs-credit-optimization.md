# CVS credit optimizer before merge

Implemented October 10, 2026: bounded single-checkout selection, physical-unit
assignments, account-bound rule preparation, wallet transitions, expiry-aware
ranking, JSON CLI/MCP and receipt reconciliation. Coverage is synthetic end to
end, including the account collector and real MCP protocol. Real receipt validation remains pending; real receipt/POS validation
has not occurred. Two-checkout planning remains deferred. Changes are unmerged.

## Default recommendation

Buy the requested items, use one checkout, reduce cash outlay without needlessly
destroying usable rewards, and consider whether saved rewards can be used before
expiration. No unrequested quantities, brands or sizes. No extras by default.
Produce a recommended plan plus the lowest-cash alternative when materially
different; show the difference in wallet value and effort. Never rank a plan
solely by the advertised reward amount or percentage saved.

Return-day scenarios are configurable in the private local profile or explicit
input. Without a configured profile, the generic fallback scenarios are 7 and
30 days. Flag expiry risk across the requested scenarios and expose when the
recommendation changes. Avoid accumulating more credit than expected eligible
purchases before its expiration. No personal shopping schedule is published.

The planning profile should record the next expected CVS visit (or unknown),
optional cash budget, whether two checkouts are allowed, and whether specific
useful extras/substitutions are allowed. Unknown shopping frequency must remain
an explicit assumption, not an invented probability. It must not prevent
single-trip comparison; show when a different future-use assumption changes
the recommendation.

## Individual certificates, not an aggregate credit balance

Preserve each observed reward's opaque identity, kind, face value, expiration,
activation, channel, eligible products and partial-redemption terms. Keep
ExtraBucks, monthly membership bonuses, gift cards and refund/store credit
separate. The current evaluator supports ExtraBucks and membership bonuses;
other instruments need separate payment rules if actually present.

Monthly bonuses are included only when observed in this account. Their unused
remainder is lost after a qualifying in-store redemption; that behavior must
not be generalized to every ExtraBucks certificate. CVS also imposes a minimum
$10 online purchase for the monthly bonus. See
[the membership terms](https://www.cvs.com/extracare/plus/terms).

Prioritize earlier expiry when other costs are comparable, but evaluate
denominations and overage together. A smaller, later-expiring certificate can
fit a basket better than a larger, earlier-expiring certificate. Spending
part of an expiring certificate may still be sensible when the remaining
value cannot realistically be redeemed before expiration. Do not buy unwanted
products just to make the displayed forfeiture zero.

## Rank wallet outcomes as well as checkout cash

The standalone calculator's economic metric charges existing rewards at full
face value and discounts newly earned rewards with one configurable factor.
The optimizer preserves that baseline in its calculation and adds the separate
expiry-aware scenario scores described below.

For each certificate, estimate the value of preserving it based on eligible
planned purchases before its expiration. An explicit expected future visit
helps establish this; expiration tomorrow alone does not prove a reward has
no future use. Newly earned rewards also need their own expiration and
redemption conditions. Unknown new-reward terms must not create confidently
valued savings.

Compare plans using:

```
score = cash paid now
      + usable value of opening wallet
      - usable value of ending wallet
      + configured extra checkout effort
```

Ending wallet includes preserved old rewards, any valid carried remainder,
and usable new rewards. Opening value is constant across plans. Spent and
forfeited value leave the wallet exactly once. Expiration and consumption are
reported separately; do not subtract a future reward from today's cash or
count an old reward spent as newly earned savings.

When future-use assumptions are uncertain, compare explicit scenarios rather
than inventing precise redemption probabilities. Display cash, old rewards
used, value forfeited, new rewards, retained wallet and upcoming expiry under
each scenario. A cash budget is a constraint; otherwise recommend the plan
with lower combined cash/usable-wallet cost, breaking ties by fewer checkouts
and lower forfeiture.

## Search strategy

Start with bounded enumeration for the requested basket. Explore eligible
coupon selections, individual reward subsets and meaningful redemption orders;
reuse the deterministic evaluator instead of duplicating checkout arithmetic.
Always include the no-credit plan. Deduplicate economically equivalent plans.
Expose the search budget, explored candidates and whether enumeration was
complete; a bounded search must say best found, not globally optimal.

Coupon/reward interactions matter. The modeled order currently places reward
spending before percentage coupons; spending credit may reduce the modeled
percentage discount. Campaign thresholds can depend on pre/post-coupon spend.
These rules must carry evidence. CVS reserves coupon processing order, so
unverified order remains an assumption and may require alternative scenarios.
See [coupon policy](https://www.cvs.com/CouponPolicy) and
[the calculator contract](cvs-basket-calculation.md).

Optional second phase: compare at most two checkouts during the same visit,
with explicit reward-issuance availability, campaign progress and household
limits. New rewards cannot fund the checkout that earns them. Before executing
a second checkout, refresh/verify that the first checkout's reward actually
appeared. See [ExtraCare terms](https://www.cvs.com/extracare/free/terms).
Offer two checkouts only when permitted and the improvement exceeds a configured
effort threshold. Do not make reward-chasing sequences the default shopping flow.

Threshold fillers require explicitly allowed useful products. Compare the
additional cash and consumed credit against the realistically usable additional
reward; report incremental cost. A purchase made only to claim an expiring
reward is not automatically a saving.

## Delivery and validation status

1. **Wallet transitions and ranking:** account-specific certificates, expiry,
   partial redemption, consumed/forfeited values, retained/new wallet and explicit
   future-use assumptions. Include tests where earliest expiry and least cash
   are not the best overall choices.
2. **One-checkout optimizer:** bounded coupon/credit selection, denomination/order
   coverage, no-credit baseline, cash-budget handling and explainable alternatives.
   Unknown applicability/terms remain unresolved; no artificial zero-cost plans.
3. **Reusable interface:** `optimize_cvs_purchase`, JSON CLI and an agent guide;
   fresh prices/account identity and selected-rule lineage checked before ranking.
   `prepare_cvs_purchase` now binds account records to basket-specific rule
   confirmations. Complete automatic terms extraction remains unresolved;
   the agent must supply fresh evidence for outstanding checks.
4. **Evidence and validation:** synthetic end-to-end scenarios plus one anonymized
   real receipt/checkout reconciliation when available. Separate verified receipt
   behavior from synthetic coverage and unresolved processing-order assumptions.
   No synthetic scenario is presented as the shopper's current savings.
5. **Scope:** this implementation uses one checkout, the requested list and no
   extras. Two-checkout planning remains deferred. Real receipt validation is
   pending an actual purchase receipt.

## Agent API and confirmation contract

| Tool | Purpose |
| --- | --- |
| `prepare_cvs_purchase` | Fresh account and exact basket → rule drafts, source fingerprints and pending term checks |
| `optimize_cvs_purchase` | Reprepare the same inputs, search bounded combinations, rank wallet scenarios and show alternatives |
| `reconcile_cvs_receipt` | Compare a saved plan with caller-normalized receipt totals, exact items and offer identities |

The optimizer takes the same raw `lines`, `channel` and **explicit** `store_id`
as the price resolver, plus the complete `account_sync` result, expected
`account_ref`, selected `confirmations` and optional public `reward_offers`.
Store `10001` and ZIP `00000` are fictional examples, not live defaults.
`store_timezone` comes from private preferences or explicit input (otherwise UTC); online expiry uses
America/New_York. The CVS extra installs `tzdata` for Windows DST/date handling.

Real account input must come from the browser, be fresh within 24 hours, match
the expected account label and have `account_binding.status="matched"` plus
`account_identity_verified=true`. This verifies device continuity, not ownership.
First-seen continuity must be established by a subsequent sync. Account access
failures, future/stale snapshots, wrong labels and unverified identities produce
`needs_input` with no recommendation. Fixtures require `allow_fixture=true` and
retain `synthetic_data=true`. Manual snapshots are not treated as matched live
account evidence.

Call preparation initially with `confirmations=[]` to inspect all reward drafts.
Include `{"id": "the-observed-coupon-id"}` to request a coupon draft. The wallet
can have hundreds of coupons; only selected coupon IDs are considered. Each
draft/pending row supplies `source_fingerprint`, binding the account record,
observed purchase requirements, literal term/restriction candidates, eligible
product membership and basket product/SKU/quantity identities.
Changed value, state, terms, membership or basket identities invalidate an old confirmation.
Fingerprints detect changed inputs; they are not authentication signatures.

For a shopping list, [the combined trip planner](cvs-trip-planning.md) handles
collection, relevant-offer selection, these confirmation drafts and optimization.
Unsupported observed stacking prohibitions and discount caps remain pending;
verification flags cannot override those missing model capabilities.

Build each confirmation only from observed complete terms:

```json
{
  "id": "the-observed-offer-id",
  "source_fingerprint": "the-fingerprint-returned-by-preparation",
  "observed_at": "2026-10-10T12:00:00-05:00",
  "source_ref": "reference to the actual observed terms",
  "unit_ids": ["paste:1", "paste:2"],
  "terms_verified": true,
  "product_eligibility_verified": true,
  "stacking_verified": true,
  "limits_verified": true,
  "minimum_quantity": 1,
  "minimum_spend": "0.00",
  "qualification_basis": "pre_coupon",
  "remaining_uses": 1
}
```

Amounts are decimal strings. The four true flags are caller attestations, not
facts inferred by this tool. Unknowns remain false/null and pending. When account
channel/issuer is unknown, confirmation must supply `channels`/`issuer`. Unknown
reward types need `reward_kind_verified=true` plus `kind`; unknown partial
redemption needs `partial_redemption_verified=true` plus `partial_redemption`.
Missing coupon discount facts need `kind`/`value`. Parsed source facts win;
conflicting discount/threshold facts stay unresolved. Amount, activation and
expiration from inventory cannot be overridden. On-card/presented requirements
remain in force; available coupons are excluded until separately activated.
The tool never performs activation.

For quantity-based manufacturer coupons with no spend threshold, confirmation
`unit_ids` is the eligible physical-unit pool. The optimizer assigns exactly
`minimum_quantity` units per redemption. Item-scoped store coupons can opt into
the same assignment with `scope="item"`; otherwise their supplied units remain
the basket/category scope. Up to 25 assignments are generated per coupon;
larger pools explicitly mark assignment search incomplete. A source offer ID can
be redeemed only once, and manufacturer allocations cannot reuse the same unit.
Offer-specific exclusions/limits still must have been confirmed for the pool.

Public earning rules use the [calculator schema](cvs-basket-calculation.md),
plus a unique `campaign_id` across ad groups. Future wallet valuation additionally
needs `earned_terms_verified=true`, aware `earned_valid_from`/`earned_valid_to`,
and `earned_channels`. The promotion's end date is not the earned certificate's
expiry. Unknown earned-certificate terms have **zero modeled preservation value**
and are disclosed, even if the campaign award amount can be calculated. They
never finance this checkout.

## Profile, ranking and result

```json
{
  "return_days": [14, 28],
  "future_eligible_spend": "20.00",
  "cash_budget": "10.00",
  "new_reward_value": "0.80"
}
```

These amounts and return days are fictional examples, not inferred preferences.
Actual cadence belongs in the private local profile. `future_eligible_spend` is one shared hypothetical
eligible future basket budget, used once across the wallet. Old certificates
use face value and new certificates use `new_reward_value`; allocation favors
higher value per dollar. Expired, unsupported-channel or unconfirmed rewards
contribute zero. This assumes that the future purchases are eligible for the
certificates and does not promise a future redemption. Without a future budget,
reported face-value preservation is an explicit **upper-bound scenario**, not
an invented estimate of the shopper's future spending.

`cash_budget` applies **before tax/fees**; it is not a guaranteed total checkout
budget. The recommendation minimizes the worst scenario cost, then combined
scenario costs, forfeiture, cash and number of certificates used. Equal-cost
certificates prefer earlier expiry. Output includes `recommended`, `lowest_cash`,
best modeled `no_credit`, a no-coupon/no-credit `baseline`, per-scenario best
plans, `sensitive_to_return_date`, `at_risk_rewards`, pending/excluded rules and
individual ending-wallet entries. Unused attempted certificates do not appear
in the actual recommended redemption list.

`max_candidates` defaults to 1000 (4–10000 allowed). Search starts with baseline,
all-rule and single-rule alternatives, then enumerates selections, assignments
and modeled processing orders. It reports explored/possible counts, assignment
coverage and `complete`. Completeness refers only to confirmed selected rules,
supported shapes and the modeled phase order—not all CVS offers or POS behavior.
Results say **best found**, never a universally optimal purchase. Unknown selected
rules are excluded and disclosed with `partial` status. If no plan meets the
pre-tax cash budget, `recommended=null` and `status=no_feasible_plan`; the baseline
can still show the shortfall. No unknown price can become a free item.

Coupon subsets and redemption order are conditional: sent-to-card coupons may
apply automatically, and CVS reserves processing order. Confirm whether the
cashier/system can implement a recommended selection; a mathematically better
subset is not proof that checkout can honor it. `execution_ready=false` and
`checkout_verified=false` remain explicit. Verify stock, refresh account/prices
and activation, and check coupon/order assumptions before making a purchase.

## Reusable example and receipt comparison

The complete [synthetic optimizer request](../tests/fixtures/cvs_optimizer_example.json)
uses a $6 basket, an expiring $10 bonus and a fresh $5 certificate. It chooses
the bonus, pays $0 before tax, forfeits $4 that would otherwise expire under
both return scenarios, and preserves the fresh $5. These are synthetic values,
not the shopper's observed account or prices. Its fixed `as_of` permits repeatable
regressions and must never be reused for a live shopping recommendation.

```sh
dealforge-cvs optimize --input tests/fixtures/cvs_optimizer_example.json
dealforge-cvs prepare --input /path/outside/repo/preparation-request.json
dealforge-cvs reconcile --input /path/outside/repo/receipt-comparison.json
```

For reconciliation, pass `{"plan": saved_recommended_plan, "receipt": ...}`.
Receipt fields are `source` (`fixture` or `shopper_receipt`),
`verified_by_caller=true`, `store_id`, `channel`, aware `transaction_at`,
decimal strings `item_subtotal`, `coupon_discounts`, `rewards_spent`,
`reward_value_forfeited`, `new_rewards_earned`, `cash_before_tax`, opaque arrays
`coupon_ids`, `reward_redemption_ids`, `earned_offer_ids`, and `items` containing
`product_id`, `sku_id`, integer `quantity`, pre-coupon `unit_price`.
Missing evidence stays partial; mismatches include differences and identity
issues. The transaction must follow planning within 24 hours. No card number,
raw receipt identifier, barcode, address or credential is required. Tax/fees
are not reconciled. Even equal money totals fail if a different certificate was
used. After purchasing, sync again to inspect actual retained/new rewards.

Fixture comparisons cannot claim real receipt reconciliation. A complete match
against caller-attested real receipt evidence sets `real_receipt_reconciled=true`,
but leaves `checkout_verified=false`: one transaction does not validate every
coupon, product or future POS behavior. Validate an actual purchase with a normalized receipt before relying on POS behavior.
