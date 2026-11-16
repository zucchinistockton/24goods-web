# review-v12 — multi-supplier procurement and upstream fulfilment

Status: **M1 built** (branch `feat/v12-procurement-m1`), M2/M3 not started. ADR-038. Migration `0023_procurement`.
First supplier: ZovoCard. Vendor docs (git-ignored, the repo is public):
`docs/research/zovocard/zovocard-{openapi,cards,cdk}.md` — section numbers below (`§`) refer to
`zovocard-openapi.md` unless marked `cdk§`.

Legend: **[V]** verified in the vendor docs or by a live call on 2026-11-14 · **[I]** inferred, confirm in sandbox.

## 0. Owner decisions (2026-11-14)

- Manual handling stays the primary path; automation must never remove a manual exit.
- Design for several suppliers; one variant may have several sources with failover/rotation.
- VCC card data is sealed in the card vault and shown only through the existing Reveal flow.
- **No real funds** until the independent security review (AGENTS.md §1.11). Production suppliers stay
  `enabled=false`; development uses the ZovoCard **sandbox** and the mock supplier only.
- Webhook callback on the demo host; CDK redemption site on its own subdomain (existing Cloudflare token).

## 1. Facts that shape the design

1. **[V]** The supplied key is a *production* key: prod answers `403` "IP allowlist not configured", sandbox
   answers `401`. Sandbox needs its own key from `https://sandbox.zovocard.com` and is fully simulated.
2. **[V]** New keys must have an IP allowlist before any call. Procurement therefore needs a stable egress IP
   per environment (dev box egress today: `107.173.10.79`).
3. **[V]** Idempotency replay caches **only 2xx**. A failed call is not cached; the same key may be sent again.
   `409` = first request still running (server waits up to 30 s): back off, reuse the key, never mint a new one.
4. **[V]** Many errors carry **no `error_code`** (all of A.3 card operations, CDK A.5 parameter errors, 401/403
   allowlist, 429). Routing rule: `error_code` when present, else HTTP status; an unrecognised 4xx on a write is
   **ambiguous → reconcile by query**, never "retry with new key" and never parse `msg`.
5. **[V]** Card recharge "result pending" is HTTP **400 without a code** (§3). So on `/cards/recharge` every
   400 that is not a known terminal `error_code` (`amount_below_min`, `insufficient_balance`, `not_found`,
   `invalid_argument`) is `accepted_pending`, resolved via `GET /cards/{id}/recharges`.
6. **[V]** **CDK cost is not paid at issue time.** `POST /gpt-direct/cdks` charges only a service fee
   (`fee_amount_minor`); the card funding and subscription price are charged to *our* balance when the buyer
   **redeems** (`funding_confirmed: true`). Disabling an unused code does not refund the fee. Consequence: the
   balance breaker must reserve for every issued-but-unredeemed code (§6 below).
7. **[V]** CDK issue is all-or-nothing per request; the owner list (`GET /gpt-direct/cdks`) returns plaintext
   codes, so an ambiguous issue is recoverable by same-key replay.
8. **[V]** Doc inconsistencies — use the stricter value: rate limit 100/min (§3) vs 300/min (portal); CDK batch
   max 200 (cdk§6.18.6) vs 50 (A.5); "CDK supports GPT plans only" (A.5) vs Grok/Claude codes (cdk§6.18.8).
   Client limit: 60 req/min, CDK `count ≤ 50`.
9. **[V]** Card/fee/balance webhooks are retried only 3 times then dropped; GPT/CDK events are durable (6 tries,
   24 h). Webhooks are hints: **polling is mandatory**, not a fallback.
10. **[V]** Card webhooks carry the **full card number**; the API also has a per-key cumulative spend cap
    (second gate besides balance).
11. **[V]** CDK public redemption (`/api/v1/cdk/*`) binds the session to egress IP + `X-Redemption-Device` and
    takes the buyer's account session/credential.

## 2. Process and trust boundaries

```
api ──(outbox: order.paid)──> delivery ──(outbox: procure.requested)──> procurement ──> supplier (internet)
                                 ^                                           │ seal (public key only)
                                 └────────(outbox: procure.fulfilled)────────┘
api <── webhook (verify, dedup, enqueue supplier.event) ── supplier
```

- `backend/app/delivery/`: **unchanged boundary** — internal network, only holder of vault private keys.
  New: for `upstream_*` orders it emits `procure.requested` instead of claiming stock, and handles
  `procure.fulfilled` (creates `Delivery` rows, marks delivered) and `procure.failed` (→ `manual_review`).
  Delivery stays the single writer of the `paid → delivered` transition.
