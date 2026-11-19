# Owner ops console + 24goods upgrades: requirements, feasibility, plan (2026-11-19, v2)

Owner requests 2026-11-19:
- v1: verify the regional prices; check what the reference projects can add to 24goods (`kesiv-commerce-mvp`,
  Dujiao-Next, `zovo_card_cdk_auto`, `okx/onchainos-skills`, BEpusdt); plan an owner-only admin console.
- v2: the owner's answers, plus two requests: whole-number prices, and pre-orders (pay now, ships in 1–3 days).

Status: **plan**. Built so far:
- the demo price correction and whole prices (§1, PR #74);
- the server-side OKX wallet CLI and login (§6).

Legend: **V** = verified in this session (source named), **I** = inferred / to confirm.

---

## 0. Owner answers (2026-11-19) and what they change

| Topic | Owner answer | Effect on the plan |
|---|---|---|
| Prices | Whole numbers, rounded first | Recommendations round **up** to ¥1 (margin never shrinks). Buyer pages show `¥121`, not `¥121.00`. Demo done; code PR #74 (§1) |
| Pre-orders | Wanted: pay first, ships in about 1–3 days | New upgrade **U10** (§4.1) |
| Licences | `kesiv-commerce-mvp` is the owner's own work (separate GitHub account). The vendor provides `zovo_card_cdk_auto` free to use | Both may be used as sources. Recorded as the owner's statement (the vendor repo still has no licence file; a one-line written permission from the vendor would settle it for good) |
| Domains | No `-` in host names. Dev console at **`opsdev.kesiv.com`** | Applied. `tools.kesiv.com` exists already (§5.1) |
| Repository | Prefer integrating into **`/root/02project/08mywebtools`** over a new repo | Assessed in §5.1: yes, with conditions |
| SOCKS5 | Optional, **off by default**. Credentials in `/root/.secrets/ipcook.env` (V: host, port, user prefix, password, default geo, IP-list URL) | Proxy module is opt-in per job (§5.3) |
| Wallet | `opsdev.kesiv.com` and the shop `kesiv.com` **share one OKX Agentic Wallet** ("Wallet 1"). The server must be able to make payments | CLI installed and login started on this server (§6) |
| 3-D Secure | Not now; future plans | Removed from scope; listed under "later" |
| Timeline | No fixed timeline | Effort columns removed; phases are an order, not dates |
| Address generator | No invented "tax-free state" addresses (OpenAI policy). Keep the kesiv **autofill capability** as an optional, off-by-default feature for learning, review and attack testing in dev | Autofill kept as an opt-in dev tool; it fills real card details only (§5.3, §7) |
| Own protocol | Not finished; hold | Adapter contract stays defined, implementation later |

---

## 1. Prices (verified; demo corrected; whole numbers)

| Plan | Cheapest region | Official web price | Net of VAT | CNY cost | Demo price |
|---|---|---|---|---|---|
| Go | Egypt | EGP 220 | EGP 192.98 | 23.97 | **¥30** |
| Plus | Philippines | PHP 1,100 | PHP 982.14 | 103.05 | **¥121** |
| Pro 100 ("Pro 5x") | Chile | CLP 102,990 | CLP 86,364.78 | 580.78 | **¥666** |
| Pro 200 ("Pro 20x") | Philippines | PHP 9,990 | PHP 8,919.64 | 935.87 | **¥1,072** |
| Pro 500 ("Pro 50x") | Chile | CLP 509,990 | CLP 428,563.03 | 2,881.96 | **¥3,295** |

- **V:** the owner's five figures are the official **web** price minus local VAT (PH 12 %, EG 14 %, CL 19 %,
  JP 10 %, US none). That is hvoyai.com's "pre-tax price" (verified 2026-11-19). Each one is the cheapest region of
  its plan.
