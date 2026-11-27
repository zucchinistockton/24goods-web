# SMS verification codes as a product (`upstream_sms`) — spec v1 (2026-11-27)

Owner request 2026-11-27: sell "receive one SMS verification code" (first: OpenAI / ChatGPT) on the shop,
fulfilled automatically from HeroSMS. Decision record: [ADR-057](../architecture.md#adr-057-sms-verification-codes-upstream_sms-owner-2026-11-27).

Legend: **V** = verified in this session (data / live API / code), **I** = inferred or a judgment.

## 1. Facts this design rests on

| # | Fact | Src |
|---|---|---|
| F1 | OpsDev bought 5 OpenAI numbers in Japan (operator Rakuten), all 5 received a code in 17–140 s (median 29 s), USD 0.455 each. No registration result was marked on any number. | V (`/var/lib/mwtops/data/ops/ops.db`) |
| F2 | HeroSMS 24 h code rate for OpenAI (2026-11-27 ~18:30 UTC): Japan 56.8 % (high volume; Rakuten 58.8 %, Docomo 43.9 %), Thailand 66.1 % (very high; dtac 83 %, TrueMove 71 %), US 45.4 %, UK / BR / ID / PH 1–18 %. Prices: JP 0.315–0.49 (retail 0.455), TH 0.13, US 0.715 USD. | V (`/api/v1/stats/deliverability`, `/activations/offers/sms`) |
| F3 | Listed stock is not buyable stock: FR (307k listed) and TH (290k listed) answered `NO_NUMBERS` to real buys with a 2 USD cap. Real per-operator stock in JP is hundreds (Rakuten ~450, Docomo ~300). | V (OpsDev audit + offers) |
| F4 | A number without a code is refunded by HeroSMS on cancel; cancel is refused in the first 2 minutes (`EARLY_CANCEL_DENIED`). Numbers live ~20 minutes. | V (audit, OpsDev code) |
| F5 | HeroSMS REST: `POST /api/v1/activations` {service, country, maxPrice, amount, verificationType, operator?}; `GET /api/v1/activations` (open, with `otpList`); `DELETE /api/v1/activations/{id}` (cancel); `POST /api/v1/activations/{id}/finish`; `GET handler_api.php?action=getBalance`. Errors are `{"title", "details"}` with the code in `title` (`NO_NUMBERS`, `NO_BALANCE`, `EARLY_CANCEL_DENIED`, `BAD_KEY`). | V (OpsDev client, live calls) |
| F6 | The HeroSMS account (USD 10.73 on 2026-11-27) is shared with OpsDev personal use; its webhook already points at OpsDev, so the shop polls. | V |
| F7 | c4 (production procurement container) reaches `hero-sms.com`. | V |
| F8 | The shop has no account balance and no gateway refund API: refunds are recorded by staff after a manual refund (ADR-019). | V |

## 2. Product model (owner decisions 2026-11-27)

- One product, **"OpenAI (ChatGPT) SMS verification code"**, no buyer choices (no country, no operator, no
  variants). The buyer pays a fixed price, then gets a number on the order page when ready.
- **Price: the highest of the proposed totals, ¥8 including the payment fee** (whole yuan). Cost ceiling per
  successful code ≈ USD 0.85 (US route cap) ≈ ¥5.7; JP typical ≈ ¥3.0.
- **Promise shown to buyers: "one code, or a full refund".** No success rates, no country talk.
- **Failed orders are refunded by hand** (order goes to `manual_review`, staff refund + record it).
- Routing: Japan first (default), then a short fallback list the admin controls (start: Thailand, US). The buyer
  sees a number with its country code; nothing else.

## 3. Flow

```
pay ──► order.paid ──► delivery: procure.requested ──► procurement: sms_sessions row (state ready)
                                                                   │
buyer order page: "Get number" ──► API POST /orders/{id}/sms/number ──► procurement /internal/sms/next
   │                                                   (buy: route pick, maxPrice cap, attempt row)
   ▼
number shown + timer  ◄── API GET /orders/{id}/sms (2-4 s poll while active)
   │
procurement SMS loop (every 3 s while an attempt waits): GET /activations → code?
   ├─ code ──► seal "<code>----<+number>" into card_stock (sold to the order) + procure.fulfilled
   │           ──► delivery: order delivered (existing path) ──► buyer sees code (reveal)
   ├─ no code after number_wait_s ──► cancel (refund upstream), attempt no_code; buyer may take another
   └─ attempts used up / window over / not started in start_hours ──► session failed
               ──► procure.failed ──► delivery: manual_review (+ buyer message) ──► staff refund
```

"Another number" (swap): allowed `min_swap_s` (120 s, HeroSMS rule) after the current number was bought;
the current number is cancelled first (refund), then a new one is bought. The buyer never pays twice.

## 4. Settings (per product, admin, audited) — `sms_products.params`

| Key | Default | Meaning |
|---|---|---|
| `service` | `dr` | HeroSMS service code (OpenAI) |
| `routes` | `[{182, 0.60}, {52, 0.30}, {187, 0.85}]` | ordered `{country, max_price}` (USD); first = default |
| `max_attempts` | 5 | numbers per order |
| `tries_per_route` | 2 | no-code numbers on one route before the next route is preferred |
| `window_minutes` | 30 | from the first number; after it no new number |
| `number_wait_s` | 480 | a number without code is cancelled after this |
| `min_swap_s` | 120 | earliest swap (provider refuses earlier cancels) |
| `start_hours` | 72 | buyer must press "Get number" within this after payment |
| `daily_cap` | 30 | sessions started per UTC day (sold out after) |
| `per_buyer_daily` | 3 | SMS orders per buyer per UTC day (checkout refuses more) |
| `cool_no_numbers_min` | 15 | route pause after `NO_NUMBERS` |
| `cool_no_code_streak` | 3 | consecutive no-code numbers on a route (all orders) that pause it |
| `cool_no_code_min` | 30 | that pause |
| `enabled` | false | master switch (off = sold out) |

PoW for instant accounts and the rate limiter are existing settings and stay as they are.

## 5. Stock and "cooling off"

A product with mode `upstream_sms` is **in stock** only when all hold (checked by procurement every health pass
and immediately after every buy / cool event; written to `sms_products.last_check_ok`):

1. `enabled`, the HeroSMS supplier row enabled and its breaker closed;
2. at least one route is not cooling (Redis `rl:proc:sms:cool:<product>:<country>`, TTL = the pause);
3. `daily_cap` not reached;
4. balance ≥ highest live route cap × (open sessions + 1) (open = paid and not finished: money is held for every
   order that can still buy a number).

The storefront shows a small stock number (min of the daily cap left and what the balance covers, max 99) or
"sold out". Checkout re-checks `last_check_ok` (fail closed).

## 6. Data model (migration `0044_sms_sessions`, FORCE RLS, tenant policy)

- `sms_products` (tenant_id, product_id UNIQUE, supplier_id, enabled, params jsonb, last_check_ok, last_check_at,
  last_check_reason, timestamps). `app_api`: SELECT, INSERT, UPDATE (enabled, params, supplier_id, updated_at);
  `app_procurement`: SELECT, UPDATE (last_check_*).
- `sms_sessions` (tenant_id, order_id UNIQUE, product_id, supplier_id, state `ready|active|delivered|failed`,
  attempts_used, params snapshot jsonb, start_deadline, started_at, window_ends_at, finished_at, fail_reason,
  version, timestamps). `app_procurement` full; `app_api` and `app_delivery` SELECT.
- `sms_attempts` (tenant_id, session_id, order_id, seq, supplier_id, upstream_id, country, operator, phone,
  cost, currency, state `waiting|code|no_code|cancelled|failed`, bought_at, expires_at, code_at, closed_at,
  error_code). UNIQUE (supplier_id, upstream_id), UNIQUE (session_id, seq). **No code column**: the code is
  sealed into `card_stock` at once (cardvault public key) and only delivery can open it. `app_procurement` full;
  `app_api` SELECT.
- `suppliers.adapter` accepts `herosms`; `card_stock` rows for SMS carry the order's product (as upstream_cdk).

## 7. Interfaces

Buyer (order owner only, tenant-scoped, rate limited):
- `GET /api/v1/orders/{id}/sms` → `{state, attempts_used, max_attempts, can_swap_in_s, window_ends_at,
  start_deadline, current: {phone, country_dial, bought_at, expires_at, state} | null, delivered: bool}`.
- `POST /api/v1/orders/{id}/sms/number` → same body; starts or swaps. Errors: `sms_not_ready`, `sms_too_early`
  (with seconds), `sms_attempts_exhausted`, `sms_window_over`, `sms_unavailable` (no route had a number — the
  buyer may try again shortly; not counted as an attempt).

Procurement internal (token, backend network only): `POST /internal/sms/next {tenant_id, order_id, user_id}`.

Admin (session admin, audited, platform tenant):
- `GET/PUT /api/v1/admin/products/{id}/sms` (params above; validation: 1–6 routes, caps 0.01–5 USD, sane ranges).
- `GET /api/v1/admin/orders/{id}/sms` (attempts: masked phone, country, cost, state, times; never a code).
- `GET /api/v1/admin/sms/stats?days=7` (per route: numbers, codes, code rate, median seconds to code, cost per
  code, refunds) — the measured success rate the owner asked for.

## 8. Security notes (AGENTS.md §1)

- HeroSMS key lives only in `deploy/env/procurement.env` (`HEROSMS_APP_SECRET`), never in the DB or the API.
- Codes are secrets of the buyer: never logged, never audited, never in a job / attempt row; sealed at once.
- Phone numbers are rented numbers (not buyer data); staff views mask the middle digits.
- Prices come from the server; the buyer sends no amount, no country, no route.
- Every session / attempt state change writes an audit row (no phone, no code in details).
- Tenant isolation: every query pins `tenant_id`; a cross-tenant test is included.
- Production writes to HeroSMS follow `APP_PROCUREMENT_ALLOW_PRODUCTION_WRITES` like every supplier.

## 9. Out of scope (v1)

Buyer-chosen services or countries, rentals, a second code on the same number, automatic gateway refunds,
store credit, reseller voucher codes, HeroSMS webhooks for the shop (polling only).