- `backend/app/procurement/` (new process, `python -m app.procurement`): outbox consumer for
  `procure.requested` / `supplier.event`, poller, reconciler. Holds supplier API secrets and the vault
  **public** key (`CardSealer`) only. Own env file `deploy/env/procurement.env`, own compose network with
  egress (pattern: `bepusdt`), DB role `app_procurement`, Redis ACL user.
- `app_procurement` grants: full on the five new tables; `INSERT` on `card_stock`, `outbox`, `audit_log`;
  `SELECT` on `orders`, `products`, `product_variants` (needed columns only). **No `UPDATE` on `orders`,
  no access to `deliveries` or vault private material.**
- Upstream secrets (CDK code, PAN/expiry/CVV) exist in procurement memory only between the HTTP response and
  `seal()`. They are inserted as a `card_stock` row already `status='sold', order_id=<order>` (never
  `available`, so the local pool can never claim them), AAD = (tenant, product, card_id) as today. No logging
  of response bodies on write endpoints; the HTTP client logs method, path, status, `error_code`, key hash.
- `backend/app/suppliers/`: `base.py` (protocol + result types), `registry.py`, `zovocard/`, `mock/`, `manual/`.
  Nothing in `suppliers/` imports from `app.modules`.

## 3. Supplier protocol (`suppliers/base.py`)

```python
class Outcome(Enum): OK; ACCEPTED_PENDING; RETRYABLE; UNAVAILABLE; FATAL; AMBIGUOUS
@dataclass(frozen=True)
class Result:  outcome; error_code: str | None; upstream_ref: str | None
               secrets: tuple[str, ...] = ()   # plaintext, sealed by the caller immediately
               cost: Money | None; retry_after: timedelta | None

class Supplier(Protocol):
    code: str;  capabilities: frozenset[Capability]      # cdk | vcc | direct | void | webhook
    async def probe(self) -> ProbeReport                 # auth, permissions, balance, plans  (read-only)
    async def balance(self) -> Balance                   # spendable, currency
    async def availability(self, offer) -> Availability
    async def submit(self, job) -> Result                # the one write per job step; uses job.idempotency_key
    async def query(self, job) -> Result                 # read-only reconciliation of the same job
    async def void(self, job) -> Result
    def verify_webhook(self, raw: bytes, headers) -> list[SupplierEvent]   # raises on bad signature
```

Outcome semantics (the only thing the engine understands; supplier quirks stay in the adapter):

| Outcome | Engine action |
|---|---|
| `OK` | seal secrets, emit `procure.fulfilled` |
| `ACCEPTED_PENDING` | state `accepted_pending`; wait for webhook, poll `query()` with backoff; never resubmit |
| `RETRYABLE` (429, 500, timeout before send) | same key, backoff |
| `AMBIGUOUS` (timeout after send, 409, unknown 4xx on a write) | `query()` first; resubmit only with the **same** key |
| `UNAVAILABLE` (503 `channel_unavailable`, not purchasable, breaker open) | definite "no charge": try next offer |
| `FATAL` (deterministic business error) | stop; `needs_manual` + staff alert + buyer message |

Failover happens **only** on `UNAVAILABLE` (same rule as ADR-034 for payment gateways). Once M2 implements
`query()`, failover additionally requires a `query()` by the job's idempotency key that finds nothing upstream;
until then no job is ever submitted, so the question does not arise. A job that ever
reached `submitting` against supplier A moves to supplier B only by an explicit staff action after A's job
is confirmed not charged or voided.

## 4. Data model (migration `0023_procurement`, all tenant-scoped, FORCE RLS)

- `suppliers`: `code`, `adapter`, `label`, `enabled` (default false), `env` (`sandbox|production`),
  `priority`, `settings` JSONB (no secrets — secrets come from the procurement env file by `code`),
  `min_spendable` (breaker), `health`, `last_probe_at`, `last_balance`, `last_balance_at`.
- `supplier_offers`: `supplier_id`, `product_id`, `variant_id`, `kind` (`cdk|vcc|direct`), `params` JSONB
  (plan key, product enum, payment_country/currency, issuer, amounts), `cost_estimate`, `currency`,
  `priority`, `enabled`. Several offers per variant = failover order; `routing` per variant: `priority|round_robin`.
- `procurement_jobs`: `order_id`, `unit_no`, `offer_id`, `supplier_id`, `kind`, `state`, `step`,
  `idempotency_key` (**UNIQUE**, `^[A-Za-z0-9._:-]{1,80}$`, format `pj:<job uuid hex>:<step>`),
  `client_request_id`, `request_hash`, `upstream_ref`, `error_code`, `attempts`, `next_poll_at`,
  `preflight_expires_at`, `cost`, `currency`, `card_stock_ids`, `manual_by`, timestamps.
  `UNIQUE(order_id, unit_no)` while not `voided`. The key is written and committed **before** the first HTTP call.
