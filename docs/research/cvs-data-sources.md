# CVS Data Source Research — verified 2026-09-17

Three tiers, by how hard they are to get.

## Tier 1 — Weekly ad: solved, public JSON, no auth ✅

CVS does not build its own weekly-ad backend. `cvs.com/weeklyad` is a Flipp/Wishabi
embed. Proof: the flyer record returns
`deep_link = https://www.cvs.com/weeklyad?flyer_run_id=1166491&...&store_code=808`.
So the Flipp API *is* the CVS weekly-ad API.

### Step 1 — zip → flyer ids (no token)

```
GET https://backflipp.wishabi.com/flipp/flyers?postal_code={ZIP}&locale=en-us
```
Filter `flyers[].merchant == "CVS Pharmacy"`. Returns `id`, `flyer_run_id`,
`valid_from`, `valid_to`. Usually 2–3 live flyers (current week, next week, 2-week ad).

### Step 2 — flyer id → full item list (richest source)

```
GET https://dam.flippenterprise.net/flyerkit/publication/{flyer_id}/products
    ?display_type=all&locale=en&access_token=d005d9bbce4a77a9f3908b83e38aae19
```
Verified: 293 items for flyer 8120809. Access token is CVS's public flyerkit key,
lifted from the page; it is not user-specific.

Alternate, fewer items (76 vs 293) but no token:
```
GET https://backflipp.wishabi.com/flipp/items/search?postal_code={ZIP}&flyer_ids={id}&q=
```

### Fields that matter to the optimizer

| Field | Use |
|---|---|
| `name` | product/eligibility text, incl. exclusions ("excludes multi-pks.") |
| `price_text` / `current_price` | sale price |
| `original_price` | baseline for savings |
| `pre_price_text` / `post_price_text` | `"WITH CARD"`, `"or reg retail ea."` |
| `sale_story` | **the promotion string** — see below |
| `disclaimer_text` | limits, per-household caps |
| `sku` | CVS deal id → `https://www.cvs.com/shop/deals/{sku}` |
| `item_categories.l1/l2/l3` | Google taxonomy, good for category coupons |
| `valid_from` / `valid_to` | promotion period |
| `sub_items` | variants under one ad block |

`sale_story` carries the whole promo stack in one string. Real examples pulled today:

```
Spend $25 get $8 ExtraBucks Rewards®✦ WITH CARD
$5.00 on 3 Digital mfr coupon + Buy 2 get $4 ExtraBucks Rewards®⬩ WITH CARD
Also get savings with $1.00 Digital mfr coupon + Spend $25 get $5 ExtraBucks Rewards**
Buy 1 get 1 40% OFF* Also get savings with $4.00 on 2 Digital mfr coupon + Buy 2 get $2 ExtraBucks Rewards**
$2.00 Digital mfr coupon + Spend $20 get $5 Extrabucks Rewards®⧫ WITH CARD
```

60 of 76 items in one flyer had a `sale_story`. This is the Promotion-object input:
a parser for `Spend $X get $Y`, `Buy N get $Y`, `$X on N`, `B1G1 Z% OFF` covers
nearly all of it. Keep the raw string on the object for audit.

