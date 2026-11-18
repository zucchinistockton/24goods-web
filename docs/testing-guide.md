# Testing guide: demo shop, console and the ZovoCard features

How to test the shop, the staff console and the supplier (ZovoCard) features by hand on the demo. Everything on the
demo is test money: payments go through a payment simulator and the supplier is our **built-in ZovoCard simulator**,
so nothing real is bought. **Production (`shop.kesiv.com`) is the real shop: do not test there.**

> **The ZovoCard sandbox is not available (since 2026-11-18) and cannot be used for the time being.** Its token and
> test account expired, the testing period ended and the support-ticket deadline passed. The demo uses the built-in
> simulator instead (§8). Sandbox-specific notes below describe how the demo behaved until 2026-11-18.

## 0. Addresses, accounts and what the demo is connected to

| | Demo (testing) | Production (real money) |
|---|---|---|
| Shop | https://demo.kesiv.com/ | https://shop.kesiv.com/ |
| Console | https://demo.kesiv.com/admin/ | https://shop.kesiv.com/admin/ (allowed IPs only) |
| Redemption page | https://redeem-demo.kesiv.com/ | https://redeem.kesiv.com/ |
| Supplier | built-in simulator (the ZovoCard sandbox expired 2026-11-18, see §8) | ZovoCard production |
| Payments | ZPay / USDT simulators | real ZPay / BEpusdt |

**Demo accounts.** Admin `admin@demo.local`, agent (customer service) `agent@demo.local`, buyer `buyer@demo.local`.
The passwords are on the demo host in `/opt/24goods-demo/deploy/secrets/demo-credentials.txt` (not in git). Use two
browser windows (or one normal and one private window): one signed in to the console, one to the shop.

**Supplier connection of the demo.**
- **Now:** the demo's supplier is our built-in ZovoCard simulator (`sandbox-off.sh` was run on 2026-11-18). The
  supplier row is still labelled `zovocard · Sandbox`; use the simulator values in §8 in your sources.
- Until 2026-11-18 the demo used the real ZovoCard sandbox. That sandbox has expired and is not available.

**Why two redemption pages.** A redemption page forwards the buyer's code to ONE supplier environment.
`redeem.kesiv.com` is production (codes bought on `shop.kesiv.com`); `redeem-demo.kesiv.com` is the demo (simulator
codes bought on `demo.kesiv.com`). A code only works on the page of the environment it was bought in.

## 1. Console overview (sign in as admin)

| Menu | What it is for |
|---|---|
| **Suppliers** (System group) | Supplier connections. On the demo: `zovocard · Sandbox`. **Probe** checks login, IP allowlist, permissions and balance. **Reset breaker** reopens a supplier the system stopped (low balance, repeated errors). Production suppliers need your password to create or switch on. |
| **Products** | Create / edit products. **Delivery mode** decides how a paid order is fulfilled (below). |
| **Procurement** | **Jobs**: one row per supplier purchase (state, detail, staff actions). **Sources**: which supplier, plan and cost a product is bought from. **Costs**: supplier costs and margin. |
| **Orders** / **After-sales** | Orders; refunds and replacements (refund is admin only). |
| **Users** | Buyers and staff, suspension, two-factor reset. |

Delivery modes (chosen when the product is created, cannot be changed later):