- **Demo (2026-11-19):** all 30 direct sources (5 plans × PH/EG/CL/JP/US, plus Plus half price) cost the net web
  price at the day's FX (open.er-api.com, CNY 6.680778 per USD).
  - Price = (cost + supplier fee USD 0.30 + 2 % top-up fee + failure allowance) × 1.12, **rounded up to a whole
    yuan** (`round_step_minor` 100).
  - Floor = that cost without the markup, also rounded up to a whole yuan.
  - **V (database):** 0 prices and 0 floors with decimals; no floor above its price.
  - Pre-change dump: `/root/demo-backups/demo-20261119T033343Z.dump`.
- **Code (PR #74):** the default rounding step is now ¥1. Buyer pages drop `.00` on whole amounts but keep real
  cents (`¥61.50` after a discount, fees such as `¥0.30`), so nothing is ever rounded on display. The staff
  console keeps two decimals for bookkeeping.
- **Open (D6):** the 12 % is added **after** the supplier's fees, because the fees are real costs. On the official
  price alone, Plus PH would be ¥116.
- The demo still names the tiers "Pro 5x / 20x / 50x"; OpenAI now says Pro 100 / 200 / 500. Rename before
  production.
- Production prices are untouched. In production the cost comes from the supplier's live quote, with this table as
  the cross-check (U1).

---

## 2. Requirements, organised

### A. Shop (24goods)
| # | Requirement | Where |
|---|---|---|
| A1 | Right regional prices: net web price, cheapest region, 12 % on ChatGPT, whole numbers | §1 done on demo; U1 |
| A2 | Use the good parts of the reference projects | §3, §4 |
| A3 | Receive buyer payments into the shared OKX wallet | U2 |
| A4 | Pay suppliers from the same wallet (server-side) | §6, console step 6 |
| A5 | Know that goods were received: buyer's plan really active; supplier really credited | U5, §6 |
| A6 | CDK / card platform upgrades, incl. upgrade plans (Plus → Pro) | U3, U4, U7 |
| A7 | `card.zzshu.pro` as a second supplier, with failover | U3 |
| A8 | Buyer's first order to the owner for a manual top-up | U6 |
| A9 | **Pre-orders**: pay now, ships within a stated window (e.g. 1–3 days) | **U10** |

### B. Owner console (in `08mywebtools`, dev at `opsdev.kesiv.com`)
| # | Requirement |
|---|---|
| B-a | Paste a customer session: account, plan, expiry; check an order's status |
| B-b | Top up through Zovocard or `card.zzshu.pro`, or (later) my own card through my own protocol |
| B-c | Several charges for one large amount; see fees and losses per job, card and region |
| B-d | Cards from other regions/issuers. SOCKS5 optional (ipcook), off by default. Optional dev-only checkout autofill |
| B-e | Shared OKX wallet: balances, history, supplier payments from the server |
| B-f | One button: cheapest region now, cost, profit, suggested whole-number price |
| B-g | Owner-only. Dev now (`opsdev.kesiv.com`, Cloudflare token), production later |

---

## 3. Reference projects

| Project | Licence | What we use |
|---|---|---|
| `kesiv-commerce-mvp` (owner's) | owner's own work | Docs, API notes and logic may be reused freely: the zzshu API, Zovocard limits (50 USDT minimum deposit, 20 USDT reserve, decline fees), treasury guard rails, autofill selectors |
| `zovocard/zovo_card_cdk_auto` | no licence file; **owner: provided by the vendor, free to use** | Feature checklist and code as needed: batch redemption, after-sales lookup, subscription/billing lookup, captcha step, subscription recovery, upgrade plans |
| `dujiao-next/dujiao-next` | GPL-3.0 (V) | Ideas only (copying code would put our code under GPL). Worth adopting: the admin route-coverage test and the import-rule architecture tests |
| `okx/onchainos-skills` | MIT (V) | The `onchainos` CLI, installed on this server (§6) |
| `v03413/bepusdt` | GPL-3.0 (V) | Already runs in 24goods as an unmodified pinned container; only its receive addresses change (U2). Supports X Layer (V, README) |

**Assessment (unchanged):** keep 24goods and add what is missing. It already covers most of the Dujiao-Next +
cdk-station design, with a stronger security model.

---

## 4. Shop upgrades (in priority order; each one is a PR to the demo first, production on the owner's go-ahead)

| ID | Upgrade | Review |
|---|---|---|
| **U1** | **Price reference table**: plan × region → web price, VAT, source, date, plus daily FX. Shown in the pricing panel beside the supplier quote and our price. Alerts when a cost goes above its price or a reference is more than 7 days old | — |
| **U10** | **Pre-orders** (§4.1) | security-reviewer (order states, refunds) |
| **U2** | **Receive into the shared OKX wallet**: BEpusdt receive addresses = the wallet's TRON (USDT-TRC20), BSC and X Layer addresses. Changing an address needs 2FA re-auth, an audit row and a Telegram alert | production go-ahead; a 1 USDT live test per chain |
| **U3** | **`card.zzshu.pro` as a second supplier**: `direct` with card + full session, status polling. **Failover:** when Zovocard's direct channel fails but the funded virtual card still holds the money, retry on zzshu with the same card. The card number is read inside procurement and never stored | security-reviewer |
| **U4** | **Upgrade products** (Plus → Pro, to Pro 200, Pro 200 renew), priced from the supplier's upgrade quote | — |
| **U5** | **"Really received" check**: after delivery, re-run the account pre-check. Show "Verified active" to buyer and support; a mismatch opens a support case | — |
| **U6** | **First-order manual review** (setting, off by default) + **ops API** for the console: scoped service token; manual queue, order read, reveal kept session with reason, mark fulfilled/failed; every call audited | security-reviewer; migration |
| **U7** | Redemption site, from the vendor checklist: lookup by code or e-mail, batch redemption, subscription/billing lookup | i18n review |
| **U8** | Route-coverage test + module import rules | — |
| U9 | (later) OKX a2a-pay / x402 for Agent-API buyers | payment-auditor |

### 4.1 U10 Pre-orders: design

**What exists (V, code):**
- manual-delivery products;
- `products.delivery_eta_hours` with a site default;
- the staff "overdue manual orders" filter;
- the delivery deadlines and after-sales timers.

Pre-orders extend these. They are not a new order type.

| Part | Design |
|---|---|
| Product setting | "Pre-order" on a product (or variant): delivery window **min–max days** (default 1–3), optional **quota** (most units sold ahead), optional **end date**, optional note ("made to order"). Works for manual products and card-key products. Not offered on direct top-ups, which complete in minutes |
| Buyer, before paying | Badge "Pre-order · ships in 1–3 days" on the card, product page and checkout. A required tick: "I understand this is delivered within 1–3 days after payment". Price and payment are as usual (full payment) |
| Buyer, after paying | Order shows **"Pre-order: expected between <date> and <date>"**, with the step "Being prepared", then delivery as usual. Live updates use the existing stream |
| Card-key products | May sell beyond stock up to the quota. **When new cards are imported, waiting pre-orders are delivered first, oldest paid first, automatically**, through the normal vault path (no plaintext anywhere new) |
| Manual products | Staff deliver as now. The staff queue gets a **"Pre-orders" tab sorted by due time** (`paid_at + max days`) |
| Late orders | Staff alert (console + Telegram) at 75 % of the window and when overdue. Once overdue, the buyer sees an apology and a **"Cancel and refund"** button (full refund through the existing cancel/refund path). An optional automatic refund after a grace period is a setting, off by default |
| Records | Every state change audited (order events + audit rows). Tenant-scoped like all order data; reseller stores inherit the setting. Migration: `products.preorder` (JSON: window, quota, end date, note) and `orders.preorder_due_at` |
| Not now | Deposits / pay-the-rest-later, per-variant windows (can follow) |

---

## 5. Owner console

### 5.1 Integrate into `08mywebtools`? Findings and recommendation

**Findings (V, 2026-11-19):**
- `08mywebtools` is a **public** GitHub repo (`stackoracode/mywebtools`). This machine's GitHub login
  (`zucchinistockton`) can read it but **cannot push**.
- It is a personal media toolbox for the LAN (Windows PC + ARM board): FastAPI single process, vanilla-JS SPA,
  JSON-file storage, in-memory job queue with live progress (SSE), a tool registry, and an **optional**
  password.
- **`tools.kesiv.com` serves a different app:** "VPS Console" (`arifulcrypto/vps-console`, service
  `vps-console`, port 8091 on this server). It is not `mywebtools`.

**Recommendation: yes, integrate, under four conditions.**
1. **Make the repo private before the first ops commit**, or keep the ops package in a private repo that
   `mywebtools` loads. A public repo would publish the supplier integrations, wallet tooling and how the business
   works. Trade-off: the README notes that GitHub's free arm64 runners need a public repo, so arm64 tests would move
   to the board or be dropped.
2. **One codebase, two deployments.** On the LAN: media tools on, ops off. On this server at
   **`opsdev.kesiv.com`**: ops on, media off, chosen by a profile setting. Shared: UI shell, i18n, job queue,
   live progress, settings, presets. `mywebtools`'s job-and-progress model fits top-up jobs well.
3. **The ops profile raises security:**
   - login is **mandatory**, with password + TOTP;
   - TOTP re-check before money actions;
   - SQLite for ops data (the JSON job file is not enough);
   - sessions, cards and proxy credentials stored sealed;
   - an append-only audit log;
   - the origin accepts Cloudflare only (like the existing `tools.kesiv.com` site file).
4. **Push access:** add `zucchinistockton` as a collaborator on `stackoracode/mywebtools`, or tell me which
   account to push with.

`tools.kesiv.com` stays on VPS Console unless the owner wants it repointed (D9).

### 5.2 Deployment (dev)
- DNS `opsdev.kesiv.com` → this server, proxied, created with the owner's Cloudflare token.
- Host Caddy site (Cloudflare-only origin), `systemctl restart caddy` (Caddy runs with `admin off`).
- systemd service under its own user. It calls the wallet CLI as user `onchainos` through a narrow sudo rule
  (§6).

### 5.3 Modules
| Module | What it does |
|---|---|
| **Session desk** | Paste a session (same safe paste as the shop): e-mail, account id, plan, expiry, validity. Account check through Zovocard preflight or zzshu `subscription-lookup`. Stored **sealed**, shown masked, deleted 72 h after the job closes, never logged |
| **Jobs** | One job = one customer upgrade (plan, region, amount, linked 24goods order or manual). Timeline with seconds; every attempt recorded (channel, card, proxy, amount, fees, result). **Several charges** per job until the target is met |
| **Channels** (contract `check` / `quote` / `submit` / `status` / `cancel`) | Zovocard direct and virtual-card · zzshu direct · Manual (record the outcome) · **My protocol: on hold** until the owner's protocol is ready (signed JSON job + signed callbacks) |
| **Cards** | Issuer-held (Zovocard card id; details fetched at use, never stored) · manual (number and expiry sealed; **CVV never stored**) · protocol-held (later). Per card: region, issuer, balance, fees, success rate, cost per successful upgrade |
| **Proxies (optional, off by default)** | ipcook SOCKS5 from `/root/.secrets/ipcook.env`: per-job checkbox, unticked by default. Region from the job, sticky exit for the whole job, exit-IP check. Session queries normally go direct |
| **Checkout autofill (optional, off by default, dev only)** | From the kesiv autofill script: a local Playwright helper fills a checkout form with a card's **real** stored details, for learning, review and attack testing in dev. No address generator; billing comes from the card record. Never submits unless explicitly asked |
| **Pricing button** | Reference table (U1) + supplier quotes + FX → each region sorted by total cost, suggested whole-yuan price at the chosen markup, profit per sale, current 24goods price. Optional push through the audited pricing action |
| **Ledger** | Per job: revenue, costs (local-currency charge at real FX, supplier and card fees, declines, proxy), profit. Per day/card/region/channel, CSV export |
| **Wallet** | §6 |
| **Audit** | Append-only log of every action |

### 5.4 Order of work (no dates)
1. Repo conditions (§5.1), skeleton of the ops profile: login + TOTP, audit, sealing, DNS `opsdev.kesiv.com`.
2. Session desk + account check.
3. Jobs + Zovocard, zzshu and Manual channels, with several charges per job.
4. Pricing button + ledger.
5. Cards + optional proxies.
6. Wallet page: balances, history, supplier payment with confirmation and credit tracking.
7. 24goods link (needs U6): manual queue, kept session, mark fulfilled.
8. Optional dev autofill helper.
9. Own protocol adapter (when ready); 3-D Secure (later); production deployment.

---

## 6. Shared OKX wallet (server side)

- **Installed (V, 2026-11-19):**
  - `onchainos` 4.6.11 from OKX's GitHub release, the static Linux build, SHA-256 checked against the release's
    `checksums.txt`, at `/usr/local/bin/onchainos`.
  - It runs as its own system user `onchainos` (home `/var/lib/onchainos`, mode 700). The wallet session lives
    there, not under root or any app user.
  - Only the CLI was installed. OKX's full installer also installs AI-agent skills and a background "A2A" daemon,
    which the server does not need.
- **Login:** started 2026-11-19 with OKX's browser login (the owner signs in with e-mail on OKX's page; the e-mail
  is never sent from this server). Result recorded in `docs/progress.md`.
- **One wallet, two uses:**
  - **Receive:** the shop's BEpusdt (production on c4) watches the wallet's **public** addresses and needs no login
    there (U2).
  - **Pay:** only this server holds the login and can send.
- **Guard rails (owner action on OKX's web portal, right after login):**
  - set the wallet **Policy**: whitelist = supplier deposit addresses only (e.g. Zovocard's), plus per-send and daily
    limits;
  - OKX enforces these, so a compromised server cannot send elsewhere or more;
  - large or new-address sends trigger OKX's e-mail approval code.
- **Payment flow:** the console proposes a supplier top-up (Zovocard minimum 50 USDT; trigger on
  `spendable_balance`).
  1. The owner re-authenticates (TOTP) and confirms.
  2. The console runs `onchainos wallet send` first **without** `--force` and shows OKX's confirmation text.
  3. Only after the owner confirms does it re-run with `--force`.
  4. It records the tx hash, matches it with Zovocard `balance-logs`, and marks the payment **credited**.
  Automatic top-ups come later, inside the Policy limits, if wanted.
- **Option:** a second account under the same login (`wallet add`) to separate "receive" from "spend". Still one
  wallet, but a server compromise could then reach only the spend balance.

---

## 7. Risks and limits

- Customer sessions are full account access: sealed, masked, 72 h, reveal with reason (audited), owner-only
  behind 2FA.
- Card data: no CVV stored ever; issuer-held cards preferred (PCI-DSS).
- **No invented billing addresses.** The optional dev autofill uses real card details only.
- **OpenAI's rules:** the owner's instruction is not to break them. Buying a plan through another country's
  checkout or through proxies can conflict with them, and the account can be flagged. The optional proxy feature
  is off by default.
- 3-D Secure and captchas: not automated; future plans.
- Money-moving code (wallet sends, U2, U3) gets the security-reviewer / payment-auditor pass. Production changes
  need the owner's go-ahead in the same turn.
- Dev box has 2 GB: tests and builds run in GitHub CI; any browser tooling runs on the owner's PC.
- **The wallet login on this server is a live spending credential.** Set the OKX Policy before any funds are sent.
  This server also runs the demo, so a breach of the demo host would expose the wallet session (contained by the
  Policy).

---

## 8. Decisions still needed

| # | Decision | Recommendation |
|---|---|---|
| D4 | Own protocol | on hold (owner) |
| D6 | 12 % after supplier fees (current) or on the official price only | after fees |
| D9 | `tools.kesiv.com`: keep VPS Console there, or repoint it to mywebtools | keep; ops on `opsdev.kesiv.com` |
| D10 | Make `stackoracode/mywebtools` private (or a private ops package), and push access for `zucchinistockton` | private + collaborator |
| D11 | Pre-orders (U10): build now on the demo? | yes, after PR #74 |
| D12 | OKX Policy: whitelist + limits (owner, on OKX's portal) | before the first send |
| D8 | Production go-ahead for #62–#74, migration `0036`, Redis ACL line (#47), production prices | open |