- `supplier_events`: `supplier_id`, `dedup_key` (**UNIQUE with supplier_id**), `kind`, `payload` (card numbers
  masked before insert; OTP never stored), `received_at`, `processed_at`.
- `supplier_ledger`: `supplier_id`, `external_id` (**UNIQUE with supplier_id** = `balance_log_id`), `type`,
  `amount` (Decimal, signed), `currency`, `balance_after`, `job_id` nullable, `occurred_at`.
- `products.delivery_mode` / `product_variants` gain `upstream_cdk | upstream_vcc | upstream_direct`
  (snapshotted on the order as today). `products.exclude_from_recommendations` boolean (see §9).

Job state machine (every transition writes an audit row):

```
queued → submitting → fulfilled
              ├→ accepted_pending → fulfilled | failed
              ├→ queued (UNAVAILABLE, next offer)
              └→ needs_manual → fulfilled (staff) | voided
fulfilled → void_pending → voided
```

Stock display for upstream variants: "in stock" iff an enabled offer exists, its last availability check
passed and the supplier breaker is closed; otherwise the variant shows unavailable (no oversell into manual
review). Checkout re-checks the cached availability, it does not call the supplier.

## 5. ZovoCard adapter (`suppliers/zovocard/`)

`client.py` — one place for: `X-API-Key` + `X-App-Id`; `Idempotency-Key` on every write; envelope decoding
(`msg`, not `message`); **HTTP 200 with `body.code != 0` is an error** (§3 "two misbehaving responses");
status + `error_code` → `Outcome` table in `errors.py` (appendix A, one dict, unit-tested row by row);
token-bucket 60/min; timeouts 10 s connect / 40 s read (server holds idempotent replays 30 s).

| Mode | Calls | Notes |
|---|---|---|
| `upstream_cdk` | `GET /gpt-direct/plans` (cached 60 s) → `POST /gpt-direct/cdks` `{plan, count:1, funding_confirmed:true, payment_country}` | one job per order unit; `OK` returns `issued[].code`; void = `POST /gpt-direct/cdks/{id}/disable` (only `unused`; fee not refunded; `409 CDK_DISABLE_REJECTED` = already used → cannot void) |
| `upstream_vcc` | `GET /products` → `POST /cards/open` → (optional step 2) `POST /cards/recharge` → `GET /cards/{id}` for PAN/expiry/CVV | two steps = two keys (`:open`, `:recharge`); void = `POST /cards/refund` then `DELETE /cards/{id}` (both may be 202 → `void_pending`, poll card list) |
| `upstream_direct` | preflight → `POST /gpt-direct/orders` (202 → `accepted_pending`) → `GET /gpt-direct/orders/{id}` | `product` is a closed enum `gpt|claude|grok|x`, sent explicitly, validated before the call; Claude/Grok plan = `registry[].key` without prefix + `payment_country='US'`, `payment_currency='USD'`; preflight token valid 10 min → re-preflight when `preflight_expires_at` passed or on `GPT_PRICE_UNCONFIRMED`; void = `POST /gpt-direct/orders/{id}/cancel` (409 = too late) |

Availability = registry entry exists **and** `purchasable === true` **and** `plans[key].enabled === true`
(strict identity checks; a missing field is "unavailable").

`FATAL` codes (no retry): `GPT_PLAN_ALREADY_ACTIVE`, `GPT_SESSION_INVALID`, `CLAUDE_SESSION_INVALID`,
`PRECHECK_REJECTED`, `GPT_RENEW_TARGET_NOT_PRO`, `GPT_RENEW_SUBSCRIPTION_EXPIRED`,
`GPT_CREDIT_REQUIRES_SUBSCRIPTION`, `INVALID_REQUEST`, `invalid_argument`, `amount_below_min`, `forbidden`.
`UNAVAILABLE`: `channel_unavailable`, `product_unavailable`, `GPT_DIRECT_UNAVAILABLE`, `GPT_DIRECT_PAUSED`,
`DIRECT_PRODUCT_DISABLED`, `GPT_CREDIT_DISABLED`. Supplier-level stop (open breaker, alert staff):
401, 403 (allowlist, `RECHARGE_REQUIRED`, `GPT_DIRECT_ACCESS_DENIED`, `FORBIDDEN`), `insufficient_balance`,
"API key spend cap exhausted" (arrives as `request_failed` → treated as supplier stop **[I]**).

