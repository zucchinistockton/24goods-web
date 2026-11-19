# Owner ops console + 24goods upgrades: requirements, feasibility, plan (2026-11-19)

Owner request 2026-11-19: verify the regional prices, check what the reference projects
(`kesiv-commerce-mvp`, Dujiao-Next, `zovo_card_cdk_auto`, `okx/onchainos-skills`, BEpusdt) can add to 24goods,
and plan a **separate owner-only admin console** (new repository, dev environment first, own DNS name).
Status: **plan only.** Nothing in this document is built yet, except the demo price correction (§1).

Legend: **V** = verified in this session (source named), **I** = inferred / to confirm.

---

## 1. Prices: verified and corrected on the demo

| Plan | Cheapest region | Official web price | Net of VAT | USD (FX 19 Nov) | CNY | Demo price (CNY) |
|---|---|---|---|---|---|---|
| Go | Egypt | EGP 220 | EGP 192.98 | 3.59 | 23.97 | 29.80 |
| Plus | Philippines | PHP 1,100 | PHP 982.14 | 15.42 | 103.05 | 120.10 |
| Pro 100 ("Pro 5x") | Chile | CLP 102,990 | CLP 86,364.78 (owner) | 86.93 | 580.78 | 665.80 |
| Pro 200 ("Pro 20x") | Philippines | PHP 9,990 | PHP 8,919.64 | 140.08 | 935.87 | 1,071.40 |
| Pro 500 ("Pro 50x") | Chile | CLP 509,990 | CLP 428,563.03 | 431.38 | 2,881.96 | 3,294.30 |

- **V:** all five owner figures are correct. Each one is the official **web** checkout price minus local VAT
  (PH 12 %, EG 14 %, CL 19 %, JP 10 %, US none). That is the method of hvoyai.com ("pre-tax price", verified
  2026-11-19), where the owner's numbers come from. Small CNY differences are FX (CLP 993.47 per USD today).
- **What was wrong:** the demo costs (18 Nov) used **App Store prices with VAT included** (geosub.org lists App
  Store only), and Pro 500 was USD 500 everywhere. Costs were 12–19 % too high, and Go Egypt used the App Store
  EGP 249.99 when the web price is EGP 220. The selling prices were too high, never below cost.
- **Fixed on the demo (2026-11-19, audited console actions, pre-change dump `demo-20261119T033343Z.dump`):** all
  30 direct sources (5 plans × PH/EG/CL/JP/US, plus Plus half price) now cost the net web price at today's FX.
  Price = (cost + supplier service fee USD 0.30 + 2 % top-up fee + failure allowance) × 1.12, and floor = cost
  with fees and without the markup. Every price is above its floor and every floor covers the cost (checked in the
  database).
- **Owner choice (D6):** the 12 % is added **after** the supplier's fees, because the fees are real costs. Plus PH
  is ¥120.10 this way. If the markup goes on the official price only, it would be ¥115.41, and on Go the USD 0.30
  fee would take about 8 points of the 12 % margin.
- **Naming:** OpenAI now calls the tiers Pro 100 / Pro 200 / Pro 500 (GeoSub says "5x" URLs are historical). The
  demo products still say Pro 5x / 20x / 50x. Rename when they go to production.
- Production prices are untouched. Production should take cost from the supplier's live quote
  (`gpt-direct/plans`), with this table as the cross-check (§4, U1).

---

## 2. The owner's requirements, organised