Caveat: `sale_story` is marketing copy, not a rule engine. The ⧫/✦/** footnote
markers point at disclaimers not present in the JSON. Treat parsed promos as
`confidence: medium` and show the raw string in the plan.

## Tier 2 — Product catalog / shelf prices: needs a browser 🟡

`www.cvs.com` sits behind Akamai. Plain curl:

- `/search?searchTerm=toothpaste` → **403**
- `/api/locator/v2/stores/search` → **403**
- `/api/...` unknown path → 404 JSON (gateway exists, paths not public)

Not worth an Akamai-bypass arms race. Options, best first:

1. Skip it for MVP — the flyer feed already has sale prices for everything on sale.
2. Read `https://www.cvs.com/shop/deals/{sku}` in a real browser when you need
   regular price / exact SKU list for an ad block.
3. Flipp's own item search for non-flyer pricing.

## Tier 3 — Personal coupons + ExtraBucks balance: authenticated session only 🔴

No public API and no prior art. What the recon shows:

- `cvs.com/extracare/home` is a Next.js app, route prefix `/ec-loyalty-feed/`.
- Bundles reference `x-api-key`, `couponDefinition`, `extracareCardNo`,
  `autoSendToCard` — but **no endpoint URLs**. Data is fetched server-side through
  a BFF, so the real upstream is never exposed to the client bundle.
- Only absolute API host found in the HTML:
  `https://internal-prod-apix.cvshealth.com/oauth2/v2/token`.
- Both known prior-art projects (mortonfox/cvs-send-all-to-card, the jadiagaurang
  gist) are pure DOM click automation — `document.querySelectorAll(".coupon-action.button-blue.sc-send-to-card-action")`
  with a 2s delay. The gist has a comment noting CVS renamed `button-red` → `button-blue`,
  i.e. selectors rot.

**Conclusion: drive the user's own logged-in browser.** Not a bypass — it's their
session, their account. Two routes:

- **Recon (do this once):** open ExtraCare logged in, record network traffic,
  find the BFF routes that return coupons + ExtraBucks. If they return clean JSON,
  call those routes from the same browser context and skip DOM scraping entirely.
- **Fallback:** Playwright/CDP attached to the real Chrome profile, read the
  rendered coupon cards. Brittle, but works today.

Never store CVS credentials in this project. Attach to an existing profile.

## Prior art — what's actually reusable

| Project | Verdict |
|---|---|
| `mohdtalal3/ads_scraper` (`cvs.py`) | **Take the endpoints.** Source of the flyerkit token + products call. Rest is PDF→JPG cropping, skip. |
| `Rocketshon/penny-finder-backend` (`scrapers/flipp.py`) | **Best reference.** Clean async Flipp client, merchant→store mapping. Its `cvs.py` is just heuristics, ignore. |
| `Pizzaface/CoupCoup` | No CVS module (Kroger/Walgreens/DG etc.). Useful only as an LLM-extraction pattern. |
| `mortonfox/cvs-send-all-to-card` + gist | Confirms send-to-card is a DOM click. No API. |
| `Coding-Krakken/BudgetBasket` | Optimizer abstractions only. |

## Legal / ToS note

Flipp endpoints are public and unauthenticated — normal. cvs.com automation of a
logged-in account is against CVS ToS as written; it's the user's own account and
own data, low practical risk, but keep it human-rate-limited, require explicit
user approval before any clip/activate action, and never share the session.

---

# Verified pull: 77008 (Houston Heights), week of 2026-09-20

Re-ran Tier 1 against the user's own zip. Flyer **8139677**, **293 items**,
frozen as `tests/fixtures/cvs_flyer_8139677.json`.

| | CVS 8139677 | H-E-B 8130390 |
|---|---:|---:|
| Items | 293 | 55 |
| Rows that are section headers, not products | 82 | 0 |
| Distinct promo strings | 141 | 44 |
| Promotions parsed | 315 | 55 |
| **Promotions per promotional item** | **1.49** | **1.00** |

That last row is the structural difference between the two retailers, and it is
what the parser had to be built around.

## The grammar, measured

| Shape | Count | Example |
|---|---:|---|
| `extrabucks` | 98 | `Spend $20 get $5 ExtraBucks Rewards®◆` |
| `bogo` | 79 | `Buy 1 get 1 50% OFF*` |
| `save_flat` (coupons) | 62 | `$5.00 on 3 Digital mfr coupon` |
| `sale_price` | 59 | `1.99 WITH CARD` |
| `n_for_m` | 11 | `2/ 9.00 or $5.49 ea.` |
| `save_percent` | 6 | `25% OFF WITH CARD` |

Six shapes cover the flyer. Zero rows fall through to `UNKNOWN`.

## One string, several offers

The CVS parser cannot use H-E-B's "first rule wins, return" dispatch, because a
single string routinely stacks three independent offers:

```
5.99 WITH CARD Also get savings with $5.00 on 2 Digital mfr coupon
+ Buy 2 get $6 ExtraBucks Rewards®⧫
```

→ `SALE_PRICE 5.99` + `SAVE_FLAT $5.00 (min qty 2, mfr coupon)` + `EXTRABUCKS $6 (buy 2)`.

It scans for every shape and each match **consumes its span**, so a lower-priority
rule matching inside an already-claimed span is discarded. That is what keeps
`Buy 1 get 1 50% OFF` from also emitting a standalone `50% OFF` — which would
halve the price of every unit rather than the second one.

## Model changes this forced

1. **`PromoKind.EXTRABUCKS` + `Promotion.reward_amount`.** ExtraBucks are *not*
   `discount_amount`. They do not reduce today's bill. Keeping them in a separate
   field means no pricing engine can accidentally subtract them from cash; they
   enter the objective only as `λ × reward_amount`. This is the λ term from the
   original context doc, finally attached to real data. 98 of 315 promotions.
2. **`PromoSource`** — `AD_SALE` / `MFR_COUPON` / `STORE_COUPON` / `LOYALTY_REWARD`.
   CVS stacks across issuers but allows one offer per issuer per item. Without
   this field that constraint is inexpressible. It also carries H-E-B's
   distinction between a yellow coupon and a shelf price.
3. **`Promotion.requires_card`** — `WITH CARD` appears on most CVS offers and on
   no H-E-B ones. Prices quoted to the user are conditional on it.
4. **`Promotion.discount_percent` on `BOGO`** — the percent is off the *received*
   item, so a plain buy-one-get-one-free carries `100`. Both providers now set it,
   which makes BOGO uniformly computable without a per-retailer branch.

## Real-data hazards found in the live ad

- `Buy 1 get 1 140% OFF*` — a typo in CVS's own copy. Passed through untouched
  would describe an offer that pays the shopper. Parsed, then flagged
  `confidence=0.0` with a caveat rather than silently clamped to 100.
- `Buy 2 get 3rd FREE**` — the ordinal means *one* free item, the third. Reading
  `3` as a count makes the offer look three times as good as it is.
- `Digital mfr coupon` with no stated value — the amount only exists in the app.
  Kept at `confidence=0.4` rather than dropped, since the item is still a
  candidate and Tier 3 can fill the value in later.
- `2/ 9.00 or $5.49 ea.` — buying one is not half the pair price. The model has no
  field for a single-unit fallback, so it is recorded as a caveat. **Known gap:**
  if the optimizer starts recommending partial multi-buys, this needs a real field.
- 82 section-header rows (`Makeup & Nails`, `Extra Big Deals`) carry a name and
  nothing else. They must yield no promotions — they are not purchasable.

## What still needs Tier 3

The circular carries public offers only. It does not contain the user's clipped
coupons, their personalized ExtraCare offers, or their current ExtraBucks balance
and expiry dates. λ cannot be calibrated without the balance and expiry. That
remains the authenticated-browser job described above, still not started.