Writes vs reads (security review of M1): on a **write**, HTTP 500 without a code, `internal_error` (also when it
arrives as HTTP 200 with `code != 0`) and the catch-all `request_failed` are `AMBIGUOUS` (the request may have
run; `query()` first, resubmit only under the same key); on a read they stay `RETRYABLE` (`request_failed`:
`UNAVAILABLE` + supplier stop). `request_failed` keeps its supplier-stop flag in both cases. `channel_unavailable`
(vendor §3: the channel breaker fires before any charge) and `product_unavailable` (A.2: product paused) stay
`UNAVAILABLE`. `GPT_PRICE_UNCONFIRMED` and `GPT_PREFLIGHT_RATE_LIMITED` stay `RETRYABLE`: the engine (M2/M3)
re-preflights and resubmits under the **same** idempotency key for that step. Vendor `error_code` values are
reduced to `[A-Za-z0-9_.:-]{1,64}` before they reach logs, `health_detail` or audit rows.

Buyer credential for `upstream_direct` (account session): sealed at checkout with a **separate procurement
keypair** (API holds the public key, procurement the private key; the delivery keyring is untouched), never
logged, deleted when the job reaches a terminal state. This also covers part of open item S7.

## 6. Webhook, reconciliation, breaker

- `POST /api/v1/webhooks/zovocard` (API process, no auth, rate-limited, body ≤ 64 KiB): read **raw bytes**,
  `hmac.compare_digest(hex(HMAC-SHA256(whsec, raw)), X-Signature)`, reject otherwise (401, no detail). Then
  `INSERT … ON CONFLICT DO NOTHING` into `supplier_events`, enqueue `supplier.event`, return 200 fast.
  The API holds only the webhook secret, not the API secret.
- Dedup keys: GPT/CDK `event_id`; card transaction `event+auth_id+type+status`; card operation
  `event+operation+operation_id+status`; `balance_change` `balance_log_id`; `card_3ds_otp`
  `matrix_id` or `HMAC(masked_card_no+otp)` (the OTP itself is never a stored key).
- A webhook never fulfils by itself: procurement re-queries the order/card (`query()`) before any transition
  (same principle as payment notifies, §1.2).
- `card_3ds_otp`: resolve card → order → buyer, publish on the buyer's SSE channel, keep in Redis ≤ 5 min for
  a late-opened page, never in PostgreSQL or logs.
- Cost ledger: **only `balance_change`** rows (webhook + `GET /balance-logs` backfill every 10 min, both
  through the same unique insert). `card_fee` is recorded as an event for display and **never** added to the
  ledger (the same money already arrives as a `balance_change`).
- Breaker: `spendable_balance − Σ(reserved redemption cost of issued, unredeemed CDKs) − Σ(in-flight jobs)
  < suppliers.min_spendable` → supplier unavailable for new jobs, staff alert. Balance refreshed every 5 min
  and after every job.
- Polling: `accepted_pending` jobs at 15 s, 30 s, 1, 2, 5 min … capped at 15 min; alert staff after 30 min.

## 7. Admin console (PC) and manual operation

- **Suppliers**: list, enable/disable, env badge, "Test connection" (runs `probe()`: auth, IP allowlist,
  permissions, `spendable_balance`, plans/products), breaker threshold, health. Platform tenant only (the
  env-file secrets belong to the platform deployment). "Test connection" is allowed for a production-env
  supplier even while the production lock keeps it disabled: it is GET-only, and the owner wants to check
  the real key. Supplier codes: the adapter name or `<adapter>_<suffix>`, never ending in `_sandbox`.
- **Sources per variant**: add/sort/enable offers, routing mode, "check availability".
- **Procurement jobs**: filter by state; detail with timeline; actions — re-query, retry (same key, shown
  read-only), switch to next source (only when not charged), **fulfil manually** (paste code → sealed like a
  card import → same `procure.fulfilled` path), void, mark failed + refund via the existing after-sales flow.
- **Cost report**: ledger by day/supplier/type, margin per order, unreconciled rows, CSV export.
- Staff can always set a variant back to `manual` / `auto_card`; open jobs keep their snapshot.
- Roles: supplier settings and void = admin; job actions = existing staff permission for manual delivery.

## 8. CDK redemption site

Static SPA on `redeem.<domain>` (own Cloudflare record, own Caddy site, strict CSP, no shared cookies with
the shop). It calls a small proxy in **procurement** (`/redeem/*` → ZovoCard `/api/v1/cdk/{preview,preflight,
redeem,result}`) so that the egress IP and `X-Redemption-Device` stay constant across the four steps. The
proxy is stateless apart from Redis rate limits, forwards the buyer's credential without storing or logging
it, and is not routed through the API container. Built in M2 after issuance works; until then buyers can
redeem on the supplier's own page.

## 9. Small fixes (separate PR `fix/pay-ux-reco`, can start now)

- `frontend/apps/web/src/pages/UsdtPayPage.vue`: card padding ≥ 16 px and row gaps at 375 px; address rendered
  as `first 6` + middle + `last 6` with both ends emphasised (monospace, grouped by 4); copy still copies the
  full address; amount row keeps the tap-to-copy.
