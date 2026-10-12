# CVS price evidence and conditional basket calculations

Use `search_cvs_weekly_ad` for fresh, ZIP-scoped public offers. The implementation
calls the public Flipp listing and item-search routes without a token or account.
It intersects flyer and item validity, filters the exact CVS merchant/flyer and
ignores ecommerce results. Historical ad examples are not current exact-SKU
shelf-price guarantees; original location metadata has been replaced with placeholders.
Search is partial, including when no results are returned. Missing footnotes,
limits and store applicability remain checks. `ad_item_id` is never a CVS SKU.
Historical repo fixtures are not a current price source.

## Price resolution

`resolve_cvs_prices` and `evaluate_cvs_basket` both take raw `lines`, an explicit
`channel` (`in_store`, `pickup`, `online`) and `store_id`. Use `get_cvs_product`
observations for `product`; independent `selected_sku_verified=true` is required.
Each line needs a unique opaque `line_id` and integer `quantity` from 1 to 100.
At most 25 lines are accepted. Product IDs/SKUs must agree with the canonical URL.

```json
{
  "line_id": "paste",
  "quantity": 2,
  "product": {"...": "the normalized exact product observation"},
  "price_evidence": []
}
```

The product's fresh `cvs_rendered_page` online price is usable for `online` and
`pickup`, with matching SKU/store/source URL. It is not usable for `in_store`.
For an in-store basket, add an evidence record:

```json
{
  "product_id": "123",
  "sku_id": "456",
  "amount": "5.99",
  "currency": "USD",
  "channel": "in_store",
  "store_id": "10001",
  "source": "shelf_label",
  "source_ref": "shopper confirmed the exact SKU shelf label",
  "observed_at": "2026-10-10T12:00:00-05:00",
  "verified_by_caller": true,
  "sale_status": "regular"
}
```

Supported caller-attested sources are `shelf_label`, `receipt`, `store_quote`
and `weekly_ad`. Quote the **unit price before coupons/reward redemption**.
Receipts are historical observations and require current-price confirmation.
`sale_status` is `regular`, `sale` or `unknown`; never infer regular pricing
merely because no sale label was captured. Conflicting explicit sale statuses
stay unknown. The catalog collector currently leaves this status unknown.
Source references should identify evidence, without personal/session data.

Ad evidence additionally needs aware `valid_from`/`valid_to` timestamps,
`price_kind="unit"`, and true `sku_membership_verified`,
`store_applicability_verified`, `card_conditions_met`. A text match to an ad
group does not justify those flags. Multi-buy/BOGO ad candidates are not converted
into unit prices; confirm the basket's actual base prices separately. A fresh
price with conflicting amounts stays unresolved; the resolver never chooses
the cheapest observation. Prices/rules expire after 24 hours as a planning
default, not CVS policy. Future observations are rejected.

Resolved lines expose unit IDs such as `paste:1`, `paste:2`. Rules must explicitly
target those units. Resolve again at evaluation time; do not pass a previously
computed result in place of the raw lines. Availability is separate from price;
shipping stock does not establish pickup stock. Do not recommend a trip/order
until availability for that channel is checked.

## Explicit calculator rules

Account `offer_rules` and parsed ad `promotions` are incomplete candidates. They
cannot be passed directly into the calculator. Build a rule only from observed
terms plus exact product membership. Verification flags are **caller attestations**,
not proof supplied by DealForge. Setting them true to obtain a numeric answer is
incorrect. Leave unknown checks false or null; the result explains the gap.

All rule types share these fields:

| Field | Meaning |
| --- | --- |
| `id`, `unit_ids` | Stable offer/certificate ID and explicit eligible physical units; IDs unique across all rule types |
| `observed_at`, `valid_from`, `valid_to` | Aware ISO timestamps; use the correct channel's expiration cutoff, including store-local versus online Eastern time |
| `state`, `channels` | `on_card` or `presented`; earning offers may be `automatic`. Channels explicitly name `in_store`, `pickup`, `online` |
| `terms_verified`, `product_eligibility_verified`, `stacking_verified`, `limits_verified` | True only after confirming full terms, exclusions, exact membership, combinations and remaining limits |
| `remaining_uses`, `source_ref` | Known remaining uses (0 means exhausted), reference to observed terms |
| `minimum_quantity`, `minimum_spend`, `qualification_basis` | Explicit thresholds; use 1 and `"0.00"` when terms have no threshold. Positive spend needs `pre_coupon`, `after_coupons` or `after_redemption` |

Set `extra_care_ready=true` only for a scenario where the qualifying linked card
will be used at checkout. Every plan remains conditional. Rules use exact
thresholds with no unofficial 98% tolerance. One supplied ID is redeemed/earned
at most once per evaluation. Use a shared campaign ID across its ad groups;
do not clone it under several IDs to earn a reward more than once.

