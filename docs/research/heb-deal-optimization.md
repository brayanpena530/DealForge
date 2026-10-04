# H-E-B Deal Optimization — Phase 3 design

How Phase 3 turns a located shopping list into a priced, route-ordered
shopping plan: the H-E-B rules encoded, the optimizer's design, and the
known limitations. Code: `src/dealforge/providers/heb/deals.py`,
`src/dealforge/providers/heb/optimizer.py`, demo `src/dealforge/heb_plan.py`.

## The rules, from the official coupon policy

(Source: `docs/research/heb-data-sources.md`, which quotes the H-E-B coupon
policy PDF in full.)

1. **One item-level coupon per item.** "Only one coupon (digital or paper)
   will be accepted per qualified item regardless of its origination."
   Paper beats digital when both are presented; the browser adapter only
   sees digital coupons, so the optimizer picks the single max-savings
   candidate per item.
2. **Weekly-ad shelf offers are not coupons.** A sale price, ad BOGO, or
   multi-buy price always applies underneath the one coupon. Where the ad
   states several shelf mechanics for one item, the best one wins -- H-E-B
   does not stack two shelf offers on the same item.
3. **Piggyback exceptions.** (a) A cents-off coupon *may* apply to the "buy"
   item of a BOGO -- modeled naturally: the ad BOGO is valued in the sale
   layer, the coupon in the coupon layer. (b) A basket-level `$X OFF YOUR
   BASKET` coupon *may* combine with item-level offers when its own
   qualifier is met -- applied after item pricing, on top of everything.
4. **Face-value clipping.** An H-E-B coupon's face value is clipped at the
   item price (no cash back). Encoded as `final = max(0, price - saving)`.
   (A manufacturer coupon's excess spills onto the basket -- asymmetric,
   rare, and not modeled; see limitations.)
5. **5% debit-card rebate on private-label items** is payment-side: it does
   not change the register total. Reported separately, only with
   `--debit` / `debit_card=True`.

## Pipeline

```
resolve_list (Phase 2)  ->  LocatedItem(product, location)
collect_deals           ->  DealOffer(name, source, promotions)
match_deals_to_items    ->  DealAssignment(per_item, basket_offers)
optimize                ->  ShoppingPlan (lines in walk order + totals)
```

**Collection.** `client.weekly_ad()` + `client.list_coupons()`, each row
normalized via `to_promotion()`. Two Phase 1 gaps are repaired
deterministically in `deals.py`:

- `WeeklyAdItem.to_promotion()` drops the stated sale price whenever
  `deal_text` is present (the deal-mechanics branch returns before the price
  branch). Collection re-attaches a `SALE_PRICE` rule carrying the sale
  price when the parsed rules don't already have one.
- Basket-level `$X OFF YOUR BASKET` rows name no product and can never
  match an item; they are partitioned into `basket_offers` and evaluated
  against the whole basket.

**Matching.** Deal rows are scored against the item's *resolved product
name* (not the shopper's query -- the coupon names the SKU-level product)
with the catalog's deterministic token-overlap matcher: head-noun
required, `min_score=0.5`, best row first, score and matched term kept on
each `DealMatch` for audit. Unmatched items map to an empty tuple -- most
of a weekly shop is not on deal, and saying so is the honest answer.

**Optimization.** Per item: (1) sale layer -- best shelf offer wins, and a
weekly-ad `SALE_PRICE` only applies when it *lowers* the resolved price
(live heb.com prices usually already reflect the ad; re-applying it would
double count); (2) coupon layer -- exactly one item-level coupon, the
max-savings one, clipped at the item price; (3) basket layer -- every
qualifying `BASKET_THRESHOLD` applies on top; near-misses within $10 of a
threshold get a "$X away from $Y off" note; (4) optional 5% debit rebate on
H-E-B private-label items (name-heuristic until `brand.isOwnBrand` is
wired through), reported separately from the register total.

Greedy per-item choice is optimal under the one-coupon-per-item rule:
items are independent, and the only coupling (the basket threshold) is
evaluated after. No ILP needed.

## Limitations (documented, not silently wrong)

- **Per-unit accounting.** A `$8 OFF ANY TWO (2)` coupon is valued at
  $4.00/unit and the "buy 2" requirement is surfaced as a qualifier note;
  the plan does not force buying two.
- **`SAVE_PER_LB` coupons** can't be valued without a weighed quantity and
  are skipped (noted on the line).
- **BUNDLE / Combo Loco "get that free"** offers name a free item whose
  price is unknown: surfaced as notes, not savings.
- **Manufacturer-coupon spillover** onto the basket is not modeled.
- **Basket qualification** is measured against the post-item-deal subtotal;
  H-E-B sometimes qualifies on pre-coupon spend, so the plan may
  understate qualification slightly.
- **Private-label detection** is a name heuristic (`h-e-b`, `hill country
  fare`, `central market`, ...). Wire `brand.isOwnBrand` from the product
  payload when available.
- **Walk order** is still the Phase 2 heuristic for store #737; the plan
  inherits it via `plan_route`.
- A full ILP solver for multi-unit / cross-item bundle games is future work.

## Demo

```bash
python -m dealforge.heb_plan [items...] [--zip 77008] [--store 737] [--debit] [--live]
```

Fixture mode (default) resolves the bundled catalog and deal fixtures --
no browser, no network. `--live` drives heb.com read-only through
`HEBBrowserClient` (never clips; the plan only *lists* what to clip).

## Phase 4 hook (MCP server)

The Phase 3 seam is `ShoppingPlan`: a frozen dataclass of priced lines in
walk order, a clip list, and totals -- JSON-serializable with no further
work. The MCP server should expose:

- `plan_shopping_trip(items: list[str], store: str, debit_card: bool) -> ShoppingPlan`
  -- the full pipeline: resolve -> collect -> match -> optimize.
- `list_deals(store_zip: str) -> list[DealOffer]` -- raw deal rows for
  agent-side re-ranking.
- `match_deals(items, offers)` -- the explainable matcher, so an LLM agent
  can re-rank candidates while the deterministic scores stay auditable.

Keep the no-LLM rule inside the engine: the agent layer may re-rank
`DealMatch` candidates, but the candidate set, scores, and reasons must
stay reproducible.