### A. Existing shop (24goods)
| # | Requirement | Where it lands |
|---|---|---|
| A1 | Regional prices right: cheapest region, net web price, 12 % on ChatGPT | done on demo (§1); U1 |
| A2 | Take good ideas from the reference projects (storefront, CDK hub, card platform, payment) | §3, §4 |
| A3 | Receive buyer payments into the OKX wallet ("Wallet 1") | U2 |
| A4 | Pay suppliers from the OKX wallet | console B6 (manual-confirm first) |
| A5 | Track that goods were received (buyer's plan really active; supplier really credited) | U5, B6 |
| A6 | CDK / card platform upgrades ("card PIN upgrades" = upgrade plans such as Plus → Pro) | U3, U4 |
| A7 | Second supplier `card.zzshu.pro` and switching to it when the main channel fails | U3 |
| A8 | First order of a buyer goes to the owner for a manual top-up | U6 |

### B. New owner-only console (separate repo, dev first)
| # | Requirement |
|---|---|
| B-a | Paste a customer session; see account, plan, expiry; check order status |
| B-b | Top up through: Zovocard API, `card.zzshu.pro` API, or **my own card** through **my own protocol** |
| B-c | Several charges for one large amount; see the "wear" (fees, declines, FX spread) per job and per card |
| B-d | Use cards from other regions/issuers; SOCKS5 proxy per job; browser with the session injected for manual checkout |
| B-e | OKX wallet balances and history in the console |
| B-f | One button: cheapest region now, cost, profit, suggested selling price |
| B-g | Owner-only, dev environment now (Cloudflare DNS with the existing token), production later |

---

## 3. Reference projects: what they are and what we may take

| Project | Licence (V, GitHub API) | What it is | What we take |
|---|---|---|---|
| `dujiao-next/dujiao-next` | **GPL-3.0** | Go/Gin/Vue card shop; Casbin RBAC, TOTP, coupons, affiliate, BEpusdt/EPay/Stripe adapters, Telegram | **Ideas only** (GPL; copying code would force GPL on our code). 24goods already has promo, resellers, 2FA, Telegram alerts, BEpusdt, EPay. Worth adopting: their **route-coverage test** (every admin route must be in a role) and **import-rule architecture tests**. |
| `zovocard/zovo_card_cdk_auto` | **none = all rights reserved** | The supplier's CDK redemption hub (Go + Vue) | **Feature checklist only, no code** (ADR-044). Gaps vs our redemption site: batch redemption, after-sales lookup, subscription/billing lookup, captcha step, subscription recovery. |
| `okx/onchainos-skills` | **MIT** | `onchainos` CLI + MCP: Agentic Wallet (keys in OKX's TEE), balances, send, history, Policy (limits/whitelist set on OKX's portal), a2a-pay / x402 payments | Use the **CLI as a tool** (no code copy needed). Wallet balance/history, guarded sends, a2a-pay later. |
| `v03413/bepusdt` | **GPL-3.0** | Self-hosted USDT/USDC gateway, watches addresses on TRON, BSC, Ethereum, Polygon, **X Layer**, Solana, Arbitrum, Base… | Already in 24goods as an unmodified container (pinned digest). Only its **receive addresses** change (U2). |
| `kesiv-commerce-mvp` (owner's local repo) | private | Design docs (Dujiao-Next + cdk-station + BEpusdt + OKX), `cdk-station` (a modified copy of `zovo_card_cdk_auto`) with zzshu failover, OKX treasury, checkout autofill | **Docs and API notes**: the zzshu API, Zovocard limits (50 USDT minimum deposit, 20 USDT reserve, decline fees), the OKX treasury guard rails. **No code from `cdk-station`**: it derives from the unlicensed vendor repo. Keep that repo private. |

**Assessment:** 24goods already covers most of what the Dujiao-Next + cdk-station design proposes, and its security
model is stronger: sealed sessions, a separate procurement process, row-level security, audited state changes. Switching
to Dujiao-Next would mean a GPL dependency and losing that work. So the plan is **to keep 24goods and add the
missing pieces** (§4). The console gets the owner-only tools that do not belong in a public shop (§5).

---

## 4. Part A: upgrades to 24goods (priority order)

| ID | Upgrade | Value | Effort (I) | Risk / review |
|---|---|---|---|---|
| **U1** | **Price reference table**: per plan × region, store the official web price, VAT, source and date, plus daily FX. The console's pricing panel shows supplier quote vs reference vs our price. Alert when the cost goes above the price or the reference is more than 7 days old. | prevents selling below cost | 1.5 d | low |
| **U2** | **Receive into OKX Wallet 1**: set BEpusdt receive addresses to Wallet 1 addresses on TRON (USDT-TRC20), BSC and **X Layer** (OKX's chain, gas-free inside the OKX wallet). Guard: changing an address needs 2FA re-auth, an audit row and a Telegram alert, and the console shows the address with a checksum. | owner's own wallet | 0.5 d code + config | **production change → go-ahead; 1 USDT live test per chain** |
| **U3** | **`card.zzshu.pro` as a second supplier** (procurement adapter): `direct` order with a card + full session, status polling, Stripe 3-D Secure hand-off to staff. **Failover:** when Zovocard's direct channel fails but the funded virtual card still holds the money, retry on zzshu with the same card (zzshu charges only on success, per its API docs as quoted in `kesiv-commerce-mvp` doc 04; I: confirm live). The card number is read from Zovocard inside procurement and never stored. | fewer failed orders, no stranded card balance | 4 d | **security-reviewer** (card data in procurement) |
| **U4** | **Upgrade products**: Plus → Pro, to Pro 200, Pro 200 renew (zzshu plan types `plus_to_5x`, `to_20x`, `renew_20x`; zovo equivalents), priced from the supplier's upgrade quote. | new sales from existing subscribers | 2 d | low |
| **U5** | **"Really received" checks**: after delivery, re-check the account through the same pre-check (plan now active). Show "Verified active" to buyer and support; a mismatch opens a support case automatically. The supplier-credit side is in console B6. | fewer disputes | 1.5 d | low |
| **U6** | **First-order manual review** (setting, off by default): a buyer's first direct order goes to the manual queue with reason `first_order_review`, and its session is kept for staff (#65). Plus an **ops API** for the console: a scoped service token (list manual queue, read order, reveal kept session with reason, mark fulfilled/failed with evidence), each call audited. | the owner's first-order workflow | 3 d | **security-reviewer**; migration |
| **U7** | Redemption-site gaps from the vendor checklist: lookup by code or e-mail (status, history), batch redemption, subscription/billing lookup. | parity with the vendor hub | 3 d | i18n review |
| **U8** | Route-coverage test (every admin route in a role) and module import rules (ideas from Dujiao-Next). | regressions caught by CI | 1 d | none |
| U9 | (later) OKX a2a-pay / x402 for Agent-API buyers. | agents can pay | 3 d | payment-auditor |

Each of these is a separate PR on the demo first. Production only gets them with the owner's go-ahead.

---

## 5. Part B: the owner's console (new repository)

### 5.1 Shape
- **Repo:** private `kesiv-ops` (name D1). Owned code from 24goods may be reused: the Zovocard client, session
  parser, cost model, FX and sealing patterns. No code from `cdk-station` or the vendor repo.
- **Stack (recommended):** Python 3.12 + FastAPI, Vue 3 + Element Plus (same as the 24goods console), SQLite
  (WAL), one container, Caddy in front. About 100–150 MB of RAM, so it fits the dev box beside the demo.
  Go would match the reference projects but means rewriting adapters 24goods already has.
- **Dev URL:** `ops-dev.kesiv.com` (D2): Cloudflare DNS record made with the owner's token (proxied), host Caddy
  site, origin locked to Cloudflare. If the token allows it, also put Cloudflare Access (one e-mail) in front.
- **Login:** single owner account. Passkey or password + TOTP, 12 h session, and re-auth (TOTP) before money
  actions: a send, a top-up, revealing a session or card.

### 5.2 Modules
| Module | What it does |
|---|---|
| **Session desk** | Paste a session: parse e-mail, account id, plan, expiry and validity (same safe paste as the shop). Check the account through the Zovocard preflight, zzshu `subscription-lookup`, or a direct check through the job's proxy. Stored **sealed**, shown masked, deleted automatically 72 h after the job closes. Never logged. |
| **Jobs** | One job = one customer upgrade (plan, region, target amount, linked 24goods order or manual). Stages and timeline with seconds; every attempt kept: channel, card, proxy, amount, fees, result. **Several charges:** a job can be funded in several attempts or cards; the console shows charged vs needed and stops at the target. |
| **Channels** (one adapter contract: `check`, `quote`, `submit`, `status`, `cancel`) | **Zovocard** direct and virtual-card · **zzshu** direct with card · **My protocol**: the console sends a signed JSON job (plan, region, session ref, card ref, proxy) to the owner's endpoint and takes signed status callbacks · **Manual**: the owner records the outcome |
| **Cards** | Three kinds: **issuer-held** (Zovocard card id; details fetched at use time, never stored); **manual** (number and expiry sealed, **CVV never stored**: typed per use); **protocol-held** (the owner's protocol keeps the card; the console knows only an alias). Per card: region, issuer, balance, fees, success rate, effective cost per successful upgrade. |
| **Proxies** | SOCKS5/HTTP pool with region tag, credentials sealed, health and exit-IP check, sticky binding per customer session (the same exit for the whole job). Supplier APIs go out from the whitelisted server IP, not the proxy. |
| **Browser helper** | A small **local** program on the owner's PC (Playwright). It pulls a job, opens a fresh profile through the job's proxy, sets the session cookie, opens chatgpt.com, and the owner completes the checkout. It returns the result to the job. Local because headless Chrome does not fit the 2 GB box, and the owner must be present for 3-D Secure. |
| **Pricing** ("cheapest region" button) | Reference table (U1 data; Hvoy-method web prices with manual override) + supplier live quotes + FX. Per plan: every region sorted by total cost (price + channel fees + card fees + FX spread), suggested price at the chosen markup, profit per sale, our current 24goods price. Optional "push to 24goods" through the audited pricing action. |
| **Ledger** | Per job: revenue (24goods order or typed), costs (card charge in local currency at the real FX, supplier fee, decline/small-amount fees, proxy), profit. Per day/week and per card/region/channel. CSV export. |
| **Wallet** (§6) | Wallet 1 balances and incoming transfers; supplier top-ups with confirmation; "credited?" tracking. |
| **Audit** | Append-only log of every action (who, what, before/after, when, IP). |

### 5.3 Phases (each = PRs with CI; acceptance in brackets)
| Phase | Content | Effort (I) |
|---|---|---|
| **B0** | Decisions D1–D5; repo, CI (ruff, pytest, vitest, gitleaks, Trivy, pinned actions), README | 0.5 d |
| **B1** | Skeleton, owner login + TOTP, audit log, sealing keys, DNS `ops-dev.kesiv.com` + Caddy [owner can log in; everything behind login] | 2 d |
| **B2** | Session desk + account check (Zovocard preflight, zzshu lookup) [paste → masked summary + current plan] | 2 d |
| **B3** | Jobs + channels Zovocard, zzshu, Manual; multi-charge; timeline [a sandbox job runs end to end] | 4–5 d |
| **B4** | Cards + proxies [job bound to card + proxy; exit IP shown] | 2 d |
| **B5** | Pricing button + ledger [table matches §1; profit per job] | 3 d |
| **B6** | Wallet: balances, history, supplier top-up with confirmation and credit tracking [test transfer on the cheapest chain] | 3 d |
| **B7** | 24goods link (needs U6): manual queue, reveal kept session, mark fulfilled [first-order flow works on the demo] | 2 d |
| **B8** | "My protocol" adapter + local browser helper [the owner's protocol completes a job] | 3 d |
| B9 | Production environment (own host or c4 beside the shop), backups | 1 d |

Order: B0 → B1 → B2 → B3 first. That alone replaces the manual top-up work. Then B5, which pays back fastest, then the rest.

---

## 6. OKX wallet design

- **Receive (shop):** BEpusdt watches the Wallet 1 addresses (U2). No key on any server, and the balance can be
  read from public addresses.
- **See balances (console):** read Wallet 1 by its **public addresses** (onchainos public-address portfolio or the
  chain explorers), so the console has no power to move funds from it.
- **Pay suppliers:** use a **separate Agentic Wallet account as a small hot wallet**. Its keys stay in OKX's TEE,
  and it gets topped up from Wallet 1 by hand. On OKX's web portal, set its **Policy**: whitelist = supplier deposit
  addresses only (e.g. Zovocard's), plus per-send and daily limits. The Policy is enforced by OKX, so even a
  compromised console cannot send elsewhere or more.
- **Flow:** the console proposes a top-up (Zovocard minimum 50 USDT; trigger on `spendable_balance`). The owner
  re-authenticates and confirms, then the console runs `onchainos wallet send` (first without `--force`, shows OKX's
  confirmation text, then with it). It records the tx hash, then matches it with Zovocard `balance-logs` and marks
  it **credited**. Automatic top-ups only later, inside the Policy limits, if the owner wants them.
- **I / D3:** if "Wallet 1" is a normal OKX Wallet app wallet (seed on the phone), it stays receive-only, and sends
  happen in the app or from the Agentic hot wallet. If it is already an Agentic Wallet, the same Policy rules apply
  to it directly.

---

## 7. Risks and limits (stated plainly)

- **Customer sessions are full account access.** They stay sealed, are masked on screen, deleted after 72 h, and
  revealed only with a reason (audited). A console breach would expose them, so access is owner-only behind
  2FA (and Cloudflare Access).
- **Card data:** storing card numbers and CVVs brings PCI-DSS obligations. The design never stores a CVV and
  prefers issuer-held cards.
- **Billing details:** the console uses each card's real billing details. It will not generate invented
  ("tax-free state") addresses like the `kesiv-commerce-mvp` autofill script. That misstates the buyer's location
  for tax, and I will not build it.
- **Platform rules:** buying in another region through proxies breaks OpenAI's terms. Accounts can be flagged, and
  card issuers can decline or close cards. The console reduces this (sticky region per customer, few calls) but
  cannot remove it. Price it into the margin.
- **3-D Secure and captchas are completed by the owner**; the console never tries to bypass them.
- **Money-moving code** (wallet sends, U2 address change, U3 card handling) gets the security-reviewer /
  payment-auditor pass, and production changes need the owner's go-ahead in the same turn.
- **Resources:** the dev box has 2 GB. Builds and tests run in GitHub CI, and the browser helper runs on the owner's PC.

---

## 8. Decisions needed from the owner

| # | Decision | Recommendation |
|---|---|---|
| D1 | Repo name and visibility | private `kesiv-ops` |
| D2 | Dev hostname | `ops-dev.kesiv.com` (production later `ops.kesiv.com`) |
| D3 | Is "Wallet 1" an OKX Wallet app wallet or an onchainos Agentic Wallet? | app wallet = receive-only + a separate Agentic hot wallet for supplier payments |
| D4 | How is "my protocol" called (HTTP? CLI?) and what does it need (card, session, proxy, region)? | HTTP endpoint with HMAC-signed job + callbacks |
| D5 | Card storage: CVV never stored, card numbers preferably issuer-held | yes |
| D6 | 12 % on top of supplier fees (current) or on the official price only | on top of fees |
| D7 | Start with the console (B0–B3) or the shop upgrades (U1, U6) first | console B0–B3, then U6 + B7 |
| D8 | Production go-ahead for #62–#71, migration `0036`, Redis ACL line (#47), and production prices | open |