Coupons additionally require `kind` (`amount_off` or `percent_off`), decimal
string `value`, and `issuer` (`mfr_coupon` or `store_coupon`). Manufacturer
coupons reserve their target units, preventing a second manufacturer coupon on
those units. Store threshold coupons reserve separate qualifying spend: two
$3 off $15 coupons need $30, not the same $15 twice. Multiple store coupons
still need offer-specific stacking confirmation. Percent coupons require
observed regular-price status, apply only to regular units and have a $99 cap
(optional `max_discount` can lower it). Other coupon shapes remain unresolved.

Existing reward `redemptions` additionally require `kind` (`extrabucks` or
`membership_bonus`), `amount`, and boolean `partial_redemption`. Partial rules
are certificate-specific. A non-partial certificate forfeits unused value only
when actually used. A zero eligible balance consumes nothing. At most one
monthly bonus is supported; online/pickup use enforces the $10 minimum. Gift
cards/store credit are not treated as ExtraBucks by this evaluator.

New `reward_offers` need `progress_verified=true`, decimal `prior_spend`, and
integer `prior_quantity` (explicit zero if no prior progress). Spend qualification
basis must be evidenced for that campaign. Use `kind="extrabucks"` plus `amount`
for one fixed award. Use `kind="percent_back"`, `value="2.00"`,
`qualification_basis="after_redemption"`, `minimum_spend` at least `"0.50"`,
and `max_reward` for the base 2% program. Its prior progress must be zero;
include it once across eligible units. Confirm account exclusions, remaining
daily dollars/transactions and put the remaining cap in `max_reward`; the
calculator also caps this award at $5. It never adds 2% automatically. Multiple
awards and transaction sequencing are not modeled.

## Arithmetic and output

The modeled order is fixed coupons, existing reward redemption, then percent
coupons. Dollar amounts are allocated proportionally in cents, with stable
largest-remainder rounding; percentages round half up. CVS reserves processing
order, so this order and allocation are estimates. No actual checkout or receipt
has validated the calculator yet. No coupons are activated and no cart is changed.

Unknown prices return `needs_input` and `totals=null`. Unknown discount/redemption
rules return `partial` and suppress `cash_due_before_tax`; unknown earning rules
suppress `new_rewards_earned` and economic cost. Known expired/excluded/exhausted
rules are `not_applicable`, with a reason. Diagnostic `modeled_*` amounts cover
only rules successfully modeled, and must not be presented as complete savings.
Even complete inputs return `conditional_estimate` and `checkout_verified=false`.
Taxes, fees, deposits and shipping are excluded; `checkout_total` remains null.
Separate `units` and `rules` explain each allocation and qualification decision.

For a **synthetic** two-item $11.98 basket, a $3 coupon and $4 old ExtraBucks
leave $4.98 cash before tax. A newly earned $5 award does not reduce that bill.
At the default `reward_value="0.80"`, economic cost is
`cash + old certificate face value consumed - 0.80 * new rewards` = $4.98.
Forfeited value is included in consumed value. Existing rewards use face value;
the new reward factor is a planning preference, not a cash conversion promise.

## Agent sequence and local example

1. Sync the wallet and relevant details; search fresh ads and exact products.
2. Resolve prices for the intended channel/store; verify stock separately.
3. Translate selected terms into explicit rules, preserving unknown checks.
4. Evaluate one requested basket; inspect unresolved rules and separate totals.
5. Use `prepare_cvs_purchase` and `optimize_cvs_purchase` for bounded coupon and
   individual-certificate selection with fresh evidence. See [the credit guide](cvs-credit-optimization.md).
   Multiple-checkout optimization remains future work.

The complete synthetic request is [cvs_basket_example.json](../tests/fixtures/cvs_basket_example.json).
Its fixed `as_of` makes the example reproducible; never use its prices or offers
for a real purchase. Running it requires no Chrome connection:

```sh
dealforge-cvs ad cereal --zip 00000
dealforge-cvs evaluate --input tests/fixtures/cvs_basket_example.json
```

The same JSON keys are the `evaluate_cvs_basket` MCP arguments. Reload the project
session to discover updated tools; no global MCP configuration changed.
Live MCP ad search returned current Colgate campaign candidates without starting
the Chrome bridge. The protocol test exposes eleven tools and exercises synthetic
price, basket, optimizer and receipt calls. All 283 CVS tests pass; the full suite has 462 passes and
the same three existing H-E-B parser failures. This is arithmetic/interface
validation, not live POS validation. Real receipt/POS validation remains pending.

Policies rechecked October 10, 2026: [pickup pricing](https://www.cvs.com/content/store-pickup),
[coupon policy](https://www.cvs.com/CouponPolicy),
[ExtraCare terms](https://www.cvs.com/extracare/free/terms),
[reward limits](https://www.cvs.com/retail/help/help-subtopic-help-with-extracare),
[monthly bonus](https://www.cvs.com/extracare/overview).