- `PayChannelPicker.vue`: consistent icon/label/fee layout for USDT and Alipay rows, ≥ 44 px targets.
- Recommendations: `products.exclude_from_recommendations` filtered in `_RECOMMEND_SQL`
  (`backend/app/modules/catalog/service.py`), toggle in the admin product form, en + zh-CN keys.

## 10. Milestones

- **M1** — `suppliers/` (base, mock, zovocard client + error table), migration, procurement process + compose
  service + role/grants, admin Suppliers page with probe. Exit: probe green against sandbox; cross-tenant test.
  **Status (2026-11-14): built; green against `app/devtools/mock_zovocard.py`; the sandbox probe waits for the
  sandbox key (§12).** Deferred to M2: per-variant `routing` (priority / round robin) storage, sealing in
  procurement (vault public key is mounted, not loaded yet), webhook secret (goes to the API env).
- **M2** — also: require the current admin password to create a production supplier or switch one to
  production; add procurement + `mock_zovocard` to `scripts/dev.sh` and to the e2e smoke; the migration that
  first writes them grants `app_procurement` INSERT on `card_stock` and `outbox` (M1 grants neither).
- **M2** — `upstream_cdk` end to end (delivery branch, job engine, seal, fulfil), webhook + dedup, ledger +
  breaker, manual fulfil/void/switch, cost report, redemption site. Exit: e2e smoke with the mock supplier
  covering timeout-after-send, 409, 202, 503 failover, duplicate webhook, forged signature.
- **M3** — `upstream_vcc` (open, recharge-pending rule, reveal, 3DS over SSE, void) and `upstream_direct`
  (procurement keypair, preflight refresh, enum lock, async orders).

Each milestone: security-reviewer + payment-auditor pass, `docs/progress.md` and ADR-038 updated in the PR.

## 11. Live verification (2026-11-14, production key, read-only calls only)

Allowlist set (dev `107.173.10.79`, prod `141.98.198.150`). `GET /balance`, `/products`, `/vip`,
`/gpt-direct/plans?product={gpt,claude,grok}`, `/gpt-direct/cdks`, `/gpt-direct/card-products`,
`/balance-logs` all return 200. Direct top-up/CDK permission is present (plans are returned). Response
samples: `docs/research/zovocard/samples/*.json` (git-ignored) — build the adapter's parsers and the mock
supplier's fixtures from these shapes, not from the prose docs.

- Plans payload: `data.plans{}` is keyed by plan key and uses **camelCase** (`enabled`, `serviceFeeUsdMinor`,
  `expectedAmountMinor`, `balanceBufferBps`); `data.registry[]` uses **snake_case** (`key`, `product`,
  `acc_plan_key`, `purchasable`, `checkout_currency`, `checkout_amount_minor`, `service_fee_usd_minor`).
  Availability joins them: `registry[key].purchasable is True and plans[registry[key].acc_plan_key].enabled is True`.
- `expectedAmountMinor` is in the plan's checkout currency (e.g. `go` = 30000 PHP minor), not USD: the
  breaker reserve needs the USD cost from the CDK order / `balance_change`, not this field.
- Amounts in card APIs are JSON floats (`open_fee: 1.5`): parse with `Decimal(str(x))`, never `float` math.
- Webhook is configured on the production key: `https://demo.kesiv.com/api/v1/webhooks/zovocard`, signing
  key in `~/.secrets/zovocard.env` (`ZOVOCARD_WEBHOOK_SECRET`). Until M2 ships the endpoint answers 404 and
  the supplier pauses delivery to it; nothing is lost that polling does not recover.
- **No production write call is allowed** from development (owner rule). The client refuses any non-GET
  request when `env == "production"` unless `APP_PROCUREMENT_ALLOW_PRODUCTION_WRITES=1`, which no dev/demo
  env file sets.

## 12. Sandbox

Access is by support ticket (1–2 business days; account lives 3 days; **scanning the API is forbidden** —
run only the scripted probe and the named scenario tests, no fuzzing, no enumeration). Until it arrives
everything is built and tested against `suppliers/mock` + `app/devtools/mock_zovocard.py` (same envelope,
status codes and quirks as §1/§5). When the ticket is approved: put the key in `ZOVOCARD_SANDBOX_APP_SECRET`,
run `python -m app.procurement probe --supplier zovocard --env sandbox`, then the [I] items in §14.
The owner's mainland VPS is a last resort for reaching the sandbox and must not hold a long-lived
connection; nothing is deployed there (AGENTS.md §1.7).

