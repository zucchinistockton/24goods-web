# review-v12 — multi-supplier procurement and upstream fulfilment

Status: **M1, M2 and M3 (`upstream_vcc` §17, `upstream_direct` §18) built**. ADR-038. Migration `0023_procurement`.
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

### 8.1 Built backend and HTTP contract (for the SPA author)

**Topology.** `python -m app.procurement` runs a second uvicorn listener (`app/procurement/redeem.py`,
`APP_REDEEM_ENABLED=1`, default off) on `APP_REDEEM_HOST:APP_REDEEM_PORT` (compose: `10.89.15.30:8006`). It
serves only `/redeem/*`. The internal API (`:8004`) is a different listener bound to procurement's `backend`
address (`10.89.11.30`); `web` (Caddy) shares only the internal `redeem` network (`10.89.15.0/24`) with
procurement, so Caddy has no path to `:8004`, and the public listener has no `/internal/*` route (both
tested). Caddy site `https://{REDEEM_HOST}` (`deploy/Caddyfile`, `Caddyfile.prod`; `REDEEM_HOST` defaults to
`redeem.invalid` = off; demo bootstrap uses `redeem.localhost`, prod only when `REDEEM_HOST` is given) serves
`/srv/redeem` (image: `deploy/redeem/`, replaced by the SPA build) with `default-src 'none'; script-src 'self';
style-src 'self'; img-src 'self' data:; connect-src 'self'; ...`, strips `Cookie`/`Authorization` before the
proxy, forwards only the four paths below and answers any other `/redeem/*` with 404. Same origin, so the proxy
sends **no CORS headers** and refuses a POST whose `Origin` differs from `Host` (403 `forbidden`).

**Verified against the vendor sandbox (2026-11-16, through `redeem.kesiv.com`, real session text) [V].** Preview and preflight field names as listed below; redeem answers `data.order.{id,status,...}` plus `data.events[]` (the order id is `data.order.id`); a real ChatGPT session text is about 37 KB. The result shape is still unverified (the sandbox answers 404).