| Mode | Meaning |
|---|---|
| Instant | your own imported codes (Inventory) |
| Manual | delivered by staff |
| **Supplier (automatic)** | a ZovoCard CDK code is bought for each paid unit |
| **Supplier (virtual card)** | a virtual card is opened at ZovoCard for each paid unit |
| **Supplier (buyer's account)** | ZovoCard applies the plan directly to the buyer's own ChatGPT / Claude / Grok account |

A supplier product is shown as in stock only while it has at least one enabled, available source.

## 2. Check the supplier first

1. Console → **Suppliers**.
2. On `zovocard · Sandbox` click **Probe**: you should see auth OK, IP allowed, permissions `direct` and `cards`, and a
   balance.
3. If it shows "breaker open", click **Reset breaker**.

## 3. Test A: CDK code product (Supplier (automatic))

**Create the product**

1. **Products → New product**: a title (e.g. "ChatGPT Plus code (test)"), a price (e.g. 150), any category, status
   Active, delivery mode **Supplier (automatic)**. Save.
2. **Procurement → Sources**, pick the product → **Add source**:
   - Supplier: `zovocard (Sandbox)`
   - Supplier plan: `plus` (the sandbox offers `go`, `plus`, `pro_5x`, `pro_20x`, `pro_50x`)
   - Payment country: leave empty
   - Cost estimate: `20` (USD; a source cannot be switched on without one)
   - Enabled: on. Save.
3. Click **Check availability** on the source: it should say available. The product is now in stock in the shop.

**Buy it (shop window, as buyer)**

1. Open the product → **Buy now** → **Place order**.
2. Choose Alipay: you land on the **ZPay Simulator** page; confirm the payment there (**Cancel** aborts).
3. A few seconds later the order is **Delivered**; tap **Reveal codes** to see the CDK code.

**Check in the console.** Procurement → **Jobs**: the order's job is `Fulfilled`; open it to see the supplier
reference, the cost and a timeline of every step. The code itself is stored encrypted ("Stored (sealed)") and is never
shown to staff.

**Redeem it.** Open https://redeem-demo.kesiv.com/, enter the code, paste an account session (see §5, step 2 of
"Buy"), confirm. With the simulator a made-up code is refused ("This code is invalid, used or disabled"); a code bought on
the demo goes through. (The expired sandbox accepted any code and never reported a result.)

**Refund test.** Buyer: order page → **Request after-sales**. Console → **After-sales** → open it → **Refund** (admin;
enter a reference such as `TEST-1`). A code that was not delivered yet is disabled at the supplier automatically.

## 4. Test B: virtual card (Supplier (virtual card))

A draft product "Sandbox virtual card …" exists on the demo: set it to Active in Products, or create your own.

1. **Products → New product**, delivery mode **Supplier (virtual card)**, price e.g. 15.
2. **Procurement → Sources → Add source**:
   - Card product code: `USMAB01` (or `PP5259RC`)
   - Opening amount: `10`
   - Top-up after opening: optional, e.g. `5`
   - Cardholder first / last name: optional, letters only
   - Cost estimate: `11`; Enabled; then **Check availability**.
3. **Buyer**: buy and pay as in Test A. When the order is Delivered, **Reveal codes** shows the card as
   **number / MM/YY / security code**. Below it, **Verification codes for this card** shows a 3-D Secure one-time code
   for 5 minutes when a shop asks for one while paying with the card (the sandbox cannot send real ones, so expect
   "none").
4. **Console**: Procurement → Jobs shows the card **masked** (`525962****1234`) and the supplier card id, never the full
   number.
5. **Refund test**: refund the delivered card order through After-sales. The job goes to `Voided` and the card is
   deleted at the supplier (its balance is returned). Verified against the sandbox.

## 5. Test C: direct subscription (Supplier (buyer's account))

ZovoCard applies the plan to the buyer's own ChatGPT account, using a session text the buyer pastes at checkout. The
session is encrypted the moment it arrives, staff cannot read it, and it is deleted when the order ends. In the sandbox
the subscription is simulated (the account is not really upgraded).

**Set up**