**Sandbox verification (2026-11-15) [V].** Key in `~/.secrets/zovocard.env` (`ZOVOCARD_SANDBOX_APP_ID` /
`_APP_SECRET`); the account expires 2026-11-18 10:57. From the dev box (IP allowlist on):
- `python -m app.procurement probe --supplier zovocard --env sandbox` is green: auth OK, IP allowed, direct and
  card permissions, 5 purchasable plans, 29 card products, balance 50000 USD. **M1 exit criterion met.**
- One scripted `upstream_cdk` scenario through `ZovoCardSupplier` (plan `plus`, count 1): `submit` -> OK with
  one code and an upstream id; **same-key replay returns the same id and the same code** (the idempotency
  cache works as documented, which is what `query()` relies on); `void` (`/disable`) -> OK; a replay after the
  void still answers from the cache, so it reports the issue, not the current CDK status.
- Issue cost is reported as 0.00 USD: the charge happens at redemption, so the breaker reserve needs the
  per-plan redemption cost (§14.3, still open).
- Webhook endpoint on the demo (M2 branch deployed there, sandbox signing secret configured): signed event
  -> 200 and stored once; duplicate -> 200, not stored again; forged signature -> 401. Self-signed test event
  only; a vendor-sent event is still to be observed (redeem a sandbox CDK), and the procurement process is
  not running on the demo, so events are stored but not consumed there.

**Sandbox scenarios, second pass (2026-11-15 12:07–12:20 UTC) [V].** Scripted, 30 calls, no enumeration.
- **The sandbox exposes five CDK plans only** (`go`, `plus`, `pro_5x`, `pro_20x`, `pro_50x`; all USD, service
  fee 0, `expectedAmountMinor` 500 / 2000 / 10000 / 20000 / 10000). Issuing a plan that is not listed
  (`credit250`) answers **HTTP 400 `request_failed`** ("plan paused"), not `product_unavailable`. Our table maps
  a write `request_failed` to `AMBIGUOUS` + supplier stop, so the availability check before `submit` is what
  keeps a paused plan from opening the breaker; the engine must never submit a plan the catalog does not list.
- **`GET /gpt-direct/cdks` does not expose the idempotency key.** Item fields: `id`, `created_at`, `code`,
  `code_prefix`, `plan`, `status`, `fee_amount_minor`, `app_id`. An ambiguous issue is recovered only by the
  same-key replay (verified earlier); the list can confirm a code afterwards, not find it.
- **Redemption is a mock.** `preview` and `preflight` work (preflight ignores the real account and reports a
  synthetic `sandbox+…@example.test`, plan `free`; it returns a `preflight_token`, `payment_country`,
  `payment_currency`, `quotes: null`). `redeem` answers at once with `order.status = "succeeded"`,
  `sandbox_mock: true`, order id `SBXORD-…`. Nothing else follows: `GET /api/v1/cdk/result` is 404
  `not_found`, `cdk-orders` stays empty, the balance and `balance-logs` do not move, and the CDK stays
  `unused` (it could still be disabled afterwards). **The USD redemption cost therefore cannot be measured in
  the sandbox**; the breaker reserve uses the plan's `expectedAmountMinor` (+ `balanceBufferBps` /
  `minBalanceBufferUsdMinor`, + `serviceFeeUsdMinor`) from `GET /gpt-direct/plans`, converted to USD only when
  the plan currency is USD, otherwise the configured per-offer cost. Confirm against the first production
  redemption.
- **Cards.** `POST /cards/open` (USMAB01, 10 USD), two `POST /cards/recharge` one second apart, `refund`,
  `DELETE`: all HTTP 200 `code 0` at once (`recharges[].status = "success"`, `data.pending = false`); a same-key
  replay of the delete returns the same body. **The recharge-pending 400 does not occur in the sandbox**, so
  its body stays as documented by the vendor ([I]). `balance-logs` rows carry `id`, `created_at`, `amount`
  (signed), `before`, `after`, `type` (`open_card`, `card_recharge`, `recharge`, …), `remark`, `ref_id`,
  `wallet`, and for card rows `card_id` / `card_number` (full PAN: mask before storing). The opening charge is
  booked as `card_recharge`, the open fee as a separate `open_card` row (amount 0 here).
- **No vendor-sent webhook arrived** for any of these (CDK issue, disable, mock redemption, card open,
  recharge, refund, delete): the demo endpoint logged no request from the vendor and `supplier_events` still
  holds only the self-signed test row. `card_3ds_otp` cannot be triggered from the API either. Either the
  sandbox does not deliver webhooks, the key has no saved callback / `webhook_events` subscription, or the
  delivery is stopped before the origin (Cloudflare). Owner to check the sandbox console (Developers → key →
  Webhook: saved URL, subscribed events, delivery log) before the account expires on 2026-11-18.

## 13. Added scope (owner, 2026-11-14)