**Which supplier.** The proxy cannot tell cheaply and safely which environment issued a code (card rows hold
sealed secrets; `app_procurement` cannot read them). It uses the **single enabled ZovoCard supplier of the
platform tenant** (a reseller's supplier is never used); if none or more than one `(code, env)` is enabled
the answer is `unavailable`. `APP_REDEEM_SUPPLIER_ENV=sandbox|production` pins one environment when both are
enabled. The vendor base is derived from that supplier's `BASE_URL` (`scheme://host/api/v1/cdk`); no API key
is sent (the vendor's public steps need none) and https is required outside dev/test.

**Conventions.** JSON in, JSON out; POST bodies need `Content-Type: application/json`, at most 96 KiB, only the
listed keys (unknown keys = `invalid_request`). Every response has `Cache-Control: no-store`; no cookies are
ever set. Success: `{"data": {...}}` (only the fields below, everything else from the vendor is dropped).
Failure: `{"error": {"code": "<code>", "message": "<fixed English text>"}}`; localise by `code`. Never put the
code or the credential in a URL; keep `redemption_token` for 15 min (the vendor session) and reuse the same
`client_request_id` when retrying `redeem`.

| Step | Request | `data` on success |
|---|---|---|
| `POST /redeem/preview` | `{"code": "<CDK>"}`; `[A-Za-z0-9._:-]{8,128}` | `redemption_token`, `expires_at` (ISO), `plan`, `plan_flow`, `funding_cap_minor` (int, 0 = none) |
| `POST /redeem/preflight` | `{"redemption_token", "credential": {"mode": "session", "session": "<ChatGPT session text, 8..65536 chars (a real one is about 37 KB), no control chars>"}}` | `preflight_token` (**null for Grok/Claude codes**), `preflight_expires_at`, `payment_country` (e.g. `PH`), `payment_currency`, `subscription_recovery_required` (bool) |
| `POST /redeem/redeem` | `{"redemption_token", "client_request_id": "[A-Za-z0-9._:-]{1,80}", "preflight_token"?, "credential"?}`; at least one of `preflight_token` (GPT/X) or `credential` (Grok/Claude, same account as preflight) | `order_id`, `status`, `stage`, `terminal` (bool). Success means *accepted*, not paid: poll result |
| `POST /redeem/result {"redemption_token"}` (POST only: a token never travels in a URL on our side; `GET` answers 405) | | `order_id`, `status`, `stage`, `cdk_status`, `terminal`, `events[]` (`type`, `status`, `at`; at most 50). `404 not_found` is normal for a while (and always in the vendor sandbox); keep polling every 3-5 s up to ~2 min, then show "check later" |

`card_id`, API keys and any other field are not accepted. Tokens are `[A-Za-z0-9._~+/=:-]{8,512}`.

| `error.code` | HTTP | Meaning / SPA action |
|---|---|---|
| `invalid_request` | 400 | malformed input, caught locally (nothing reached the vendor) |
| `forbidden` | 403 | cross-origin request |
| `not_found` | 404 | result not available (yet); also unknown path |
| `method_not_allowed` | 405 | wrong method |
| `payload_too_large` | 413 | body over 96 KiB |
| `code_unavailable` | 422 | unknown, disabled, used or wrongly-scoped code (one code: no oracle) |
| `credential_invalid` | 422 | the session was not accepted; ask for a fresh one |
| `account_ineligible` | 422 | the account cannot take this plan (e.g. plan already active) |
| `rejected` | 422 | any other vendor refusal (cards, funding); show a generic failure + support link |
| `session_expired` | 409 | redemption/preflight token invalid or expired: restart from preview |
| `conflict` | 409 | `client_request_id` reused with other parameters: show result, do not resubmit |
| `rate_limited` | 429 | `Retry-After` seconds; applies to our limits and to the vendor's |
| `unavailable` | 503 | vendor down, no/ambiguous supplier, our own failure (limits fail closed when Redis is down) |
| `timeout` | 504 | vendor did not answer in 20 s; for `redeem` retry with the **same** `client_request_id`, or poll result |

**Limits (all fixed 60 s windows, settings `APP_REDEEM_*`).** Per client IP and step: preview 10, preflight 10,
redeem 6, result 40. One shared budget per step for all buyers, because the vendor limits per *egress* IP
(30/20/60 per minute) and every buyer shares ours: preview 25, preflight 20, redeem 15, result 50. Per code
(keyed HMAC): 10 previews per 10 min. Per redemption token: 40 requests per minute. Client IP comes from
`X-Forwarded-For` only when the peer is in `APP_REDEEM_TRUSTED_PROXIES` (compose: Caddy's `10.89.15.10`).
Redis keys are `rl:proc:redeem-*` with a keyed hash of the identifier (inside the `procurement` ACL; no
bootstrap change needed). Upstream timeout 20 s (5 s connect), one request per step, no automatic retry.

**What is never recorded.** Log lines carry the step, the mapped outcome and the vendor HTTP status only;
no body, header, URL query, exception message, code, token or credential (the `httpx` logger is forced to
WARNING because the vendor's result call carries the token in its query). No audit rows are written, no
database is touched except the supplier lookup (cached 30 s). Caddy drops query strings, `Cookie`,
`Referer` and `Authorization` from its access log on this host. `X-Redemption-Device` is the constant
`APP_REDEEM_DEVICE` (bootstrap generates `24g-<24 hex>` once per deployment); egress is procurement's
`procurement_egress` network.

**Settings** (`ProcurementSettings`, env `APP_REDEEM_*`): `ENABLED`, `HOST`, `PORT`, `DEVICE` (required when
enabled, `[A-Za-z0-9._-]{8,64}`), `SUPPLIER_ENV`, `TRUSTED_PROXIES`, `UPSTREAM_TIMEOUT_S`, `IP_LIMITS`,
`GLOBAL_LIMITS`, `CODE_PER_10MIN`, `TOKEN_PER_MIN`. Deploy: `REDEEM_HOST` in `deploy/.env` (bootstrap).
Mock vendor: `app/devtools/mock_zovocard.py` serves the four steps (device-bound tokens, `SBXORD-` order,
CDK stays `unused`, `APP_MOCKZOVO_RESULT_404=1` reproduces the sandbox's missing result).

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

## 17. M3 — `upstream_vcc` (virtual cards bought per order)

Migration `0028_procurement_vcc`; tests `backend/tests/test_procurement_vcc.py` (+ the `upstream_vcc` step of
`e2e/smoke.py`). `upstream_direct` is **not** part of this part. Frontend: not built, the API it needs is listed below.
Everything M2 and §16 established applies to cards unchanged (order-status check in every executing mode,
`maybe_sent`, same-key replay, `request_hash`, replay age, recovery, void of fulfilled-undelivered units, both
breakers, fail-closed cost). Production supplier writes stay gated exactly as in M2
(`APP_PROCUREMENT_ALLOW_PRODUCTION_WRITES`).

### 17.1 Offer parameters and availability

A product with `delivery_mode = upstream_vcc` has sources (`supplier_offers.kind = 'vcc'`, the kind follows the
TARGET product's mode, never the request). `params` (decimal strings, at most 2 decimals, validated by
`vcc_spec()` on create/patch and again before every send):

| key | meaning |
|---|---|
| `product_code` | the supplier's card product (`GET /products`) `[A-Za-z0-9_-]{1,40}` |
| `init_amount` | opening amount in USD (> 0, <= 100000) |
| `recharge_amount` | optional top-up after opening (own idempotency key, see below) |
| `first_name`, `last_name` | optional cardholder name, letters only (default `Digital` / `Goods`) |

Availability (read-only, fresh before the first send): the product is listed and `enabled is True`, `init_amount >=
min_amount` and `init_amount + recharge_amount <= max_amount` (`amount_out_of_range` otherwise, a definite "no" that
fails over / goes to `needs_manual` without a call). The adapter names the cost (`init + recharge + open_fee`), so a
source can be enabled without a staff `cost_estimate` (`cost_known`). Amounts always come from the offer; a buyer
never sends one (§1.3). Storefront stock follows the same rule as CDK (an enabled source, last check passed, breaker
closed). Catalogue and offer cost are cached 60 s; the engine's pre-check bypasses the cache.

### 17.2 State machine (one job per unit; same states as §4)

```
queued -> [precheck: availability, fresh] -> submitting (step ready -> sent; maybe_sent = true, committed first)
   submit/query = ONE idempotent walk, every step under its own fixed key:
     1. POST /cards/open      key pj:<job>:submit      ONLY while the job has no card id (17.8 F2); afterwards
                              the walk continues on the recorded card, never opens again
     2. GET /cards/{id}       balance witness: skip the top-up when available >= init + recharge (17.8 F5)
        [recharge_amount]     else GET /cards/{id}/recharges first; a row = it ran (never POST again), else
                              POST /cards/recharge   key pj:<job>:recharge
     3. GET /cards/{id}       must be ACTIVE and hold the expected amount; PAN / expiry / CVV -> Result.secrets
   OK                  -> fulfilled (card_stock row, procure.fulfilled when every unit is)
   recharge "pending"  -> accepted_pending, step pending; polls re-run the walk, which reads the recharge list
                          and NEVER POSTs the recharge again (the vendor's pending 400 is not cached)
   AMBIGUOUS / timeout -> reconcile: the same walk under the same keys (open and recharge replay as cache hits)
   refused after open  -> the card id is recorded on EVERY answer that carries it, so the job is never "nothing
                          happened" (no failover, `needs_manual`, flagged maybe charged, voidable)
fulfilled (delivered or not) / refund / close -> void_pending: DELETE /cards/{id} (key pj:<job>:void) -> voided
                          (a refund of a DELIVERED card also deletes it, 17.8 F1); a refused / unresolved void ->
                          needs_manual `void_failed` (17.8 F3)
closed job with a possible card (recover) -> one same-key open replay learns the card id (no recharge, no reveal,
                          nothing sealed), then the card is deleted
```

Void = **`DELETE /cards/{id}` only**. The vendor returns the whole remaining balance to the wallet on delete; the
separate `POST /cards/refund` must leave USD 1 on the card and adds nothing (deviation from the §5 table, safer).
`GET /cards/{id}` already answering `DELETED` is "done"; a `202` is "pending" (the engine asks again with backoff, the
read answers). Recharge-pending is resolved only through `GET /cards/{id}/recharges` (`success` -> continue; failed ->
FATAL `recharge_failed`; still pending -> keep waiting); a pending job whose list is still empty waits, it does not POST.
Error mapping uses the existing table; `/cards/recharge` keeps the "400 without a known terminal code is pending" rule.

### 17.3 The card secret path (§1.1)

* PAN, expiry and CVV are read from `GET /cards/{id}` (the open answer's copies are dropped in the adapter), joined
  into ONE secret `"<pan>----<MM/YY>----<cvv>"` (`base.card_secret()`; format-validated: PAN 12-19 digits, expiry
  `MM/YY[YY]`, CVV 3-4 digits) and returned only in `Result.secrets`. `Result.__repr__` hides it.
* The engine seals it with the vault PUBLIC key (`CardSealer`, AAD = tenant, product, card_stock id) straight into
  `card_stock` as `status = 'sold', order_id = <order>` (the 0024 policy still forbids anything else). `fingerprint`
  and `lookup_fp` do NOT derive from the secret (see 17.8 S1). Procurement
  holds no private key and no SELECT on `card_stock`.
* Delivery reveals it through the existing path unchanged: `POST /orders/{id}/reveal` -> `{"secrets": [...], "cards":
  [{"id", "code"}]}` where `code` is that one string. The buyer app splits it by the product's `delivery_separator`
  and labels the parts with `delivery_format`. Backward compatible: nothing in the reveal API changed.
* For a `upstream_vcc` product the API forces `delivery_separator = "----"` and a default `delivery_format`
  (`Card number----Expiry (MM/YY)----CVV`) on create and patch (the sealed format depends on it).
* Stored outside the vault there is only `procurement_jobs.card_mask` (first 6 + last 4, `mask_pan`) and the vendor
  card id (`upstream_ref`). Webhook payloads are masked before insert, balance-log rows are masked in the adapter (the
  vendor's `remark` is dropped). No log line, audit row, exception, Redis key or outbox payload carries a card field
  (`test_no_card_secret_outside_the_vault` greps all of them, plus the live Redis keyspace).
* Hand fulfil (`POST /admin/procurement/jobs/{id}/fulfil`, ADMIN only for a card job) takes `code = "<pan>----<MM/YY>----<cvv>"`,
  validated by the same `canonical_card_secret()` (422 `bad_card`), sealed through the same path, `card_mask` set.

### 17.4 Cost ledger

Cost is booked exactly once per vendor balance-log row (`UNIQUE(supplier_id, external_id)`; webhook `balance_change`
and the 10-minute backfill share one insert path). The opening charge arrives as `card_recharge`, the open fee as a
separate `open_card` row, a top-up as another `card_recharge`, a delete as `card_refund` (positive). The row's own
`ref_id` is the vendor's and is NOT the card id, so migration 0028 adds `supplier_ledger.card_id`; a card row links to
its `vcc` job through `card_id = upstream_ref`. `job.cost` is the estimate (`init + recharge + open_fee`); the ledger is
the truth. A card is paid at purchase: the balance breaker reserves only IN-FLIGHT card jobs (the CDK
"issued, unredeemed" reserve is limited to `kind = 'cdk'`).

### 17.5 Webhooks and 3DS

* `card_operation` / `card_transaction` / `card_fee` are hints. `card_operation` with a `card_id` of a `vcc` job
  records `upstream_status = "<operation>:<status>"` and makes an ACTIVE job due (never under a live lease: L6 is
  fixed for CDK events too). The re-query stays the truth; `card_transaction` / `card_fee` change nothing (fees reach the
  ledger only as `balance_change`).
* **3DS over SSE (built).** `card_3ds_otp` is the only event whose secret (`otp`) is read from the RAW verified body
  (`modules/orders/card_otp.py`); `supplier_events` keeps only the masked whitelist payload. For a NEW event (a
  duplicate delivery never re-sends) the code goes to the owner of the card only: webhook `card_id` -> `fulfilled`
  `vcc` job -> order -> buyer, and only while the order is `delivered`/revealable; otherwise it is dropped. It is
  `SET otp3ds:<order>:<unit> EX 300` in Redis and published once on that buyer's realtime channel as event `card_otp`;
  it never reaches PostgreSQL, logs, audit rows (a `card.otp_forwarded` row records the unit, not the code) or the
  outbox. Not verified against the real vendor: the field names are as documented (`otp`, `card_id`) and the sandbox
  cannot trigger one (§12, §14.3). No vendor event was observed; the path is covered by signed self-made events.

### 17.6 API the buyer app and the console need

Buyer (`/api/v1`):

| Route | Notes |
|---|---|
| `POST /orders` with an `upstream_vcc` product | unchanged; quantity capped by `MAX_UPSTREAM_QTY` (5); the existing out-of-stock error when no source is usable |
| `POST /orders/{ref}/reveal` | unchanged; `cards[].code` is `pan----MM/YY----cvv` for card orders |
| `GET /orders/{ref}/card-otp` | NEW. Owner only (404 otherwise, also for non-card orders), `Cache-Control: no-store`, 60 req/min. `{"items": [{"otp", "card" (masked), "unit", "expires_in" (s)}]}`; empty when none is live |
| SSE `GET /stream`, event `card_otp` | NEW. `{"order_id", "otp", "card", "unit", "expires_in"}`; show it once, never persist it client-side beyond the page |
| order detail | an order that is `paid` for a while shows the existing `paid`/`manual_review` messages; no new fields |

Console (admin, staff):

| Route | Change |
|---|---|
| `POST /admin/products`, `PATCH /admin/products/{id}` | `delivery_mode` accepts `upstream_vcc` (separator/format forced, see 17.3) |
| `POST /admin/procurement/sources` / `PATCH .../{id}` | `params` for a card product: `product_code`, `init_amount`, optional `recharge_amount`, `first_name`, `last_name` (strings; `plan` is for CDK products only). Errors: 422 `bad_params`, `cost_required` (when enabling without a usable cost) |
| `POST /admin/procurement/sources/{id}/check` | availability incl. `amount_out_of_range` / `not_purchasable` |
| `GET /admin/procurement/jobs`, `/{id}` | new `card_mask`; `kind = "vcc"`; `upstream_status` is `active`/`deleted` or the last webhook `operation:status`; no secret ever |
| `POST /admin/procurement/jobs/{id}/fulfil` | card jobs: `code = "<pan>----<MM/YY>----<cvv>"`, 422 `bad_card` |
| `.../void`, `/fail`, `/retry`, `/requery`, `/switch` | unchanged semantics; void = delete at the supplier |

New error codes: `bad_card` (422), `bad_params` (existing, now also for card params), job `error_code` values
`amount_out_of_range`, `malformed_open`, `malformed_card`, `recharge_pending`, `recharge_failed`, `delete_pending`.
Settings: none new (the M2 ones apply: `APP_REPLAY_MAX_AGE_S`, `APP_BALANCE_STALE_S`, `APP_JOB_LEASE_S`, the
production-write gate). Tenant isolation: every query filters by tenant; `test_cross_tenant_card_job_is_invisible`
covers list, detail, every action and a row-level-security read.

### 17.8 Payment-audit and security-review fixes (2026-11-16)

Tests: `backend/tests/test_procurement_vcc_audit.py`. No migration change (0028 untouched).

| # | Rule now |
|---|---|
| F1 | A refund / close of an order whose card job is `fulfilled` voids the card even when the order is DELIVERED (`void_pending`, DELETE returns the remaining balance), the local `card_stock` row goes to `void`, and a critical `procurement.delivered_card_voided` audit row is written. Delivered CDK codes are unchanged (after-sales). |
| F2 | `POST /cards/open` is sent only while the job has no card id. With `upstream_ref` known the walk continues on that card. An answer that carries a different id than the recorded one is never adopted: `needs_manual` `card_mismatch`, `maybe_sent` kept, critical `procurement.card_mismatch` audit with both ids (not secrets). The reference (and the first cost) is recorded on every answer that has one. |
| F3 | A void the supplier refuses, or that is unresolved after its budget, ends in `needs_manual` with `error_code = void_failed` and a critical `procurement.void_failed` audit row (CDK too; also closes the M2 re-audit note). Staff can retry with `void`. Budgets: 10 retries for an error, `VOID_PENDING_MAX` = 100 polls (~24 h at the 15-minute cap) for a pending delete. |
| F4 | After the first `202` the engine persists step `delete_pending`; from then on only `GET /cards/{id}` runs until it says `DELETED` (the DELETE is never re-sent). |
| F5 | The card read is the balance witness: no top-up when `available_amount >= init + recharge`; before sealing the card must be `ACTIVE` (`card_not_active` otherwise, FROZEN is not delivered) and hold the expected amount (`balance_short` -> `needs_manual`). Unreadable amount/status = `malformed_card` (ambiguous). |
| F6 | Hand-pasting a card whose mask differs from the vendor mask already recorded overwrites `card_mask` with the delivered card's, and writes a critical `procurement.manual_card_differs` audit row with BOTH masks and the vendor card id (the vendor's card is an orphan to delete with `void`). Every card paste writes `procurement.card_pasted` (mask only). |
| F7 | A void waits (`recharge_pending`, same poll budget) while a recharge row is non-terminal instead of deleting under it. |
| F8 | On `/cards/recharge` only a 400 WITHOUT `error_code` is "pending". A coded 400 keeps its table rule, an unknown code is FATAL (`needs_manual`). |
| F9 | Any ledger row with a `card_id` links to its `vcc` job (not only the known card types). |
| F10 | The estimate / reserve includes the top-up fee: `recharge_amount x catalogue recharge_fee` (a rate; 0 when the catalogue does not list one). The ledger rows stay the truth. |
| F11 | `card_operation` dedup key uses the card id when `operation_id` is empty (delete); the OTP forwarder and the dedup MAC read the same field list (`otp`, `code`, `verification_code`). |
| S1 | A card's `card_stock.fingerprint` is `HMAC(fp_key, tenant | "vcc" | vendor card id)` (hand-pasted: order id, unit, last 4) via `CardSealer.card_fingerprint`, and `lookup_fp` is NULL. No PAN / expiry / CVV is an input of any key the API can compute, so a database read plus `fp_key` recovers nothing. Staff find a card by `card_mask` / card id, not by its number ("find cards" no longer matches a card by content or first field). Imports and replacements only exist for `auto_card`. |
| S2 | Hand fulfil of a `vcc` job needs an admin (403 for agents). A `reason` with 12+ digits (spaces/dashes allowed between them) is refused with 422 on every job action (fulfil, fail, void, ...). |
| S3 | `test_card_secret_never_leaves_the_vault_strict_sweep`: every public table via `row_to_json`, admin job/source/order HTTP bodies, pub/sub payloads and DEBUG logs, with a standalone 4-digit CVV and a `MM/YY` expiry, across two failure paths (ambiguous open, malformed card read). |
| S4 | `GET /orders/{ref}/card-otp` is empty unless the order status is revealable. |

New fields / codes: job `error_code` `card_mismatch`, `void_failed`, `card_not_active`, `balance_short`; job `step`
`delete_pending`; audit actions `procurement.delivered_card_voided`, `.card_mismatch`, `.void_failed`, `.card_pasted`,
`.manual_card_differs`. Not changed: routes and settings.

### 17.9 Re-audit notes (2026-11-16, round 3)

* **Balance witness tolerates REPORTED fees.** Required balance = `init + recharge - (card open_fee + sum of the
  successful recharge rows' fee)`, never below `init - open_fee`; a card under that is `balance_short`. A recharge row
  existing still dominates ("it ran, never POST again"). A `balance_short` / `card_not_active` job has `upstream_ref`,
  `maybe_sent`, `card_mask`; the job view adds `card_id` (the supplier's id, not secret), `charged_possible` is true,
  `switch` is not offered and `void` deletes the card.
* **Hand-pasted card dedup.** The fingerprint no longer derives from the secret, so a paste is refused with 409
  `duplicate_code` when another non-voided card job of the tenant has the same `card_mask` (first 6 + last 4; a collision
  of two different live cards in one tenant is rare and acceptable: void the other or give the vendor card id). The
  paste body may carry the supplier's `card_id`; it becomes the fingerprint input exactly as in the engine path, so the
  UNIQUE catches an engine/hand overlap (409 `duplicate_code`).
* **`void_failed` alert.** A job that ends in `needs_manual` `void_failed` raises its own staff alert through the
  reconciler's procurement alert path (`proc.void_failed`, en + zh-CN, once per job: key
  `procurement_job:<id>:void_failed`), not only an audit row.
* **Admin `void` and `delete_pending`.** Voiding a job whose step is `delete_pending` keeps that step (no second
  DELETE, the card is only read); only a job in `void_failed` (step `done`) restarts the void from the beginning.

### 17.7 Left open

* The recharge-pending 400 body and the `card_3ds_otp` payload are still [I] (not reproducible in the sandbox); the
  mock reproduces them as documented. Confirm with the first sandbox/production card.
* `cards/open` replay window: the vendor's idempotency retention is unknown; `APP_REPLAY_MAX_AGE_S` (6 h) bounds it.
* **Verified in the real sandbox [V] (saved responses, 2026-11-15):** `POST /cards/open` with 10 USD answers
  `available_amount: 10`, `open_fee: 0`, `status: "ACTIVE"` (the card object carries `open_fee`);
  `GET /cards/{id}/recharges` lists ONLY recharges (two recharges of 10 = two rows `{id, card_id, amount, fee, status:
  "success"}`, the opening funding is not a row) and after two recharges the card read shows `available_amount: 30`;
  `GET /cards/{id}` of a DELETED card answers HTTP 200 with `available_amount: 0` (not 404); `DELETE` answers
  `{"pending": false}` (a `pending: true` is treated like a 202). Sandbox fees are 0, so whether a fee is taken from the
  card balance or from the wallet is still unknown (17.9 tolerates exactly what the vendor reports). The mock mirrors
  these shapes (`short_open`, `fee_from_card`, `recharge_fee` knobs).
* The top-up fee uses the catalogue's `recharge_fee` as a rate (vendor 6.1); if the vendor changes its meaning the
  estimate is off, the ledger is not.
* `min_recharge` (card detail) is not read: a top-up below it answers `amount_below_min` (FATAL, `needs_manual`, the card
  is deletable by `void`). Read it before the first top-up offers go live.
* A card that is bought but never delivered because the buyer never reveals it stays `ACTIVE` at the supplier until the
  refund/close path (no automatic expiry).
* `upstream_direct`, the frontend (buyer card view with copy buttons + 3DS panel, console card-source form), and the
  production egress allowlist (§14.4) remain.

### 17.10 Owner decision 2026-11-17: no sales, no reveal

Buyers misused cards for $0 authorisations and card-binding checks, which cost vendor fees and risk.
The owner stopped both the sale and the display of card data. The engine below stays dormant and testable.

- **No new sales** (`APP_VCC_SALES_ENABLED`, default `false`, keep false). While false: admin create of an
  `upstream_vcc` product is 422 `vcc_not_sellable`; a product of that mode cannot be set active (PATCH of
  `status`/`delivery_mode`) and bulk publish skips it; the storefront (list, detail, by-ids, recommendations,
  promo lines, SEO, reseller catalogue) never lists it; buy-now, cart checkout, quote creation and quote
  acceptance (all via `create_order`) answer 422 `vcc_not_sellable`. Existing rows are untouched.
- **No reveal, unconditional** (not behind the setting): `reveal_for` and `can_reveal` refuse the mode, and the
  delivery worker's `/internal/reveal` refuses it too (409) even with a valid internal token. A card order is
  still bought, sealed and delivered; the buyer never sees the number. Staff keep `card_mask`.
- **No 3DS codes**: `card_otp.forward` drops every code before any lookup (nothing read, stored, published or
  audited). `GET /orders/{ref}/card-otp` stays and always returns `{"items": []}` so old clients do not break.
  The buyer panel is removed from the app. Existing jobs stay visible and can be voided/refunded in Procurement.

## 18. M3 part 2 — `upstream_direct` (the supplier subscribes the BUYER's own account)

Migration `0029_procurement_direct`; tests `backend/tests/test_procurement_direct.py` (+ the `upstream_direct`
step of `e2e/smoke.py`). Frontend: not built, the API it needs is below. Everything M2 / §16 / §17 established
applies unchanged (order-status check in every executing mode, `maybe_sent`, same-key replay, `request_hash`,
replay age, recovery, reference recorded on every answer, never re-POST a known order, void failure =
`needs_manual` `void_failed`, fail-closed cost, both breakers, production supplier writes gated by
`APP_PROCUREMENT_ALLOW_PRODUCTION_WRITES`). The vendor flow is documented in `zovocard-openapi.md` §6.18.

### 18.1 The buyer credential (AGENTS.md §1.1 level of care)

An account session (a ChatGPT session text is about 37 KB) is a live credential for the buyer's account.

* **Separate keypair.** `python -m app.devtools.keygen` now also writes `cred_public_keys.json` (-> API) and
  `cred_private_keys.json` (-> procurement ONLY, 0400) and `APP_CRED_ACTIVE_KID` in `secrets.env`. It is NOT the
  card vault's pair: delivery (vault private keys) cannot open it, the API (public keys only) cannot open either.
  Same primitive as the vault (`app/core/credvault.py`: X25519 + HKDF + AES-256-GCM, `kid` for rotation); the
  AAD binds a blob to `(tenant, "buyer-credential", order)`, so it cannot be moved to another order or mistaken
  for a card secret. `ProcurementSettings` still refuses `APP_VAULT_PRIVATE_KEYS_FILE`; the credential private key
  is `APP_CRED_PRIVATE_KEYS_FILE`, a different variable.
* **Where it lives.** Table `procurement_credentials` (FORCE RLS, `tenant_isolation`, UNIQUE `order_id`,
  `blob` 64 B-140 kB, plus the non-secret `product` / `email_masked` / `kid` / `created_at`). Grants:
  `app_api` INSERT and column-SELECT of `id, tenant_id, order_id, product, email_masked, created_at` only (a
  `SELECT blob`, `SELECT *`, `UPDATE` or `DELETE` as `app_api` fails with "permission denied": tested);
  `app_procurement` SELECT + DELETE (no INSERT / UPDATE); `app_delivery` nothing. The scan / sweep policies of
  `app_procurement` apply to UNPINNED sessions only (the sweeper); a pinned session sees only its own tenant.
* **Checkout.** `POST /orders` for an `upstream_direct` product REQUIRES `session`; the API validates it
  (`app/suppliers/direct_credential.py`: size <= 64 KiB by bytes, no control characters, the GPT session check of
  §15, `sk-ant-` shape for Claude, an `eyJ...` / `sso=` cookie for Grok), seals it at once with the procurement
  PUBLIC key inside the order's transaction and never stores, logs, audits, or echoes it. `orders.buyer_input`
  (plaintext, visible to staff) is NOT used and stays empty. A 422 for an over-long field never echoes the input.
* **Use.** The engine opens the blob in memory at the moment of the supplier call (`_open_credential`: key held
  by this process only), hands it to the adapter in `JobSpec.credential` (`repr` hidden), the adapter sends it to
  the vendor (preflight and order POST) and it goes nowhere else: not a job column, not Redis, not the outbox,
  not an audit row, not a log line, not an exception message, not an admin response, not a job view.
* **Deletion.** The engine deletes the row in the same transaction as the `fulfilled` transition; the sweeper
  (every `APP_CRED_SWEEP_INTERVAL_S`, default 30 s, also staff actions which run as `app_api` and cannot delete)
  removes it when every direct job of the order is terminal (`fulfilled`/`failed`/`voided`), when the order is no
  longer `pending_payment`/`paid`/`manual_review`, and in any case after `APP_CRED_MAX_AGE_S` (default 5 h; the DB backstop is 6 h, the
  same bound as a same-key replay). `needs_manual` keeps it (a retry may need it) until the max age.
* **Re-supply.** A session that expired or was rejected: the owner `POST /orders/{ref}/direct-session` (only a
  `paid` / `manual_review` direct order that holds no session; 409 `session_exists` otherwise); staff then retry.
* **Production gate.** The preflight carries the buyer's session, so it obeys the production-write gate like a
  write (`read_only_post`): with writes disabled nothing leaves the process (tested).

### 18.2 Adapter (`suppliers/zovocard/direct.py` + `supplier.py`)

* **Enum lock.** `params.product` is REQUIRED and one of `gpt|claude|grok` (the three literals the vendor
  documents; the §5 table's `x` is not an order product in the vendor docs and is not accepted). The vendor
  silently turns an unknown `product` into `gpt` and charges it, so anything else is refused server side: by
  `direct_spec()` before every call, and by the console (`bad_params` / schema 422). `plan` is the registry key
  (no `*_renew`, no `credit*`); Claude / Grok are pinned to `US` / `USD` and sent explicitly; GPT may name a
  `payment_country` + `payment_currency` pair. `card_id` (a card of OUR vendor account the supplier pays with,
  `POST /gpt-direct/orders` requires one) is an offer parameter chosen by staff; `no_auto_card_switch: true`.
  All direct sources of one target must buy the same vendor product (`mixed_products`): checkout asks for one
  kind of session.
* **Availability.** The registry/`plans` rule of §5 (strict identity), plus a USD cost estimate (Claude/Grok:
  `checkout_amount_minor` + service fee + the plan's buffer; GPT: the plan formula, USD plans only) so a source
  can be enabled without `cost_estimate` once checked (`cost_known`).
* **Walk.** `submit`: a job that knows its order id only READS it. Otherwise `validate_credential` ->
  `POST /gpt-direct/preflight` (read-only upstream, no key) -> stop on `quote_error` / `plan_already_active` ->
  `POST /gpt-direct/orders` under the job key with `client_request_id = pj<job hex>` (the vendor also dedups on it;
  a second protection) -> `202` = `accepted_pending` with the order id recorded (and the masked account). A
  `GPT_PRICE_UNCONFIRMED` (stale / used token, new price version) re-quotes ONCE inline under the same key (the
  refusal is definite). `query` is READ-ONLY: the order by id, else the order list searched for our
  `client_request_id`; nothing found = `UNAVAILABLE`, after which the engine resubmits under the SAME key (the
  vendor de-duplicates twice). `GET /gpt-direct/orders/{id}`: `completed` = OK; `queued`, `running`,
  `awaiting_card`, `funding_pending`, ... and any unknown status = pending (never success); `declined`,
  `failed_precharge`, `cancelled`, ... = FATAL `order_<status>` with the reference kept (needs_manual, order held).
* **Void.** A COMPLETED subscription cannot be undone through the API: `void` answers FATAL `order_completed` (the
  engine ends in `needs_manual` `void_failed` + critical audit + a staff alert, `proc.direct_done`). Only an order
  that has not started paying (`queued`/`awaiting_card`/`funding_pending`) is cancelled
  (`POST /gpt-direct/orders/{id}/cancel`, 409 = too late = FATAL `order_not_cancellable`); one already paying is
  waited out (`void_waiting`, the usual poll budget). The vendor's `cancel-renewal` is not used.
* **Error table.** Added: `GPT_DIRECT_ORDER_REJECTED` = AMBIGUOUS (read the order first, never a blind new one),
  `SESSION_REQUIRED` = FATAL. Existing rows cover the rest (`GPT_SESSION_INVALID`, `CLAUDE_SESSION_INVALID`,
  `GPT_PLAN_ALREADY_ACTIVE`, `PRECHECK_REJECTED`, `INSUFFICIENT_BALANCE` (supplier stop), `DIRECT_PRODUCT_DISABLED`,
  `GPT_PREFLIGHT_RATE_LIMITED`...).

### 18.3 State machine (one job per order, quantity 1)

```
paid -> procure.requested -> job queued -> [credential check: sealed row present, key loaded, product matches]
   -> [precheck: availability, fresh] -> submitting (maybe_sent = true, committed first)
   -> preflight + POST /gpt-direct/orders  (202)      -> accepted_pending (order id recorded)
   -> GET /gpt-direct/orders/{id} polled at 15 s ... 15 min:
        completed -> fulfilled (delivery text sealed into card_stock like a code; credential deleted; procure.fulfilled)
        declined / failed_precharge / cancelled -> needs_manual order_<status>  (order held, never re-created)
        still pending after APP_DIRECT_PENDING_MAX_S (default 2 h) -> needs_manual pending_too_long
   ambiguous POST (timeout, 5xx) -> reconcile by READS (order list by client_request_id) -> found: continue;
        not found: resubmit under the SAME key + request id (fresh preflight token)
   no session (never sealed / expired / unreadable key / product mismatch) -> needs_manual credential_missing |
        credential_unreadable | credential_mismatch  BEFORE maybe_sent is set (nothing was sent)
refund / close, order still pending upstream  -> failed(recover) -> read the order -> cancel -> voided (or void_failed)
refund / close AFTER completion               -> job stays fulfilled, error_code refunded_after_success,
        critical audit procurement.direct_refunded_after_success + staff alert; nothing is undone automatically
```

Delivery: nothing secret exists, so the "delivery" is ONE short text `<product>/<plan>----<masked account>----<supplier
order id>` sealed into `card_stock` as `sold` (like a CDK code; `card_fingerprint` of the vendor order, no
`lookup_fp`) and shown by the existing `POST /orders/{ref}/reveal` -> `secrets[0]` / `cards[0].code`. The product's
separator is forced to `----` and `delivery_format` defaults to `Plan----Account----Supplier order`. A refusal that
arrives before the order (preflight, credential) never clears `maybe_sent` of a job that may have sent earlier
(`Lease.prior_send`). Cost: booked once per vendor balance-log row (`gpt_direct*` rows link by the order id, the
ledger of §6 unchanged). The balance breaker reserves every IN-FLIGHT direct order (`kind` any, not fulfilled),
unlike a fulfilled one which was paid at order time.

### 18.4 API the buyer app and the console need

Buyer (`/api/v1`):

| Route | Notes |
|---|---|
| `POST /orders` with an `upstream_direct` product | body adds `session` (string, up to 64 KiB of text; JSON body up to about 70 kB); `qty` must be 1 |
| `GET /orders/{ref}/direct` | NEW. Owner only (404 otherwise and for other modes), `no-store`, 60/min. `{"order_id", "account" (masked e-mail or null), "plan" ("claude/pro"), "state" (job state or null)}` |
| `POST /orders/{ref}/direct-session` | NEW. `{"session"}`; owner, `paid`/`manual_review` direct order without a stored session; 5/h |
| `POST /orders/{ref}/reveal` | unchanged; `secrets[0]` is the delivery text |
| cart `POST /checkout`, chat quotes | refuse a direct product: `direct_buy_now_only` (cart lines also show `unavailable_reason: direct_buy_now_only`), `direct_not_quotable` |

Errors of `POST /orders` (422 unless noted): `session_required`, `direct_qty_one`, `session_too_large`,
`session_too_short`, `session_invalid_format`, `session_not_json`, `session_access_token_only`,
`session_missing_session_token`, `session_missing_access_token`, `session_malformed_access_token`,
`session_expired`, `session_missing_account`, `session_account_mismatch`; 409 `out_of_stock` (no usable source);
503 `direct_unavailable` (no credential public key configured). Offer the UI the same checks before submit; the
server is the truth. The buyer page should say plainly what the session is used for and that it is deleted when
the order ends.

Console (admin, staff):

| Route | Change |
|---|---|
| `POST /admin/products`, `PATCH .../{id}` | `delivery_mode` accepts `upstream_direct` (separator `----` forced) |
| `POST /admin/procurement/sources`, `PATCH .../{id}` | direct `params`: `product` (REQUIRED, `gpt|claude|grok`), `plan`, `card_id` (int), optional `payment_country` + `payment_currency` (GPT only). Errors `bad_params`, `mixed_products`, `cost_required` |
| `GET /admin/procurement/jobs`, `/{id}` | `kind = "direct"`; new fields `account_mask` (masked e-mail), `plan` (`product/plan`), `preflight_expires_at`; `upstream_ref` = vendor order id; never the session. Error codes: `credential_missing`, `credential_unreadable`, `credential_mismatch`, `credential_<reason>`, `plan_already_active`, `quote_error`, `order_declined`, `order_failed_precharge`, `order_cancelled`, `order_not_found`, `order_mismatch`, `plan_mismatch`, `malformed_order`, `malformed_preflight`, `pending_too_long`, `void_failed`, `refunded_after_success` |
| `.../retry`, `/requery`, `/fail`, `/void`, `/switch` | unchanged semantics. `retry` of a held order whose session is gone needs the buyer's `direct-session` first |
| `POST /admin/procurement/jobs/{id}/fulfil` | direct job: ADMIN only (403 otherwise), NO `code` (422 `direct_no_code`), `reason` of 12+ characters REQUIRED (422 `reason_required`); marks the job done after the subscription was done by hand at the supplier; seals the delivery text; critical audit `procurement.direct_manual_fulfil` (who, why, order, supplier reference) |

### 18.5 Settings, env and deploy

New: API `APP_CRED_PUBLIC_KEYS_FILE` (or `APP_CRED_PUBLIC_KEYS`), `APP_CRED_ACTIVE_KID`; procurement
`APP_CRED_PRIVATE_KEYS_FILE`, `APP_CRED_MAX_AGE_S` (18000, max 21600), `APP_CRED_SWEEP_INTERVAL_S` (30),
`APP_DIRECT_PENDING_MAX_S` (7200). `deploy/bootstrap.sh` wires them for a new stack (compose secrets
`cred_public_keys` -> api, `cred_private_keys` -> procurement only; `backup.sh` puts the private key with the vault
keys with their own recipients file, 18.5a). **An already-bootstrapped stack** (no restart of delivery needed):

1. `docker compose run --rm --no-deps --entrypoint python -v "$PWD/secrets:/keys" api -m app.devtools.keygen --out-dir /keys --cred-only`
   (writes `secrets/cred_public_keys.json` 0644 and `secrets/cred_private_keys.json` 0600; prints
   `APP_CRED_ACTIVE_KID=...`; never use `--force` on the vault files). `chown` the private file to the container uid
   and `chmod 0400` it, as for `vault_private_keys.json`.
2. Add to `deploy/env/api.env`: `APP_CRED_PUBLIC_KEYS_FILE=/run/secrets/cred_public_keys` and
   `APP_CRED_ACTIVE_KID=<printed kid>`; to `deploy/env/procurement.env`:
   `APP_CRED_PRIVATE_KEYS_FILE=/run/secrets/cred_private_keys`.
3. In `docker-compose.yml`: top-level secrets `cred_public_keys` / `cred_private_keys` (files above), `api` gets
   `cred_public_keys`, `procurement` gets `cred_private_keys` (and keeps `vault_public_keys`).
4. `alembic upgrade head` (the `migrate` service), then recreate `api` and `procurement`. Add `backup-cred-recipients.txt` for the
   private key's backup archive. Rotation: generate a new pair, keep the old private key in the file until the sweeper has
   emptied the table (max 6 h), switch `APP_CRED_ACTIVE_KID` and the public file, then drop the old key.

### 18.5a Audit and security-review fixes (2026-11-16)

Tests: `backend/tests/test_procurement_direct_audit.py`. Migration `0030_cred_delete_backstop`.

| # | Rule now |
|---|---|
| H1 | The funding `card_id` is never a card bought for a buyer: source save / patch / enable refuse a `card_id` equal to the `upstream_ref` of ANY `vcc` job of that supplier (any state) with 422 `funding_card_invalid`, and the engine re-checks in the lease before `maybe_sent`. Fresh availability reads `GET /cards/{id}` (404 = not ours) and requires ACTIVE and a balance >= the order cost; PAN / CVV in that read are dropped. |
| H2 | A direct job that may already have an order (`prior_send` / `upstream_ref`) is never failed over and never `_unsent` on a "no" (UNAVAILABLE becomes reconcile, ends `needs_manual unresolved`, flagged maybe charged). The reconcile list search pages until the vendor's `total` is reached (or a short page), up to 50 pages; beyond that it answers AMBIGUOUS `order_list_too_long`, never "not found". A same-key resubmit after "not found" is kept (the vendor de-duplicates by key AND `client_request_id`). |
| M1 | The preflight quote is used. USD quote: refused as `quote_over_bound` above `cost_estimate x (1 + APP_DIRECT_QUOTE_TOLERANCE)` (default 10 %; without a configured cost: the catalogue's own USD cost). Non-USD quote (a GPT plan in PHP): compared in its own currency with the plan's `maxAmountMinor` (else `expectedAmountMinor` x (1 + tolerance)); another currency = `quote_currency_mismatch`; no quote for GPT = `quote_missing`. Nothing is sent, the job is not maybe charged. |
| M2 | Fresh availability also reads `GET /gpt-direct/cards/{id}/usage` (`funding_card_exhausted` when the plan's bucket has 0 left; an unreadable shape does not block [I]). `APP_DIRECT_DECLINE_LIMIT` (3) consecutive `order_declined` / `order_failed_precharge` on one source set `last_check_ok = false` and write a critical `procurement.offer_declining` row. `APP_DIRECT_CARD_INFLIGHT_MAX` (1) orders at the vendor per funding card: others wait (`card_busy`, advisory lock + `maybe_sent` in one commit), they do not fail over. |
| M3 | A `completed` order is delivered only when `client_request_id` and `plan` are present and equal and the masked account matches `job.account_mask`; otherwise `needs_manual order_mismatch` + critical `procurement.order_mismatch`, nothing delivered. A later preflight whose masked account differs from the job's is `session_other_account` (nothing sent). |
| M4 | Hand fulfil (admin) first asks the procurement process to READ the supplier order (`POST /internal/direct-order`, fail closed): `pending`/`unknown` = 409 `order_in_flight` (void it first, or wait); a job without a recorded order runs the reconcile search; `completed` / `failed` / `not_found` / `not_sent` may proceed. |
| M5 | A `gpt_direct*` ledger row links only to a job of its own kind (`*cdk*` types to `cdk`, every other to `direct`): CDK ids and order ids overlap. |
| M6 | A direct order whose cost is not in the wallet currency (no USD job cost) is `cost_incomplete: true` in the margin report and has no margin. |
| M7 | `void`: only `cancelled` / `failed_precharge` whose `service_fee_status` is `released` / `refunded` / `none` are "nothing to undo". `declined`, unknown failures and unconfirmed fees end in `needs_manual void_failed` (`order_failure_unconfirmed`); the job view's `upstream_status` shows `status:fee_status[:funding]` (e.g. `declined:held`). |
| M8 | A `needs_manual` direct job with an order id and a "still moving" reason (`pending_too_long`, `unresolved`, `retry_exhausted`, ...) is read every 15 min (up to 96 reads) so a late `completed` is seen: delivered when the order is still `paid` / held by procurement; if the order is no longer open it becomes `fulfilled` with `refunded_after_success` and a critical audit row + alert. |
| Low | `direct-session` must be the same account (409 `session_other_account`); after-sales / admin refund of an order whose direct job COMPLETED needs `acknowledge_irreversible: true` (422 `direct_refund_ack_required`); buy-now of a direct product on a reseller storefront is refused (`direct_not_on_storefront`). |
| S-M1 | (a) The session sweep runs at the START of every engine pass. (b) Migration 0030: `app_api` may DELETE rows older than 6 h (policy `api_expired_delete`; `tenant_isolation` FOR ALL was split into per-command policies because permissive policies are OR-ed); the API reconcile loop runs it (`purge_overdue_sessions`), writes a critical `procurement.sessions_overdue` row and alerts staff (`proc.sessions_overdue`). `APP_CRED_MAX_AGE_S` may not exceed 21600. (c) Checkout answers 503 `direct_unavailable` for an hour after such a purge, and whenever the oldest session is older than 6 h 30 min. |
| S-L1 | In production (`APP_ENV=prod`) a direct source of a `sandbox` supplier cannot be enabled (422 `sandbox_in_prod`), is not routed, is not counted for checkout, and the lease refuses it. |
| S-L2 | The leak sweep dumps every table of every tenant (superuser connection, NULL-tenant rows included), checks base64 / URL-encoded variants of the marker, e-mail and token, negative requests (wrong JSON type, malformed JSON, oversize) and the re-supply path. |
| S-L3 | `deploy/backup.sh` writes the credential private key to its own archive `cred-keys.tgz.age`, sealed to `BACKUP_CRED_RECIPIENTS` (default `/opt/kesiv/backup-cred-recipients.txt`); without that file it falls back to the vault recipients and prints a warning. |
| CI | The opener of buyer sessions lives in `app/procurement/credopen.py` (procurement imports it; `app/core/credvault.py` is seal-only). `tests/test_vault_security.py` asserts that no API code names an opener or imports `credopen`. `scripts/dev.sh` generates and wires the pair for the dev stack. |

| N1 | A hand-pasted card's vendor `card_id` is recorded in `upstream_ref` when the job has none (a pasted card job has no supplier, so its id blocks every supplier's funding cards). Availability returns the funding card's masked number; a funding card whose mask equals the `card_mask` of any non-voided card job of the tenant is refused (`funding_card_invalid`) at the check, in the periodic refresh and in the pre-check. |
| N2 | Three consecutive declines now DISABLE the source (`enabled = false`, `last_check_ok = false`), write the critical `procurement.offer_declining` row and alert staff (`proc.offer_off`, once per switch-off); a precheck / refresh cannot switch it back on, routing and checkout stop; re-enabling is a staff action. |
| N3 | After a direct order completes, the recorded USD cost is compared with the budget it was sent under (`cost_estimate x (1 + tolerance)`): above it is a critical `procurement.cost_over_bound` row and a staff alert (`proc.cost_over`). **Unbounded:** a non-USD plan's real cost (PHP...) is only bounded BEFORE the send by the plan ceiling (`maxAmountMinor`); after completion nothing can be compared (the job has no USD cost, the margin report says `cost_incomplete`). |
| N4 | The per-card in-flight count also counts a `needs_manual` direct job whose supplier order is still watched (M8); a `card_busy` park raises the stuck-job alert after 30 min like the balance park. |
| N5 | `APP_CRED_MAX_AGE_S` defaults to 5 h so the engine's own sweep wins over the database backstop (6 h). `APP_REPLAY_MAX_AGE_S` must not exceed the vendor's idempotency-key retention (unverified). |
| N6 | The refund acknowledgement is required whenever a direct job of the order has a supplier order id and is not `failed` / `voided` (completed OR still moving), not only when `fulfilled`. |
| [V] | Verified against the REAL sandbox (2026-11-16): `GET /gpt-direct/orders` answers `{list: [], total}`; `GET /gpt-direct/account-orders` answers `{list, next_cursor, has_more, sync_until}`; `GET /gpt-direct/cards/{id}/usage?product=gpt` answers `card_id, card_number (masked), card_status, product, light{used, remaining, cap, completed, failed, reserved}, pro20{...}, count_failures, cooling, decline_count, busy`; `GET /gpt-direct/card-rules` answers `{list: [...]}`; the preflight answers `blocking_reason, can_purchase_at, credentialMode, currentPlan, email, last_payment, mailProvider, payment_country, payment_currency, payment_method, preflight_expires_at, preflight_token, promotions, quote_error, quotes (NULL), subscription_active_until, subscription_has_active, subscription_is_delinquent, subscription_purchase_origin, subscription_recovery_required, subscription_will_renew, x_subscription_type`, and without `payment_country` the sandbox answered US / USD (not PH / PHP). Consequences: the preflight is refused BEFORE any send when `blocking_reason` is set (`preflight_blocked`), `can_purchase_at` is in the future (`purchase_not_yet`), the account has an active subscription (`subscription_active`) or needs recovery (`subscription_recovery_required`); a funding card whose usage says `busy` or `cooling` is not used now (`funding_card_busy`, parked, retried) and one whose `card_status` is not ACTIVE is `funding_card_invalid`; a missing quote (`quotes: null`) is accepted on a SANDBOX supplier (the offer's cost bound stays the ceiling) and refused on a PRODUCTION supplier (`quote_missing`; Claude / Grok keep the catalogue-price check). |

New settings: `APP_DIRECT_QUOTE_TOLERANCE` (0.10), `APP_DIRECT_DECLINE_LIMIT` (3), `APP_DIRECT_CARD_INFLIGHT_MAX` (1).
New codes: `funding_card_invalid`, `funding_card_exhausted`, `funding_card_low`, `quote_over_bound`,
`quote_missing`, `quote_unbounded`, `quote_currency_mismatch`, `session_other_account`, `order_failure_unconfirmed`,
`order_in_flight` (409), `direct_refund_ack_required` (422), `direct_not_on_storefront`, `sandbox_in_prod`,
`card_busy` (job `error_code` while parked), `order_list_too_long`. New internal route `POST /internal/direct-order`.
Not done: the hand-fulfil guard refuses an in-flight order instead of cancelling it for staff (void it first).

**Verified against the vendor sandbox with a real order (2026-11-16) [V].** `POST /gpt-direct/orders` WITH a fresh
preflight token answers HTTP 400 `GPT_DIRECT_ORDER_REJECTED` ("preflight token invalid") and creates nothing; the same
request WITHOUT `preflight_token` answers 202 `{id, status: "queued", stage: "card_ready", pricing_version,
client_request_id, account_email, card_id, product, plan, source: "api", ...}`. The adapter therefore omits the token
for the host `sandbox.zovocard.com` only (`direct.omit_preflight_token`); production keeps it. A few seconds later
`GET /gpt-direct/orders/{id}` answers `data.order.{status: "pending", stage: "processing", client_request_id,
account_email, card_number (masked), quoted_amount_minor, final_amount_minor, charged_amount_minor, currency,
payment_state, ...}` and the list rows carry `client_request_id` (the reconcile search relies on it). The sandbox
preflight returns `quotes: null` and defaults to US / USD. The sandbox also reports a different synthetic account
in the preflight (`…@example.test`) and in the order (`…@sandbox.local`), so the "completed order is for the
preflighted account" check is skipped for that host only (`direct.real_sandbox`). A sandbox order completed in about
35 s with `status: completed`, `final_amount_minor: 2000`, `currency: USD`, `service_fee_status: none`.

### 18.6 Left open

* Verified [V] since (18.5a): the list is `{list, total}`, usage and preflight shapes. Still unverified: `client_request_id` per list row and the order **detail** fields (`status`, `plan`,
  `account_email`, `final_amount_minor`) are read as documented/seen in the 202 body; not verified in the real
  sandbox (the vendor's direct orders were not run: the sandbox account lapsed). The reconcile does not depend on
  the list: a miss falls back to the same-key resubmit, which the vendor de-duplicates by key and by
  `client_request_id`. **Confirm both against the first sandbox direct order**, including whether the Idempotency-Key
  is compared with the (changing) preflight token on a replay (`IDEMPOTENCY_CONFLICT` would be FATAL and flagged).
* The funding **card** is a staff-chosen `card_id` per source (checked, see 18.5a); there is no card pool and no
  automatic switch (`no_auto_card_switch: true`). The `usage` and quote shapes (`quotes[plan]`, `remaining`) are read as
  documented, not verified in the sandbox.
* `cancel-renewal` (auto-renewal of the bought subscription) is not called: the vendor cancels once itself on
  success; a `pending`/`warning` renewal status is not surfaced yet. Subscription terms must say that.
* Credits (`credit*`) and renew-binding (`*_renew`) plans are refused; `x` is not an order product.
* Reseller-tenant products: the supplier secrets belong to the platform; a reseller direct product is not designed.
* Frontend: buyer session paste box with the offline checks, masked-account / plan panel
  (`GET /orders/{ref}/direct`), re-supply form, console direct-source form (`product`, `plan`, `card_id`) and the
  job view fields above. 
* The `1/6 h` credential lifetime is a pragmatic bound; a USDT order that is paid after it needs the buyer to
  re-supply (staff see `credential_missing`).


## 19. Cost model and price recommendations (issue #28, owner decision 2026-11-17)

Staff-only view of cost, gross margin and net profit per product / variant and supplier source, plus a **price
recommendation**. Recommendations only: no background job ever changes a price; "apply" is the normal audited
price edit (§1.3: the server recomputes the number, the client names only the unit). Buyers never see any of it
(no public endpoint reads it; a test greps the public product and site payloads). Admin only, like the §7 cost
report. Code: `app/core/costmodel.py` (pure `Decimal` formulas), `app/modules/admin/procurement_pricing.py`
(`/admin/procurement/pricing/*`), console tab **Procurement → Pricing**. No migration: the schedule lives in
`tenants.settings.cost_model` (a section the site-settings editor preserves), every change writes an audit row.

### 19.1 Settings (`GET/PUT /settings`, USD, Decimal strings, rates as fractions)

| key | default | used in the estimate |
|---|---|---|
| `card_open_fee` | 1.50 | yes: `vcc` only, divided by `uses_per_card` |
| `uses_per_card` | standard 3, pro20x 3 | tier = `pro20x` when the offer's `product_code` contains `pro`/`20x` |
| `topup_fee_rate` / `topup_fee_fixed` | 0.02 / 0 | yes: `direct` and `vcc` (card-funded), on the supplier price |
| `service_fee_direct` | gpt 0.30, grok 0.15, claude 0 | yes: `direct` only, by the offer's `product` |
| `failed_fee` | 0.30 | yes, with `expected_failure_rate` (default 0.05): card kinds |
| `failed_free_count` | 3 | informational (the estimate charges every expected failure: conservative) |
| `auth_reversal_rate` | 0.01 | informational |
| `chargeback_tier1_*` | 15 % over 100 tx: +0.30 per tx | informational |
| `chargeback_tier2_*` | 30 %: 200 deposit frozen | informational |
| `target_margin` | 0.30 (net margin on the sale price) | recommendation; per product / variant override via `PUT /target` |
| `round_step_minor` | 10 | recommendation is rounded **up** to a multiple of this (shop-currency minor units) |
| `fx_bands`, `fx_max_age_days`, `fx_max_change` | CNY 3..15, 7 days, 0.2 | FX sanity (see 19.3) |
| `fx` | none | `{CNY: {per_usd, updated_at, updated_by}}`, staff-entered; no rate service yet |

Our own payment-channel fee is not duplicated: it is the gateway fee already configured (`zpay_fee_permille`,
0 for self-hosted USDT) of the entry channel. **Hook for a rate service:** a job would call
`costmodel.set_rate(settings, "CNY", rate, actor="rate-service")` and store the result under the same key; the table
and the report only read `rate_for(settings, currency)`. A missing rate makes every shop-currency figure
`cost_incomplete` (reason `fx_rate`), never a guess. Cost estimates in another currency use that currency's rate.

### 19.2 Formulas (USD; `P` = sale price / rate, `S` = supplier price, `g` = channel fee rate)

```
service  = service_fee_direct[product]                      (direct)
topup    = S * topup_fee_rate + topup_fee_fixed             (direct, vcc)
card     = card_open_fee / uses_per_card[tier]              (vcc: the only source that opens a card per sale)
failure  = expected_failure_rate * failed_fee               (direct, vcc)
channel  = g * P
gross    = P - S                       gross margin = gross / P
net      = gross - service - topup - card - failure - channel      net margin = net / P
C        = S + service + topup + card + failure              (price independent)
P_rec    = C / (1 - g - target)        (None when 1 - g - target <= 0: "target unreachable")
```

`S` is the source's `cost_estimate` (converted at the rate of its currency); a product with no source falls back to
its manual `cost_minor` (shop currency, kind `manual`). A variant uses its own sources, else the product's. Rows are
keyed `(product, variant, offer, region)`; `region` is always `null` until #27 adds regions, so a region dimension
needs no reshaping. The estimate treats the buyer's channel surcharge as pass-through (conservative).

### 19.3 Estimated vs actual

* **Estimate** (`GET /units`): schedule + `cost_estimate`; every component is returned. A supplier price (or manual
  `cost_minor`) that is missing **or zero** is `cost_incomplete` (`supplier_price`): zero is "unknown", never "free".
  Rows lacking a price, a rate or a direct product get no margin or recommendation. `truncated: true` is returned
  instead of silently cutting a page that exceeds 1000 rows.
* **FX safety.** Each currency may have a plausible band (`fx_bands`, default only `CNY` 3..15 units per USD); a rate
  outside it is refused (`fx_out_of_band`, catches the inverted rate). A move of more than `fx_max_change` (20 %)
  against the saved rate needs `fx.confirm` (`fx_change_confirm`). A rate older than `fx_max_age_days` (7) marks the
  rows `fx_stale` and `apply` refuses (409 `fx_stale`); saving the rate again refreshes the stamp.
* **Apply** (`POST /apply`) is a compare-and-set. The request carries the price, the recommendation the staff member
  saw and a fingerprint (hash of the settings, FX rates, target margin, channel rate and source cost behind it). The
  server first locks the tenant settings row and the product / variant row, recomputes, and applies only if all three
  still match, otherwise 409 `recommendation_changed`; the standard audited price edit then runs in the same
  transaction, so a concurrent price edit waits and a stale apply fails: never a silent overwrite. Further refusals:
  `no_recommendation` (incomplete), `offer_required` (several sources, none named), `offer_disabled`, `below_floor`
  (422), `confirm_large_drop` (422: a recommendation more than 50 % below the current price needs
  `confirm_large_drop: true`; the console asks). A recommendation equal to the current price returns
  `{applied: false, noop: true}` and writes nothing. Audit: `pricing.recommendation_applied` (with fingerprint and
  the large-drop confirmation) plus the standard `product.updated` / `variant.updated` row. Orders, quotes and cart
  snapshots keep the price they were created with: only the catalog price moves.
* **Actual** (`GET /actual`, `/actual.csv`): the §7 margin report (ledger, job-cost fallback) converted at the saved
  rate. **Revenue = `amount_minor − fee_minor (channel surcharge) − reseller_markup_minor`**: the surcharge is
  pass-through (it exists to cover the gateway) and the reseller's markup is the reseller's income, so neither is
  platform revenue (`paid` shows the gross amount). The gateway fee is computed on that revenue with today's
  `zpay_fee_permille` (it is not stored per payment, hence `fee_estimated`); the channel comes from the order, else
  its payment row, and an unknown channel gives no fee and `channel_unknown`, never an assumed zpay. Ledger rows
  already hold the real card, top-up and service fees. A row is `cost_incomplete` with `reasons`:
  `ledger_incomplete`, `unreconciled` (a job without ledger row), `after_sales_adjusted` (any after-sales case not
  rejected: a full refund leaves the report, anything else may change revenue or cost), `fx_rate`,
  `channel_unknown`, `no_cost`. The CSV carries the reasons and neutralises formula cells.
* The delivery worker receives only the tenant settings keys the deadline maths reads, never `cost_model`.
* Audit actions: `pricing.settings_updated`, `pricing.target_updated`, `pricing.recommendation_applied`,
  `procurement.margin_exported`.

### 19.4 Left open

* The console help text exists in en and zh-CN; the typecheck of the new Vue panel ran only in CI.
* Whether the "first 3 failures free" applies per card or per account is not modelled (informational).
* Reseller prices (`reseller_prices`) and bundle / cart discounts are not part of the unit estimate.

## 20. Plan × region checkout and the half-price check (issue #27, owner decisions 2026-11-17)

Migrations `0031_variant_options`, `0032_half_price_checks`; tests `backend/tests/test_regions.py`,
`backend/tests/test_half_price.py`, `frontend/apps/web/test/variant-options.test.ts`. Developed for the demo;
production untouched (the production-write gate of §11 applies to every preflight below).

### 20.1 Data model: variants, not a matrix

No new matrix table. A product already has a flat list of variants (v11), each with its own price, floor, stock and
its own supplier sources (`supplier_offers.variant_id`). A variant gets nullable structured attributes
`product_variants.options = {"plan": "<vendor plan key>", "region": "<ISO country>"}` (CHECK: an object with both
keys; partial UNIQUE index on `(tenant, product, plan, region)`; FORCE RLS and `app_api` table grants unchanged).
Admin create / patch validate it (`^[a-z0-9_]{1,40}$`, `^[A-Z]{2}$`, nothing else, 409 `variant_options_taken` for a
duplicate in any status). A source of such a variant carries `plan`, `payment_country` and (direct) `payment_currency`
exactly as before. The buyer product payload adds `variant_options` (true only when EVERY active variant has options:
the UI then shows two selectors, plan chips then region chips with the price; any plain variant falls back to the
v11 chips), per variant `options`, `region_currency` (display label of the region's own charge currency, PHP / EGP /
CLP / JPY / USD; the amount shown stays in the shop currency) and `half_price`.

### 20.2 Regions from the vendor

* The procurement process reads `GET /gpt-direct/plans` -> `payment_regions` (already parsed and cached 60 s with the
  plans catalogue): `POST /internal/regions {tenant_id, product}` (token, rate limited, 502 `supplier_unavailable`
  when the vendor cannot be read). The API never calls the vendor.
* Owner settings (`tenants.settings["procurement_options"]`, admin `GET/PUT /admin/procurement/options`, audited
  `procurement.options_updated`): `region_allowlist` (default PH, EG, CL, JP, US; Grok / Claude are USD-only per the
  vendor docs, so only the regions the vendor lists for them apply) and `half_price_check`.
  `GET /admin/procurement/regions?product=` returns the vendor list with `allowed` per region (`available: false` and
  no rows when the vendor cannot be read).
* A region the vendor stops offering is hidden, never forced: the adapter's availability check (cdk and gpt direct
  offers naming `payment_country`) answers `region_not_offered`, the cached `last_check_ok` goes false and the variant
  reads out of stock. A catalogue that lists no region at all is read as "not stated" and hides nothing.

### 20.3 Generator

`POST /admin/procurement/generate-variants {product_id, supplier_id, vendor_product, plans[], regions[], card_id?,
cost_estimate?}` (platform admin; CDK and direct products only; direct needs the funding `card_id`). For each missing
`(plan, region)`: an ARCHIVED variant named `Plan · RG` with options, price = the product's price as a placeholder
(the schema forbids 0; archived, so never shown), and ONE source (supplier, plan, country, currency) that is
DISABLED. Idempotent (existing combinations are `kept`); regions outside the allowlist or not currently offered by
the vendor are `skipped` (`not_allowed`, `not_offered`); at most 50 variants per product. Prices are never
auto-applied: staff set the price (the #28 recommendation applies as usual), check the source and activate both. The
cost estimate is whatever the staff member types: the vendor's quote is not read here (deviation from the brief:
the unit-cost estimate of §19 already shows it per source). Audit: `variant.generated` per variant.

### 20.4 Half-price check before payment (check-then-order)

Applies when the PRIMARY (priority-first) enabled source of the variant has a half-price plan key
(`plus_half`, Grok `*_half`; `is_half_plan`), for CDK and direct products alike. **Check-then-order**: an order for
such a plan cannot be created without a passed check. `create_order` (the funnel of buy-now, cart and quotes) refuses
with 422 `half_check_required` unless handed a usable check of the same buyer, product and variant (`half_check_id` on
`POST /orders`; 422 `half_check_invalid` when it belongs to another buyer / product, was used, expired (30 min) or has
no usable outcome); the check is locked and consumed (`half_price_checks.order_id`, UNIQUE) in the order's own
transaction. Cart and quote paths pass none, so a half-price plan is buy-now only. `pay_url` re-checks the rule for
orders that exist already (409 `half_check_required` / `half_check_refused`).

* `GET /half-checks/required?product_id&variant_id` -> `{required, method, plan}` (tells the UI whether a session is
  needed); `POST /half-checks {product_id, variant_id?, session?}` -> `{id, status, reason, method, plan, account,
  quote, expires_at, usable}`; 6 per buyer and 30 per IP per 10 minutes; 409 `half_check_not_required`; 422
  `session_*` (same offline validation as checkout); 503 `half_check_unavailable` (procurement unreachable: the pending
  row and its sealed session are deleted at once).
* **The session** is validated and sealed AT ONCE with the procurement public key (`credvault`, AAD = tenant + check
  id) into `half_price_checks.blob`; the API role can INSERT and read every column except `blob` / `kid`. Only the
  procurement process opens it (`POST /internal/half-check`), for the one read, then nulls `blob` in the same
  statement as the outcome (also on every error path); the sweeper wipes any blob older than 15 minutes and drops
  unused checks after 2 days; the API deletes only its own abandoned `pending` rows. A trigger limits `app_api` to
  `order_id` NULL -> value and `manual_pending` -> `manual_approved` / `manual_refused`: it cannot turn a refusal into
  a pass. For a direct product the UI reuses the pasted session in memory for the order itself (sealed again for the
  order as in §18); for a CDK product it is used only for the check. Tested with the strict sweep of every table,
  HTTP body, Redis key / value, pub/sub message and DEBUG log.
* **Methods** (owner setting `half_price_check`, stored on each check):
  * `vendor_preflight` (default): the supplier's `POST /gpt-direct/preflight` with the session and the plan / region.
    Eligible = no `blocking_reason`, no future `can_purchase_at`, no `subscription_recovery_required`, no
    `subscription_has_active`, no `quote_error`, and a quote for the half-price key itself [I: the vendor quotes it only
    to an eligible account; `promotions` is parsed for staff display only]. The vendor's real sandbox never quotes
    (`quotes: null`) [V]: there the quote condition is waived. Ineligible -> status `ineligible` with the reason
    (`subscription_active`, `preflight_blocked`, `purchase_not_yet`, `quote_error`, `plan_not_quoted`, ...), refused
    before payment with a buyer message; a rejected session -> `session_invalid`; a vendor / network failure ->
    `unavailable`.
  * `chatgpt_direct` (the owner's fallback): ONE plain read-only `GET https://chatgpt.com/backend-api/accounts/check/
    v4-2023-04-27` for the buyer's OWN account with the access token of the session just pasted, from the procurement
    process (the only one with egress), ordinary httpx defaults, `Authorization: Bearer`, `Accept`, an honest
    `User-Agent` (`24goods-eligibility-check/1.0`), no browser-fingerprint imitation, no TLS impersonation, no
    sentinel / attestation / risk preparation, no proxy, no retry. 3 reads per buyer per 10 minutes and 60 per hour
    for everyone (shared egress IP), then `unavailable` / `rate_limited`. Eligible = a FREE account that ChatGPT lists
    a `plus` or 50 % campaign for [I]; 401 -> `session_invalid`; 403 / 429 / challenge page / non-JSON -> `unavailable`
    (`chatgpt_refused`, `unreadable_answer`) so staff can switch the method; nothing is stored; the outcome is logged
    as `half-check check=<id> method=... outcome=... reason=...` without the session. GPT sessions only. Tests use a
    stub transport, never the real service.
  * `manual`: nothing is read and no session is stored; the check is `manual_pending` and usable. The order is created
    and paid, then the engine holds the job BEFORE the supplier is called: `needs_manual` `half_price_check_manual`
    (the order moves to `manual_review`). Console (Procurement -> Regions): approve, then Retry the job; refuse
    (`half_price_check_refused`), then refund the order. A refusal before payment makes `pay` fail with
    `half_check_refused`. Audit: `half_check.started`, `half_check.approved`, `half_check.refused`.
* **Second line of defence**: the vendor fails half-price keys BEFORE any charge when the account is not eligible and
  never falls back to full price. At execution the adapter reads the same preflight: for a half-price plan a
  `quote_error` or a missing quote is a definite `FATAL` `half_price_ineligible` (no retry loop, nothing sent,
  `needs_manual`, the order is refunded through the normal refund path), whatever the earlier check or staff approval
  said.

### 20.5 Direct top-ups can be switched off

Per product: an `upstream_direct` product with status `draft` / `archived` is neither listed nor buyable (404), the
CDK products next to it are unaffected, so a CDK-only shop is configuration, not code (#35 option A; tested).

### 20.6 Buyer and console UI

Buyer (mobile first, en + zh-CN, existing tokens): `VariantPicker` (plan chips -> region chips with price, currency
label, "lowest" hint, "unavailable" tag) on the product page and in the buy sheet; for a half-price plan the buy sheet
runs `HalfCheckSheet` after the order-information step: the shop's method decides whether the session sheet
(`DirectSessionSheet`, purpose `check`) is shown, then the result (continue / not eligible with the reason / paste
again / try again later) and only then the order. Console: Products -> variants "Generate plan × region variants"
(`VariantGenerator`), Procurement -> Regions (allowlist, method with a hint per method, manual checks), translated job
reasons for the three new `error_code`s.

### 20.8 Fixes of the 2026-11-17 payment audit and security review

* **Sources are pinned to what was sold (HIGH).** `engine.candidates()` (and so `_failover`, round-robin and
  the admin reassign) keeps only sources whose `plan` / `payment_country` match the variant's `options` and, for an
  order with a check, the check's plan and country; round-robin rotates the matches only. No match left ->
  `needs_manual` (`no_source:...`). The procurement role got `SELECT (options)` on `product_variants` (0031).
  `procurement_sources` rejects create / patch / enable with `source_options_mismatch`.
* **Check binding.** `buy_now` requires the order session's masked account to equal the check's
  (`half_check_account_mismatch`); the mask is the one derived from the sealed session (the vendor's mask no
  longer overwrites it). It is `first letter + domain`, so it is a guard against pasting the wrong account, not a
  proof of identity. One check = one unit (`half_check_qty`). **CDK:** a code carries no account, so for
  `upstream_cdk` the vendor enforces eligibility at redemption only; the check bounds the sale, not the redeemer.
  `claim` also refuses a check whose plan / country differ from the source now sold.
* **Generated variants** start `price_set = false` (archived); activation needs a price sent explicitly and
  `>=` the highest cost estimate of the variant's sources (FX from Costs; no rate = `fx_rate_missing`).
  `cost_estimates {region: amount}` sets per-region estimates.
* **Region withdrawn after payment:** `region_not_offered` is a definite catalog refusal; the held job shows
  `no_source:region_not_offered` to staff. A refused half price tells the buyer (`half_price_refused` message).
* **Manual method:** the buyer confirms (`half_check_confirm`) that staff decide after payment; at most 3 open
  manual checks per buyer; a paid held order raises one `procurement.half_hold` warning and the Regions list
  flags it `overdue` after 60 min.
* **Check endpoint hardening:** registered (non-instant) accounts only (`half_check_register`); per-IP daily cap;
  buyers get coarse reasons only, and account / quote only for a usable check; the `chatgpt_direct` budget
  (60/h) is per tenant and spent only by reads that reach ChatGPT; hitting it raises one `half_check.limit_hit`
  warning ("switch to manual"); `GET /half-checks/required` needs an active product.
* **DB (0032, amended, not deployed):** `app_api` may update `order_id` only, and only of a usable check; the staff
  decision runs through `half_price_checks_decide()` (SECURITY DEFINER, tenant from the session pin,
  `manual_pending` only).
* **Blob exposure window:** a sealed session lives at most until the read finishes; worst case (procurement down
  after the insert) the API deletes its own `pending` row on failure, the sweeper nulls it after 15 min
  (`MAX_AGE`) and deletes unused rows after 2 days.

**Round 2 (payment audit + security re-review of the same head)**

* **No-check half-price bypass (HIGH).** A plain variant (no `options`) or a product with no variants could hold
  a full-price primary and a half-price secondary source; failover or round-robin then reached the half plan
  with no check. Fixed three ways: (a) source create / patch / enable refuse a target whose enabled sources mix
  half and non-half plans, or mix plan / product / country once one is half-price (422 `source_plan_mixed`);
  (b) `requirement()` is "required" when ANY enabled source is half-price (the storefront flag uses the same
  rule); (c) fail-closed in `engine.candidates()`: an order with no attached check never gets a half-price
  source, and with nothing left the job is `needs_manual`.
* **Live options.** The engine reads the variant's live `options`: a change is refused with 409
  `variant_has_open_orders` while an order of the variant is `pending_payment` / `paid` / `manual_review`, and with
  422 `source_options_mismatch` if an enabled source would no longer match.
* **Account binding is keyed.** The `email_masked` comparison (first letter + domain) is only an early UX hint on
  the API. The binding is `account_hmac`: HMAC-SHA256 over `tenant_id` and the canonical identity (ChatGPT
  account id, else the lower-cased full e-mail), computed by PROCUREMENT when it opens the check's sealed
  session, stored on the check row (the API role cannot read it). At execution procurement opens the order's
  sealed session, derives the same identity and compares (constant time) BEFORE any vendor call; a mismatch or a
  missing identity for a session-bearing plan ends the job `needs_manual` with `half_check_account_mismatch` and
  no spend. A `manual` check has no session (staff decide) and so no binding. Claude / Grok sessions carry no
  account, so a non-manual check for them is refused (`half_check_unsupported`).
  **Key:** HKDF-SHA256 of the lexicographically first PRIVATE session key of the procurement credential keyring
  (`APP_CRED_PRIVATE_KEYS_FILE`; procurement only). No new variable. Adding later key ids keeps bindings valid;
  removing the first id invalidates open checks (30 min; a refusal before any spend).
* **Execution-time refusal** now covers EVERY ineligible reason of the vendor preflight (blocked, waiting,
  active / recovering subscription, `quote_error`, no quote), not only `quote_error` / `plan_not_quoted`.
* **CDK orders (bug).** A CDK order has no session, so `claim` skips the account comparison for
  `upstream_cdk`; it used to fail every CDK half-price order with `half_check_account_mismatch`.
* **Price below cost.** The price guard also runs when a variant is CREATED active; the price AND the floor
  (quotes use the floor) must be `>=` the highest cost estimate of its sources; a variant with `options` or with
  sources whose source has no `cost_estimate` cannot be activated (`cost_unknown`); generated variants get
  `floor_price` = the converted cost estimate (rounded up; 0 until an FX rate exists); enabling a source or raising
  its cost under an ACTIVE variant is refused with `price_below_cost` / `cost_unknown` / `fx_rate_missing`.
  **Not included in the guard:** promotions / coupons and payment-channel fees (see the margin report).
* **DB (0032, amended).** A BEFORE INSERT trigger lets `app_api` open only `pending` / `manual_pending` checks
  with no `order_id`, `reason`, `decided_at` or `account_hmac`; setting `order_id` also requires an order of the
  same tenant and BUYER as the check. The triggers are role-named, so the API asserts at startup in production
  that it connects as `app_api`. All three functions use `search_path = pg_catalog, public, pg_temp`.
* **`chatgpt_direct` oracle / DoS.** The same session is read from ChatGPT once per 6 h (Redis key = keyed
  hash of the session cookie, per tenant; the value is the earlier coarse outcome, only `passed` / `ineligible`
  are kept); the method needs an account at least 24 h old or with a paid order (`half_check_account_new`, coarse
  buyer message). Registration: e-mail verification is a per-tenant OPTION (`email_verify_on_register`, default
  off), with a limit of 5 registrations per minute per IP and optional Turnstile, so the age gate is the control
  that makes a mass of throw-away accounts cost time.
* **Catalog path.** `decorate` reads the half-price flag with ONE query per product (`required_flags`), not one
  per variant. `GET /half-checks/required` is rate limited (60/min per user, 120/min per IP).
* `region_offered` fails closed when the vendor's region list is empty and the source pins a country.

* **Round 3 (re-audit of round 2).** (a) The account identity is read ONLY from the `accessToken` claims
  (`https://api.openai.com/auth` `chatgpt_account_id`, else `user_id`, else the profile e-mail), never from the
  buyer-editable `account.id` / `user.email` fields: a forged claim breaks the token signature, so the vendor
  refuses the token before any charge; no claims → no identity → fail closed. The session validator already
  refuses a JSON account id that differs from the token's claim (`session_account_mismatch`). (b) The production
  role assertion (`current_user = app_api`) runs on every entry point, including `python -m app.serve`, which
  passes its own resources. (c) **Accepted residual risk:** a `manual` check carries no account binding (no
  session is collected); for a direct source the gate is the vendor's preflight on the account that actually
  pays, which refuses every ineligible reason before any charge. Staff approval is advisory there. Revisit
  before real money if manual checks are used for direct sources.

### 20.7 Left open

* The e2e smoke scenario was not extended (CI runs it); the typecheck of the new Vue components ran only in CI.
* The half-price promotion key of the account-check document and the vendor's `promotions` shape are inferred [I];
  confirm against a real eligible / ineligible account before production.
* The generator does not read the vendor quote for a cost estimate (staff enter it or use the §19 estimate).


## 21. Card modes for direct orders (issue #29, part 1: the procurement engine)

Owner decision 2026-11-17: let ZovoCard choose cards automatically and issue a new card when none is available,
count the extra cost in the cost model (§19) and watch the decline / chargeback thresholds (15 % / 30 %). Part 1 is
the engine; the console card-pool view, the card-rules editor and the risk monitor are part 2.
Legend: [V] documented by the vendor (`docs/research/zovocard/zovocard-openapi.md` §6.18.3-§6.18.8a), [I] inferred.

### 21.1 Card mode per direct source (`params.card_mode`)

* `fixed` (default, not stored): `card_id` required, exactly as before (`DirectSpec.card_id`, usage / busy / cooling /
  in-flight checks, `no_auto_card_switch: true`).
* `auto_existing`: `card_id` is optional and **never stored or used** (the console drops it). Pre-check (`engine`
  `precheck` mode, `ZovoCardSupplier._auto_card_availability`) asks `GET /gpt-direct/card-pool/schedule`
  (`product`, `plan`, `card_mode=auto_existing`) [V]: `data.pick` first, then `data.candidates[]` with **no**
  `skip_reason`, at most 3 cards in all (the id spelling `card_id` | `id` is [I]). Each card runs through our
  existing funding checks (`_funding_card_problem`: ours, ACTIVE, balance, usage cap, busy / cooling). The usable
  ones go to the job (`procurement_jobs.card_candidates`, `"id:last4,..."`, with `card_candidates_at`). Each
  candidate also returns its masked number to the engine (`Availability.candidate_masks`, `repr=False`), which runs
  `funding_card_clash` AND `funding_mask_clash` on it (as for a fixed card) in the same call that stores the list; a
  clashing card is dropped and never spent on. The masks stay in that call: not stored on the job, never logged,
  never in an audit row. The lease that sends the order takes the first candidate that is not a buyer's card (id
  clash again) and has no order in flight (`direct_card_inflight_max`, the same advisory lock per card, locks taken
  in id order). **Freshness:** a list older than `CARD_FRESH_S` (120 s) is dropped and the job re-runs the read-only
  pre-check (nothing parked, nothing sent), so a card is never spent on without a check inside that bound. The pick
  is written to `procurement_jobs.card_id` / `card_last4` (the card WE SENT: the in-flight identity, never
  overwritten by the vendor) and the engine injects it as `picked_card_id` into the spec (the offer row and the
  pinned `request_hash` never carry it); the wait counter `card_wait_since` is cleared by the assignment.
* `auto_issue` (a NEW card per order): reserved, see §21.4. Validation: `card_mode_unavailable`; any other unknown
  value is `bad_params`. `direct_spec` also refuses both, so a stray database value is never sent.
* The card used is recorded as **id + last four only** (`card_id`, `card_last4`, CHECK `^[0-9]{4}$`); the number
  never reaches a row, a log, an audit row or an API answer (test: PAN sweep). The card the VENDOR says paid
  (`order.card_id` / `card_last_four`, [V] §6.18.8a) is recorded separately as `used_card_id` / `used_last4` (also for
  a fixed job, whose sent card is the offer's); see §21.2 for the checks.

### 21.2 Optional vendor switch (`params.vendor_switch`, default false)

`true` sends `no_auto_card_switch: false` [V]: after a refused payment the vendor retries on another card (default at
most 2 switches, `max_auto_switches`). Consequences, stated in the console hint: it **weakens "one order in flight per
card"** (the vendor, not we, picks the retry card), and every extra failed attempt can cost `failed_fee` once the
card's `failed_free_count` is used up. Platform admins only (the source routes); every change is in the
`procurement.offer_updated` before / after audit, and a flip of the flag writes a warning
`procurement.vendor_switch_changed` row.

**Never trust the vendor's card.** `card_id` stays the card we sent. On EVERY vendor read (submit, query, verify,
watch) the reported card is compared with it and recorded as `used_card_id` / `used_last4`:
* `vendor_switch` off and the card differs: discrepancy, the job goes to `needs_manual` / `card_mismatch`, flagged
  maybe charged, with a warning audit row `procurement.card_discrepancy` (card ids only, never a number);
* `vendor_switch` on: the new card runs `funding_card_clash` (and `funding_mask_clash` when the answer carries a
  number) - a clash is `needs_manual` / `switched_to_buyer_card` with the same warning row;
* a reported id that is not 1-20 digits is rejected the same way (`card_mismatch`), never truncated.
`funding.card_in_flight` counts the sent card and, when it differs, the used card.

**Production suppliers: refused.** Until the vendor can restrict the switch pool to cards we may use, a switched
order could pay with a card bought for a buyer. `vendor_switch` is refused for a supplier with env `production`: 422
`vendor_switch_unavailable` on source create, patch and enable; the adapter sends `no_auto_card_switch: true` for a
production supplier whatever the row says, and the engine ignores the flag there. Allowed on sandbox and the demo mock.

### 21.3 Parked state "waiting for card"

No new job state (the 0023 CHECK is unchanged): a parked job is `queued`, step `submit`, `error_code
waiting_for_card`, with `card_id` / `card_candidates` cleared and `maybe_sent` false. It parks when
* the vendor made no pick, or none of the candidates passed our checks (`no_card_pick`),
* the candidates are busy / cooling / in flight (`card_pool_busy`),
* the order POST is refused with a DOCUMENTED "no card" code AND a lookup proved that no order exists (below).

Codes: `CARD_REFUSAL_CODES` = `CARD_FUNDING_BUSY`, `CARD_IN_USE`, `CARD_CAPACITY_REACHED`, `CARD_CAPACITY_PRO20`,
`CARD_COOLING_DOWN` [V: the vendor's owner-card refusals, §6.18.8a, applied to direct orders by analogy [I]]; only
these are mapped to UNAVAILABLE in `errors.TABLE` (a fixed source keeps today's handling of them: reconcile, then the
same-key resubmit or failover). The three guesses `NO_CARD_AVAILABLE`, `CARD_POOL_EMPTY`, `SUPPLIER_BUSY` [I] are
NOT mapped and never park or re-key: the job goes to `needs_manual` / `card_code_unconfirmed` (maybe sent, warning).

**Re-key after a refusal.** The vendor replays the first answer per idempotency key and treats a changed body under
one key as a conflict, so a parked job that picks another `card_id` cannot reuse its key. After a documented
refusal on an `auto_existing` source the adapter looks the order up by `client_request_id` in the vendor's order list
(`GET /gpt-direct/orders`, `{list, total}` [V, verified against the real sandbox 2026-11-16]; matching the row's
`client_request_id` is done client-side [I]). Only when the whole list has no such order (`no_order_verified`) does
the engine re-key: `card_attempt += 1`, idempotency key `pj:<job hex>:c<N>` and client request id `pj<job hex>a<N>`
(attempt 0 keeps `pj<job hex>`), a warning audit row `procurement.card_rekeyed`, then the park. An order that does
exist is processed as an order; a lookup that fails or is cut short (list too long) leaves the refusal unverified:
`needs_manual` / `card_refusal_unverified`, never a re-key (at most 10 re-keys per job). A crash after the lease
never re-sends under a new key: the job reconciles by lookup and resubmits only under the SAME key. Parks caused by
our own checks (no pick, all candidates busy, stale list) send nothing and keep the key.

The wait is bounded: backoff retries (5 s doubling, capped at 15 min and at the deadline) up to
`APP_DIRECT_CARD_WAIT_MAX_S` (default 1800 s = 30 min) in total from the first park (`card_wait_since`), then
`needs_manual` / `card_wait_exhausted` (the usual `procure.failed` path: order to manual review, buyer message).
One staff warning audit row `procurement.waiting_for_card` (state before / after, reason, max wait) is written at the
first park of a job; later parks write none. No vendor spend while parked: nothing is sent, and a verified refusal created
no order. **No double submit:** a job keeps its key until a refusal is verified as order-less (above); a job with an
order or `maybe_sent` is never parked (it reconciles as before). The park writes a `queued` transition row when it
moves a job out of `submitting`, takes its `before` snapshot first, and grows the backoff (`attempts`).
Buyer: `GET /orders/{ref}/direct` adds `waiting_for_supplier` (true while the job is parked); the order page shows
"Waiting for the supplier" (en / zh-CN). No card, reason or timing leaves the server.

### 21.4 Automatic card issuance: open vendor question

The documented OpenAPI has **no field to issue a new card on a direct order** (`POST /gpt-direct/orders` requires
`card_id`; the CDK owner path says an explicit card is locked, "no auto-open"). We do **not** invent one. Reserved:
a third card mode `auto_issue`, refused by validation with `card_mode_unavailable`. **Question for ZovoCard:** is
there a documented parameter (or a rule such as `card-rules` / the card-pool schedule with `card_mode=auto_issue`)
that makes the vendor open a card for a direct order, and what does it return (card id and last four on the order)?
How it would plug in once answered: `CARD_MODES` gets `auto_issue`; `_auto_card_availability` asks the schedule with
that mode (or the order omits `card_id`); the order read already records `card_id` / `card_last_four`; costs
(card open fee 1.50 USD, 2 % top-up) enter §21.5 as another component; nothing else in the engine changes. Until
then, pre-issue cards by hand and use `auto_existing`.

### 21.5 Cost model

`vendor_switch` sources add `switch_cost = expected_failure_rate x max_auto_switches x failed_fee` per sale
(`core/costmodel.switch_cost`; `Estimate.switch_cost`, included in `fees` / `total_cost` and the price
recommendation; `cost_usd.switch_cost` in the cost rows). It is a **conservative upper bound**: no
`failed_free_count` discount (a card's free failures may be used up), and `max_auto_switches` (default 2 = the
vendor's default, integer 0-10) is a cost-model setting. Together with `failure_cost = expected_failure_rate x
failed_fee` (the first failed attempt) it covers all `1 + max_auto_switches` attempts of an order that keeps failing
(`rate x (1 + max) x failed_fee`): the last failed attempt is counted once. It is an estimate only: it never changes a
price by itself (a recommendation still needs staff to apply it). `auto_existing` without a switch adds nothing (no new
card is opened). The decline / chargeback thresholds are watched in part 2.

### 21.6 Simulator

`app/devtools/mock_zovocard.py`: `GET /gpt-direct/card-pool/schedule` (`pick` + `candidates` with `skip_reason`;
`card_pool`, `schedule_skip`, `schedule_none` = no card available), `GET` / `PUT /gpt-direct/card-rules`,
`POST /gpt-direct/cards/reset-usage`, `direct_no_card_next` / `direct_no_card_code` (refused order POSTs),
`direct_order_over` (e.g. a reported `card_id`), `direct_switch_to` (the vendor switches card when `no_auto_card_switch` is false), `card_last_four` on orders.

### 21.6a Re-audit of the fix round (2026-11-17)

* **M1, "no order" proof.** The vendor order list search (`_direct_find`), which unlocks a re-key, claims
  `order_not_found` only when the answer states `total` and that many DISTINCT order ids were read. A missing
  `total` (an unknown page-size cap), an empty page before `total`, or a row without an id is AMBIGUOUS
  (`order_list_unverifiable` / `malformed_order`): the job goes to staff, never to a new key. Distinct ids keep
  rows that shift between pages from ending the search early. `{list, total}` is [V] (sandbox 2026-11-16);
  matching `client_request_id` on the rows is [I].
* **M2, buyer card recorded between the check and the spend.** `_assign_card` drops a vetted list and re-runs
  the full-mask pre-check when a non-voided buyer card whose mask ends in a candidate's last four was recorded or
  changed after the list was checked (`funding.buyer_card_since`). A false match only costs one more pre-check;
  only cards recorded after the check count, so there is no loop. Candidates without last four rely on the
  pre-check's full-mask check alone.
* **M3, cost floor.** `max_auto_switches` cannot go below the vendor default (2): we send no switch cap, so the
  estimate must not assume fewer switches than the vendor may make.

### 21.7 Left open

* The exact JSON of `card-pool/schedule` (`pick` as an object or an id, the candidate id key) and the "no card" error
  codes marked [I] are unverified: confirm against the sandbox.
* Part 2: card pool view, card-rules editor (`GET/PUT card-rules`, `reset-usage`), decline / chargeback risk monitor.
* The console shows a source's card mode in the list but not yet the card a job used (the job API returns the
  columns' absence; part 2).