1. A draft product "Sandbox direct Plus …" exists: set it to **Active**. Or create one with delivery mode
   **Supplier (buyer's account)** (one unit per order).
2. **Procurement → Sources → Add source**:
   - Supplier product: **ChatGPT**
   - Supplier plan: `plus`
   - **Funding card id (ours)**: `257662`. This is a sandbox card of ours with 2 Plus uses left (the sandbox allows 3
     per card). For more tests, open another card in the ZovoCard sandbox console and use its id. It must be **our own**
     card, never a card sold to a buyer; the system refuses those.
   - Payment currency: `USD`; Cost estimate: `22`; Enabled; then **Check availability**.

**Buy (as buyer)**

1. **Buy now** → **Place order**. A **Connect your account** sheet opens.
2. In the same browser, sign in to the ChatGPT account, open `https://chatgpt.com/api/auth/session` in a new tab, copy
   **everything** it shows and paste it into the sheet → **Continue**.
3. Pay in the simulator.
4. The order page shows **Your account** (masked e-mail, plan `gpt/plus`, status "Being applied" → **Applied**). The
   order becomes **Delivered** after about 30–60 seconds.

**Console**: Procurement → Jobs shows kind `direct`, the supplier order number, the masked account, the plan and the
cost. **Refund test**: refunding a delivered direct order asks you to confirm that the subscription cannot be taken
back (the refund is recorded only).

The order stops before anything is bought (job "Needs attention" with the reason) when the account already has an
active subscription, the session has expired, or the account is blocked.

## 6. Buyer accounts

- **Register**: shop → My account → Create account. Or check out as a guest ("one-tap account": login ID and password
  are shown once).
- **Two-factor login**: My account → Settings → **Two-factor login** → scan the QR code with an authenticator app → save
  the 10 recovery codes. The next sign-in asks for a 6-digit code. Admins can reset it under Users → Reset 2FA.
- **Orders**: My account → Orders; each order has Reveal, After-sales and Confirm receipt.

## 7. Staff actions on a job (Procurement → Jobs → open a job)

| Action | Use |
|---|---|
| **Re-query** | ask the supplier again what happened (safe, never buys) |
| **Retry** | try again under the same request key (can never buy twice) |
| **Switch** | move to another source; only offered when nothing can have been bought yet |
| **Fulfil by hand** | admin only. Code job: paste a code. Card job: paste `number----MM/YY----CVV`. Direct job: give a reason of 12+ characters after doing it at the supplier yourself. |
| **Void** | disable the code / delete the card / cancel a direct order at the supplier |
| **Fail** | close without delivery, then refund through After-sales |

A yellow warning "the supplier may already have charged" means: re-query first, never buy it from another source.

## 8. Simulator values (the sandbox expired on 2026-11-18)

Done on 2026-11-18 (the sandbox is not available any more); to repeat it, on the demo host:

```bash
/opt/24goods-demo/deploy/sandbox-off.sh
```

Then use these simulator values in the sources (products you created keep working; edit their sources):

| | Sandbox | Simulator |
|---|---|---|
| CDK plans | `go`, `plus`, `pro_5x`, `pro_20x`, `pro_50x` | `plus`, `go` |
| Card product code | `USMAB01`, `PP5259RC` | `MOCK01` |
| Funding card id (direct) | `257662` or another of our sandbox cards | `1` |
| Direct session | a real ChatGPT session text | any session-shaped text |

`redeem-demo.kesiv.com` keeps working and then redeems simulator codes.

## 9. Production (real money)

The same console steps, at `shop.kesiv.com/admin` (allowed IPs only), with the **production** ZovoCard supplier.
Before the first real sale: top up the ZovoCard wallet, set the production webhook callback in the vendor console
(`https://shop.kesiv.com/api/v1/webhooks/zovocard`), choose real cost estimates and prices, and use one of our own
funded cards as the funding card for direct products. The deploy record and open owner tasks are in
`docs/progress.md`.

## 10. How the demo redemption host is wired (for operators)

`redeem-demo.kesiv.com` → Cloudflare (proxied, Full strict) → the demo box's Caddy
(`/etc/caddy/sites/redeem-demo.kesiv.com.conf`, Cloudflare Origin CA wildcard certificate, Cloudflare IPs only) → the
demo stack on `127.0.0.1:18888` (`REDEEM_HOST=redeem-demo.kesiv.com` in `/opt/24goods-demo/deploy/.env`) → the
procurement process's public redemption listener (`APP_REDEEM_SUPPLIER_ENV=sandbox`). `redeem.kesiv.com` →
Cloudflare → c4 (`REDEEM_HOST` in c4's `.env`, `APP_REDEEM_SUPPLIER_ENV=production`).