**Optional two-factor authentication (TOTP), own PR `feat/totp-2fa`, after M1.** Buyers and staff may enable
an authenticator app; not enabling it never blocks login. RFC 6238 (SHA-1, 6 digits, 30 s, ±1 step), secret
shown once as `otpauth://` QR (rendered locally, no third-party asset) + 10 single-use recovery codes
(stored hashed); TOTP secret encrypted at rest with an app key (not the card vault); login becomes
password → short-lived pending token → code; replay of a used step rejected; rate limit and lockout shared
with the login limiter; disabling requires a current code or a recovery code; every change audited.
Closes open item S8 for staff. Admin setting "require 2FA for staff" is off by default.

**Visual direction.** Both apps keep the existing design tokens and the system light/dark themes; no new
palette, no gradients, no third-party fonts or assets. New and reworked buyer screens (pay pages, redemption
site, 2FA setup) follow an editorial-minimal direction: one clear focal element per screen (the amount on a
pay page, the code on redemption), strong type scale with tabular/monospace numerals, generous spacing on
the 4/8 grid, hairline dividers instead of nested cards, restrained motion (150–200 ms opacity/transform,
disabled under `prefers-reduced-motion`). Frontend design is done by the orchestrator (Fable), not delegated.

## 14. Open items

1. ~~Sandbox ticket approval → sandbox key~~ done 2026-11-15 (§12); webhook test still needs the sandbox
   signing secret and M2 on the demo.
2. Rotate the production key and the webhook signing key before go-live (both were pasted into chat).
3. Sandbox [I] items, state after the second pass (§12): `GET /gpt-direct/cdks` exposes no idempotency key
   (**settled**: replay only); USD redemption cost per plan is **not measurable** in the sandbox (mock
   redemption; use `expectedAmountMinor` + buffers, confirm in production); the recharge-pending 400 body and
   the `card_3ds_otp` field names **stay [I]** (not reproducible; both are M3). A vendor-sent webhook was not
   observed (owner to check the console).
4. **Egress allowlist before go-live:** the `procurement_egress` network has general internet egress. Restrict
   procurement's outbound traffic to the supplier hosts (firewall / egress proxy) before real funds.
5. Supplier balance is 6.27 USD: below any useful breaker threshold, which is the intended state until the
   security review.

## 15. GPT session checks and the backup supplier (owner, 2026-11-15)

**Session pre-check (built).** `backend/app/suppliers/gpt_session.py::parse_gpt_session` validates a pasted
`chatgpt.com/api/auth/session` document offline before it is sealed or forwarded: size cap 64 KB, tolerant of
text around the JSON, requires `sessionToken` + `accessToken`, rejects a bare `eyJ…` access token, checks the
access-token `exp` and the session `expires`, and that `account.id` matches the token's
`chatgpt_account_id`. Error codes (to map to i18n keys): `too_large`, `not_json`, `access_token_only`,
`missing_session_token`, `missing_access_token`, `malformed_access_token`, `expired`, `missing_account`,
`account_mismatch`. The JWT signature is **not** verified, so the result is a hint; the supplier preflight is
the truth. The raw session is a credential under the same rule as card secrets: sealed with the procurement
key (§5), never logged, UI shows `email_masked` and the plan only. To do: wire it into the `upstream_direct`
checkout and the redemption site (M3).

**Live verification (2026-11-15) [V].** Owner test session (Plus, personal workspace, Google login, no MFA):
access token valid about 10 days from issue, session cookie until 2027-02-13. Through the ipcook SOCKS5 exit
(US, residential) `GET /backend-api/me` and `GET /backend-api/accounts/check/v4-2023-04-27` return 200 with a
plain `Authorization: Bearer` header and the session's own User-Agent; the entitlement shows an active
`chatgptplusplan`. Both proxy geos tested (`-US_city_LosAngeles`, `-PH`) exit in the named country.
Read-only calls only; no checkout or payment endpoint was called.

**Backup supplier — future work, parked by the owner on 2026-11-15 (payment instrument and billing
countries undecided); nothing built.** Owner direction: keep ZovoCard as the primary upstream
(the protocol changes often, so the vendor absorbs that) and add a second adapter under `suppliers/` that
performs the subscription itself, using the payment protocol of `gpt-subscribe-studio` and, alternatively,
browser session injection (currently done by hand with a Chrome extension). Before any code:
1. Read the fork and write `docs/research/gpt-subscribe-studio.md` (licence, the exact endpoints and payment
   flow, what it needs from the buyer, failure modes, how often upstream changed).
2. Decide the trust boundary: it holds buyer sessions and a payment instrument, so it belongs in the
   `procurement` process (egress, no vault private key), with the proxy as per-supplier settings and
   credentials in `env/procurement.env`, never in the repo.
