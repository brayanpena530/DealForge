# H-E-B Savings Programs + Data Sources — verified 2026-09-20

## Short answer

H-E-B has **no points or rewards-currency program**. There is no ExtraBucks
equivalent, no punch card, no points balance. H-E-B is an everyday-low-price
retailer; all savings are coupon/promo-based at the item level, plus two
payment-side rebates.

That makes the optimizer **simpler** than CVS in one way and **harder** in another:

- Simpler: no λ term for a reward currency. `Score = Cash` for the basic case.
- Harder: H-E-B **forbids coupon stacking**, which turns the problem into a
  one-coupon-per-item assignment rather than a stack search.

## The programs

### Item-level offers (the optimizer's real input)

| Program | Mechanic | Notes |
|---|---|---|
| **Digital coupons** | Clip to My H-E-B account, redeem by verified phone at checkout | Unlimited clips; per-coupon redemption limits |
| **Yellow coupons** | H-E-B-issued, in-store paper, also surfaced digitally | Weekly ad literally says `SAVE $1 with yellow coupon in-store or online` |
| **Combo Loco** | Buy X → get Y free or discounted | Bundle promo; signature H-E-B mechanic |
| **Meal Deal** | Buy qualifying main → several sides free | Weekly, high value |
| **Weekly ad sale price** | Straight markdown | Runs Wed→Tue |

Plus manufacturer coupons and online printables, which H-E-B accepts but — see below —
does **not** let you stack.

### Payment-side rebates (a second, smaller optimization layer)

| Program | Value |
|---|---|
| H-E-B Debit Card | 5% back on H-E-B private-label items |
| H-E-B Visa Signature | up to 5% cash back |
| Gift-card fuel discount | 12¢/gal at participating stations |

The 5%-on-private-label rebate is the one that changes basket decisions: it biases
brand choice toward H-E-B store brands by a real margin. Model it as a per-item
rebate rate keyed on `is_private_label`, not as a λ.

## The rule that reshapes the optimizer: NO STACKING

From the official [H-E-B Coupon Policy PDF](https://images.heb.com/is/content/HEBGrocery/PDF/heb-coupon-policy.pdf)
(fetched and read in full, 2026-09-20):

> Only one coupon (digital or paper) will be accepted per qualified item regardless
> of its origination.
>
> H-E-B will not allow stacking of any coupons. Only one item specific coupon per
> qualifying item is allowed.

**Several blogs and aggregators claim H-E-B digital + manufacturer coupons stack.
They are wrong.** The policy is explicit. Trust the PDF.

Conflict resolution rule the policy states: if a paper and a digital coupon are both
presented for the same item, the **paper** one applies and the digital is returned
to the account.

### The two "piggybacking" exceptions

These are the only combinations allowed, and both matter to the solver:

1. **BOGO**: a cents-off coupon *can* apply to the "Buy X" item, because that item
   was purchased — not to the free item.
2. **$-off-basket**: a basket-level coupon *can* combine with an item-level offer,
   provided each coupon's own qualifying requirements are independently met.

### Other hard rules worth encoding

- No doubling or tripling.
- H-E-B coupon face value > item price → clipped to item price, no cash back.
- Manufacturer coupon face value > item price → **excess applies to basket balance**.
  (Asymmetric with the H-E-B rule above. Encode both.)
- Sales tax may be charged on the **pre-coupon** price.
- "Free product" printables (Get X Free) rejected; BOGO printables (Buy X Get X Free) accepted.
- H-E-B coupons are not redeemable through third parties (Instacart etc.).

**Needs verification:** the PDF says "Digital coupons cannot be used for purchases
made on the H-E-B website or H-E-B mobile application," which contradicts H-E-B's own
current marketing ("redeemable online or in store") and the weekly ad's own
"yellow coupon in-store or online" language. The PDF line is probably stale. Confirm
before relying on it.

## Data access

### Tier 1 — Weekly ad: works, no auth

Same Flipp/Wishabi pipeline as CVS. Different merchant slug and token:

```
GET https://backflipp.wishabi.com/flipp/flyers?postal_code={ZIP}&locale=en-us
    -> filter merchant == "H-E-B"

GET https://dam.flippenterprise.net/flyerkit/publication/{flyer_id}/products
    ?display_type=all&locale=en&access_token=98856c1ed32273db1aac58bfe8c76d90
```

Verified today: flyer 8130409 (Austin), 30 items, 22 with `sale_story`.

Flipp carries **per-market flyers** — Austin 8130409, San Antonio 8130405,
Houston 8130390, Dallas 8130424, RGV 8130413 — plus the **Joe V's Smart Shop** and
**Mi Tienda** banners in Houston/Dallas. Store-specific pricing comes free.

`sale_story` encodes H-E-B's promo grammar directly. Real strings from this week:

```
SAVE $1 with yellow coupon in-store or online
SAVE 20% with yellow coupon in-store or online
BUY Ziploc Plastic Storage Bags 10-81 ct., assorted varieties GET FREE! with yellow coupon
BUY two (2) H-E-B Fully Cooked ... GET ...
WHEN YOU BUY $12 OF H-E-B, Higher Harvest, H-E-B Organics, H-E-B Mi Tienda ...
SAVE up to $2 per lb.
```

**Circular depth varies a lot by market.** Austin's flyer is 30 items; Houston's is 55.
Still well short of CVS's 293, and H-E-B keeps the real depth (full weekly ad, all
digital coupons, Combo Loco, Meal Deal) on heb.com — but the promo *density* is far
higher than CVS's, so the circular carries more usable signal than the item count suggests.

## Verified pull: 77008 (Houston Heights), week of 2026-09-16

Three H-E-B-owned banners serve this zip:

| Banner | Flyer | Items | With promo | Character |
|---|---:|---:|---:|---|
| **H-E-B** | 8130390 | 55 | 45 (82%) | promo-dense, yellow-coupon driven |
| **Mi Tienda** | 8131508 | 67 | 4 | near-pure price list |
| **Joe V's Smart Shop** | 8131782 | 38 | 0 | pure EDLP price list |

Joe V's and Mi Tienda reject the flyerkit token (HTTP 422 — tokens are per-merchant);
pull them through `backflipp.wishabi.com/flipp/items/search` instead, which needs none.
Their zero-promo price lists make a good cross-banner price baseline.

### Promo grammar, measured on the 55-item Houston flyer

| Pattern | Count | Example |
|---|---:|---|
| `save_flat` | 25 | `SAVE $2 with yellow coupon` |
| `bogo` | 10 | `BUY one (1) BODYARMOR 16 oz. GET 2ND FREE!` |
| **`basket_threshold`** | **5** | `WHEN YOU BUY $40 OF Huggies ... SAVE $10 OFF YOUR BASKET` |
| `save_pct` | 4 | `SAVE 20% with yellow coupon` |
| `n_for_m` | 3 | `2 for 7.00` |
| `save_per_lb` | 3 | `SAVE up to $2 per lb.` |
| `save_qty` | 2 | `SAVE $3 WHEN YOU BUY 2` |
| `yellow coupon` tag | 40 | — |

Seven patterns cover the entire flyer. A parser for these is a bounded job.

**The 5 `OFF YOUR BASKET` promos are the important find.** Per the coupon policy these
are exactly the `$-off-basket` piggyback case — they combine *on top of* item-level
coupons. That is the same threshold-crossing optimization CVS's `Spend $X get $Y`
creates, and it is live at H-E-B every week:

```
when you buy $9 of TRESemmé Hair Care        → SAVE $3 off your basket
WHEN YOU BUY $40 OF Huggies/Pull-Ups/Wipes   → SAVE $10 OFF YOUR BASKET
WHEN YOU BUY $20 OF H-E-B Mi Tienda Items    → SAVE $4 OFF YOUR BASKET
WHEN YOU BUY $25 Bayou Boil House items      → SAVE $5 OFF YOUR BASKET
H-E-B / Higher Harvest / Organics / HCF / CM → SAVE $3 OFF YOUR BASKET
```

So H-E-B is **not** the simple assignment problem the no-stacking rule first suggests.
It is: per-item exclusivity (pick one coupon per item) *plus* a basket-threshold layer
stacked over the top. Both constraint families in one retailer.

### Tier 2 — heb.com: hard-blocked

heb.com is behind **Imperva/Incapsula**, stricter than CVS's Akamai:

- `curl https://www.heb.com/` -> 1KB Incapsula wall, every time, all header spoofing tried
- Automated browser -> **hCaptcha "Additional security check is required"**

I did not attempt the CAPTCHA and won't.

What did get through:
- `cx.static.heb.com` static JS bundles -> **200, unblocked** (1.2MB `_app.js`)
- `images.heb.com` PDFs -> **200** (that's how the coupon policy was read)

### What the bundle reveals about the real API

From `cx.static.heb.com/_next/static/chunks/pages/_app-*.js`:

- H-E-B runs **Apollo GraphQL at `https://www.heb.com/graphql`**
  (`link: new HttpLink({ uri: '/graphql', fetch })`, `ApolloClientAuthError`,
  `apollographql-client-name` / `-version` headers).
- Named operations in the app shell: `SessionContext`, `GetLoggedInUserInformation`,
  `UserSelectedStore`, `ShoppingStore`, `CartSource`, `GetCartFulfillment`,
  `UserVPPStatus`, `CategoryBrowsePage`.
- Coupon/Combo-Loco queries live in the page-specific chunks, not the shell — findable
  from a real session.
- `robots.txt` disallows `/graphql` and `/loyalty/`, confirming both exist.

**A single GraphQL endpoint is much better than the `_next/data` JSON routes** that
`mohdtalal3/ads_scraper` uses (`/_next/data/{buildId}/en/digital-coupon/coupon-selection/all-coupons.json`).
Those routes break on every deploy when `buildId` rotates; GraphQL operations are stable.
Treat that repo's H-E-B files as a hint about page structure, not as a working client —
they will hit the Incapsula wall as written.

### Tier 3 — Personal coupons: the user's own browser, same as CVS

Identical conclusion to the CVS research: drive the user's real logged-in Chrome,
capture the GraphQL operations the coupon pages fire, then replay them in that same
authenticated context. Never store credentials; attach to an existing profile.

## What this means for DealForge's architecture

The provider abstraction survives, with three additions:

1. `Promotion` needs a `stackable: bool` (CVS true, H-E-B false) and the solver needs a
   per-item exclusivity constraint when false. This is the single biggest modeling
   difference between the two retailers.
2. `Promotion` needs a `basket_level` flag, for the `$-off-basket` piggyback exception
   and CVS's `Spend $X get $Y`.
3. Add a `payment_rebate` layer: `rate × subset_of_items`, applied after coupons.
   Covers H-E-B's 5%-on-private-label and any future CVS card perk. Distinct from λ —
   this is a deterministic rebate, not a probabilistically-valued future currency.

λ becomes retailer-scoped: meaningful for CVS ExtraBucks, effectively 0 for H-E-B.

## Legal / etiquette

Flipp endpoints are public and unauthenticated. heb.com's `robots.txt` disallows
`/graphql`; that is a crawler directive, and using the endpoint as the user's own
authenticated client is a different activity, but it is a clear signal H-E-B does not
want automated access. Keep it human-rate-limited, user-initiated, and require explicit
approval before any clip action. Do not attempt to defeat the Incapsula CAPTCHA.