3. Owner decisions needed: which payment instrument funds the purchases, which billing countries are used,
   and acceptance of the account/terms-of-service risk on the upstream platform. This adapter moves real
   money, so invariant 11 applies: no production use before the independent security review.


## 16. Payment-audit fixes on M2 (2026-11-15)

Engine rules added after the payment-auditor pass (migration `0027`; tests `tests/test_procurement_audit.py`):

- **Order state.** Nothing is bought, replayed or bound for an order that is not `paid`. A held order parks its
  job in `needs_manual` (`order_not_paid`); a refunded / closed one fails it. A job that may have reached the
  supplier (`maybe_sent` or `upstream_ref`) is never dropped: `failed` + `recover` makes the engine ask once under
  the SAME key to learn the reference, then disable the code (`voided`), or end in `needs_manual`. An answer that
  arrives after the order left `paid` is not sealed; the code is disabled (`void_pending`). Refund and "close held
  order" close the order's open jobs with audit rows.
- **Availability first.** `availability()` (read-only) runs before the first send of a job; an unavailable plan
  fails over (nothing was sent) or goes to `needs_manual` and sets `last_check_ok = false`. An ambiguous
  `request_failed` on a write stays AMBIGUOUS for money but no longer stops the supplier.
- **`maybe_sent`** (set in the lease commit, before the first call) replaces the error-code guess: switch,
  failover and "maybe charged" read it. It is cleared only when the supplier said "nothing happened". `request_hash`
  is enforced in every mode; a same-key replay older than `APP_REPLAY_MAX_AGE_S` (default 21600 s) is refused
  (`replay_expired`).
- **Balance breaker fails closed.** An offer is enabled only with a `cost_estimate` in the supplier's currency or a
  cost the supplier named itself (`cost_known`); `unknown_cost > 0` opens the (self-closing) balance breaker; no new
  purchase is leased while the last balance reading is older than `APP_BALANCE_STALE_S` (default 900 s); queued jobs
  of a supplier held by a balance breaker back off and resume by themselves.
- **Staff actions.** `fulfil` / `fail` answer 409 `job_in_flight` while the engine's lease is live; for a possibly
  sent job they schedule the same recovery. `void` also voids the sealed `card_stock` row.
- **Locks.** One order: job set (one ordered statement), then order, then supplier. Refund / close take the job set
  `NOWAIT` (409 `procurement_busy`).
- **Left open.** L3 (release of late-payment upstream orders) and L6 (webhook pulling `next_poll_at` forward on a
  leased job) are marked `TODO(v12-M3)` in the code. (The earlier note that a hand-fulfilled job's supplier code is
  disabled by the recovery is superseded by "Re-audit fixes" below.)

### Re-audit fixes (2026-11-16, N1-N8)

- **Recovery policy and cost bound (N1).** The recovery of a closed job (`maybe_sent`, no `upstream_ref`) is one
  same-key replay -> record the reference -> disable the code. It stays because the replay is the only way to learn
  whether a code exists (the vendor's CDK list has no idempotency key). Its worst case is ONE issue fee per job (the
  key makes a second replay a lookup, never a second purchase). It honours the kill switches: no recovery call while
  the supplier is disabled or its `hard` breaker is open; the job waits (backoff, `RECOVER_BLOCKED_MAX` = 20
  attempts, about 1.5 h) and then goes `needs_manual` with `recover_blocked`, still flagged maybe charged.
- **Hand fulfil (N2).** For a hand-fulfilled job the recovery records `upstream_ref` and writes a critical audit row
  `procurement.recovered_after_manual_fulfil`; it never disables the code (staff may have pasted exactly it).
  Disabling is an explicit staff `void`. Jobs closed by refund / fail keep the automatic disable.
- **Freshness (N4).** `last_balance_at` is stamped only after the breaker evaluated the reading; a failed evaluation
  leaves the gate stale.
- **Pre-check (N3).** The pre-check before the first send reads a FRESH catalog, so a paused plan is not
  submitted. A write that fails ambiguously stays ambiguous whatever the catalog says afterwards (a plan can sell
  out because the write ran): same-key reconcile, never a failover, `needs_manual` flagged maybe charged at the end.
- **Retries (N6, N7).** The recovery's FATAL path keeps `maybe_sent` on `IDEMPOTENCY_CONFLICT`; a passed pre-check no
  longer resets the retry counter.
- **Refund (N5).** `fulfilled`, undelivered units go to `void_pending` (disable + void the `card_stock` row).
- **Parked jobs (N8).** A queued never-sent job behind a balance breaker fails over when another source exists; the
  stuck alert also fires for a long park. A `maybe_sent` job with a NULL `request_hash` is treated like a mismatch.
