# Progress

Update this file in every PR. Phases are defined in [review-v2.md §8](review-v2.md#8-revised-plan-and-timeline-solo-developer--agents-honest-estimate).

## Current state — hand-off (read this first in a new session)

**Hand-off 2026-11-18 (after PR #49; the owner's order #29 → #30 → #31 is done on the demo). START HERE.**

- **Production (c4) runs `55ad99a`** (v12 launch, see the launch block below). Nothing newer is deployed there, by
  owner decision ("develop on the demo, not production"). Waiting for production: #36 (no virtual-card sales /
  no card reveal), #37 (cost model), #38 (regions + half-price check), #42 (card modes), #45 (card pool console), #47 (progress display; **needs the procurement Redis ACL line
  from ADR-043 in production's `users.acl` plus a Redis restart**) #49 (redemption admin; migration `0035`), #53 / #61 (demo catalogue switch, session paste fixes), #62 (account pre-check before payment), #63 (issue #54 fixes, incl. `deploy/backup.sh` fencing procurement), #64 (live progress, seconds, recent purchases) and #65 (session sharing with support; migration `0036`).
  Hand deploy procedure +
  `--profile procurement` in the launch block.
- **Demo (demo.kesiv.com) runs `main` `9465b0d`** (deployed 2026-11-18 18:19 UTC): v12 + #36 + #37 + #38 + #42 + #45 + #47 + #49 + #53 + #61 + #62 (account pre-check) + #63 (issue #54) + #64 (live progress, seconds, recent purchases) + #65 (session sharing with support) + #67 (owner feedback: no supplier wording, session link, order page catch-up, markup on cost, redeem flow) (CI images of run 44366467391 loaded as `goods24-{api,web}:m2-demo`, previous kept as `:demo-prev`; migrations up to `0036_support_sessions`; pre-deploy dump `/root/demo-backups/demo-20261118T181827Z.dump`). **Demo config 2026-11-18:** cost settings `target_markup = 0.12` and every ChatGPT price applied from it (cost x 1.12 per region, audited `apply`, floors untouched); the "ChatGPT Plus" code product has `redeem_url = https://redeem-demo.kesiv.com/`; the demo redemption site answers for BOTH `redeemdemo.kesiv.com` (owner's new name, **no DNS record yet: NXDOMAIN on 2026-11-18 18:00 UTC**) and `redeem-demo.kesiv.com` (`REDEEM_HOST="redeemdemo.kesiv.com, http://redeem-demo.kesiv.com"` in the demo `.env`, backup `/root/demo-backups/env-20261118T175021Z`; host Caddy site `/etc/caddy/sites/redeemdemo.kesiv.com.conf` added, host Caddy has `admin off` so a change needs `systemctl restart caddy`). Once the DNS record exists: switch the product's `redeem_url` and redirect the old name. Demo Redis ACL: the
  `procurement` user has `&rt:procprogress +publish` (backup `/root/demo-backups/users.acl-20261117T214314Z`). Demo redemption page `redeem-demo.kesiv.com` (production page
  `redeem.kesiv.com`). Testing guide: `docs/testing-guide.md`. **The demo's supplier is the built-in simulator**
  (`sandbox-off.sh` run 2026-11-18 07:49 UTC, env backup `deploy/env.bak-20261118T074913Z`); the supplier row is
  still labelled `zovocard · Sandbox`. **Demo showcase (2026-11-18, set up through the admin API, audited):** the
  direct product is live as "ChatGPT Plus · direct top-up (demo)" with four plan × region variants (Plus and Plus
  half price × Philippines / US; 99 / 69 / 169 / 139 CNY), all sources on the simulator's funding card `1`.
  **Demo catalogue (2026-11-18, owner request):** the simulator runs with `APP_MOCKZOVO_CATALOG=full` (demo
  `deploy/env/mockzovo.env` only): every documented GPT direct plan is on sale (Go, Plus, Plus half price, Pro 5x,
  Pro 20x, Pro 50x; synthetic PHP prices) in the five regions PH / US / JP / CL / EG. The category "ChatGPT Direct
  Top-up (Demo)" holds one product per plan with one variant per region (half price on Plus only), for the owner to
  pick what goes to production. Renewal (`*_renew`) and Codex credit plans are not direct top-ups (the adapter
  refuses them) and are not listed.
  **Owner test 2026-11-18 (11:46–11:55 UTC) and fixes:** order GGDZ26YVZJAN6 went to manual review with
  `duplicate_code`: the demo simulator restarted at the 11:20 deploy and reused vendor order id 801 (already
  delivered at 10:38); the engine correctly refused a second delivery. The simulator now seeds its vendor ids from
  the clock at start-up. The owner's Chrome froze while pasting the session (Incognito worked: an extension in the
  normal profile scanning the long text); the session sheet now takes a paste out of the text box at once and shows
  a confirmation (masked account, current plan, validity, size) or a clear problem (cut off, token only, missing
  part, expired) instead of rendering the token. Regions are listed cheapest first and the cheapest region in stock
  is preselected (owner rule). A direct order in manual review now points the buyer to the progress panel (not
  "checking the payment"), and the fresh-session button is primary when the server asks for a new session.
  **Account pre-check before payment (owner decision 2026-11-18, review-v12 §24, ADR-045):** every direct
  checkout (and every session resupply) asks the supplier's preflight first; a signed-out session, an account
  that already has the plan (or another subscription), a "not yet" or a vendor block is refused with a clear
  message BEFORE any order exists, and an unanswered check refuses too (fail closed). On the demo the
  simulator takes the account's plan from the pasted session (`account.planType`; e-mail tags `+active`,
  `+blocked`, `+wait`, `+recover` for the other outcomes).
  **Owner test 2026-11-18 (afternoon), live updates:** the buyer page showed "Live updates paused" and the
  direct progress stood still: the demo log had many `429` on `/api/v1/stream` (4 streams per account; behind
  Cloudflare a closed tab is noticed late) and the progress panel did not refetch on the polling tick. Now a new
  stream replaces the account's oldest one, and the panel refetches on every polling tick and on any order
  change. Times show seconds everywhere (buyer and console). The console's order drawer shows the supplier
  progress of a direct order live (staff stream `order_progress`), the Procurement job drawer refreshes live. The
  storefront shows "Bought in the last hour" (real deliveries, masked buyer, `GET /recent-purchases`).
  **Audit issue #54 (2026-11-18):** all seven findings reproduced and fixed (review-v12 §25): backup fencing of
  procurement, verified off-site pulls, the console order read after rollback, staff release of late-paid supplier
  orders, half-price CDKs kept out of the cart, the settings link, card-list pagination.
  **Session sharing with support (owner decision 2026-11-18, review-v12 §26, ADR-046):** the session sheet asks
  (ticked by default) whether support may see the session if the top-up fails; a consented session of an order
  that goes to staff or fails is kept sealed for 72 h and shown in the console's order drawer after a written
  reason (audited). Migration `0036_support_sessions`.
  **Owner feedback 2026-11-18 (evening):** the session sheet shows a full-width button that opens
  `https://chatgpt.com/api/auth/session` in a new tab; buyer pages never mention a supplier (progress steps,
  pre-check texts and the privacy note reworded, EN and zh-CN); the order page catches a delivery that finished
  before its live stream opened (one catch-up fetch on open, and a 4 s look while an automatic order is "paid");
  the cost model takes a markup on cost (`target_markup`, 0.12 for ChatGPT on the demo); the redemption link is
  no longer on the product page: after delivery the code comes first, then "Redeem now", which opens our own
  redemption page with the code filled in through the URL fragment (never sent to a server) and copied. The
  demo simulator's orders now report the pasted account (showcase mode), which had held the owner's demo direct
  orders as `order_mismatch`.
- **The ZovoCard sandbox is NOT available and cannot be used for the time being (2026-11-18).** The sandbox
  token and test account have expired (the authenticated API answers 401 since at least 04:38 UTC), the testing
  period has ended and the support-ticket deadline has passed, so no new sandbox can be requested now. **Rule
  from now on:** proceed on the vendor's documentation and on what was verified against the sandbox on 2026-11-16/17; everything still marked [I] stays unconfirmed and keeps its fail-closed handling (unknown vendor values never map to completed / failed, guessed codes go to
  staff, vendor card switching stays refused for production suppliers). Nothing below may assume a sandbox run.
  An item that needs real vendor behaviour can only be confirmed in production, with the owner's explicit
  go-ahead, as a small first order watched by staff, or by a written answer from the vendor.
- **Open work, in order** (tracking issue #33; owner accepted all suggestions; demo only):
  1. ~~#27 PR #38~~ **done**: plan × region variants + half-price check before payment, merged `401c401` after three
     fix rounds and re-audits (review-v12 §20.8), deployed to the demo, browser-checked (buyer product page at
     375 px, console Procurement → Regions in light and dark). **Before real money:** a `manual` check on a direct
     source has no account binding (vendor preflight is the gate); demo has no plan × region variants yet (create
     them with "Generate plan × region variants" on a CDK / direct product to try the buyer flow); e-mail
     verification at registration is off by default (the 24 h account-age gate is the control for `chatgpt_direct`).
  2. #29 automatic card selection / issuance (owner: yes). **Parts 1 and 2 done** and on the demo: PR #42 `9b38f1a`
     (card modes, review-v12 §21) and PR #45 `edbf0f9` (console Cards tab: pool, schedule preview, rules editor,
     failed-usage reset, decline monitor; §21.8), browser-checked against the real vendor sandbox (before it
     expired). **Before real money (no sandbox any more):** that the vendor rule `auto_switch_on_fail` cannot
     override our per-order `no_auto_card_switch=true` is documented but unexercised, so keep the vendor rule at
     `auto_switch_on_fail=false` in production; the guessed vendor codes / shapes marked [I] stay unconfirmed
     (handled fail-closed). #29 stays open only for
     automatic card issuance (waits for the vendor's answer).
  3. ~~#30~~ **done**: PR #47 `6978938` (buyer progress steps + live updates via a least-privilege relay, redemption
     page progress, staff supplier timeline; review-v12 §22, ADR-043), on the demo, browser-checked (order page at
     375 px light/dark, redemption page markup, console job detail). The vendor status / stage values marked [I]
     cannot be confirmed (no sandbox); unknown values map to safe in-progress steps, never to completed or failed.
  4. ~~#31~~ **done**: PR #49 `347c9d6` (redemption admin in the console: attempt log with masked data, code lookup,
     stats, settings with announcement and maintenance switch, vendor disable / enable for codes we sold; review-v12
     §23, ADR-044; own design, the vendor's unlicensed repo used only as a checklist), on the demo, browser-checked
     (console tab light/dark; maintenance switched on and off end to end, banner at 375 px). Vendor CDK list shape
     is [I].
  **Next (not started):** 5. #35 shop sells codes, redemption on its own platform (both modes kept; direct can be
     switched off per product), 6. #34 OKX Agentic Wallet top-ups (later).
- **Audit exception (owner-approved 2026-11-17, temporary):** `pnpm audit` ignores GHSA-fc5q-39xc-vjpf (picomatch
  ReDoS, no patched release) only; not reachable at runtime or from untrusted input. **Mandatory review
  2026-12-17** (`pnpm lint` fails after that date). Assessment: `docs/security-exceptions.md`.
- **Owner decisions of 2026-11-17** (details on the issues): five ChatGPT regions (PH, EG, CL, JP, US); price
  recommendations only (#28); virtual cards never shown or sold to buyers (#32, done); ZovoCard may pick/issue cards
  automatically (#29); half-price check method admin-selectable, `chatgpt_direct` = plain read-only check, no
  fingerprinting / anti-bot workarounds (#27).
- **Owner tasks still open for production**: top up the ZovoCard wallet (6.27 USD at launch), set the production
  webhook callback, create products/sources, rotate the ZovoCard keys and `APP_OWNER_PASSWORD` (pasted in chat),
  file the security review report, encrypted backups + restore drill.
- Owner's Chrome freezing after pasting a session: our pages are fine in a clean Chrome (measured); likely an
  extension or Chrome writing help in the everyday profile; the session boxes now opt out of such tools.

**v12 released to `main` (2026-11-17, owner decision).** The owner stated on 2026-11-17 that the independent
security review (M1 exit gate, AGENTS.md §1.11) has been passed and approved merging v12 procurement to `main`,
deploying it to production and selling with real money. The review report itself is not in the repository yet:
file it under `docs/research/` or link it here.

**Demo testing guide and demo redemption host (2026-11-17).** `docs/testing-guide.md` is the hands-on guide for the
shop, the console and the ZovoCard features (CDK, virtual cards, direct subscriptions, refunds, staff actions).
`redeem.kesiv.com` serves production only; the demo's own redemption page is **`redeem-demo.kesiv.com`** (Cloudflare
DNS → the demo box's Caddy `/etc/caddy/sites/redeem-demo.kesiv.com.conf` with the wildcard origin certificate →
demo stack, `REDEEM_HOST=redeem-demo.kesiv.com`).

**Production launch of v12, 2026-11-17 (owner's go-ahead in the same message).** Production (c4) runs `55ad99a`
(`main` after the release merge #23 `d074ac3` and the redeem fix #24); migrations `0023`–`0030` applied (database at
`0030_cred_delete_backstop`); api / web / delivery / **procurement** healthy; `prod.sh smoke` all pass; data intact
(42 orders before and after); FORCE RLS on every new table. Pre-deploy dump:
`/root/kesiv-backups/predeploy-v12-20261117T040400Z` (sha256 verified, 44 tables). Rollback images on c4:
`goods24-{api,web}:rollback-eed9972` (before v12; to roll back the schema too, restore that dump) and
`:rollback-d074ac3` (v12 before #24). Set up by hand on c4 (backups `deploy/env.bak-20261117T042914Z`,
`.env.bak-*`, `/opt/kesiv/backups/users.acl-*`, `/opt/kesiv/backups/certs-*`): session keypair
`secrets/cred_{public,private}_keys.json` (kid `c20261117-e7…`, private key 0400 for the container user only),
`env/procurement.env` (DB role `app_procurement`, Redis user `procurement`, production ZovoCard keys,
`APP_PROCUREMENT_ALLOW_PRODUCTION_WRITES=true`, redeem proxy `APP_REDEEM_*` with `SUPPLIER_ENV=production`),
`env/api.env` additions (procurement URL / token, session public key, production webhook key,
`APP_PROCUREMENT_ALLOW_PRODUCTION=true`), `APP_DB_PROCUREMENT_PASSWORD` in `env/migrate.env`, `REDEEM_HOST` in
`.env`. **Compose commands on c4 now need `--profile procurement`** (otherwise the procurement process is not
started; `scripts/prod.sh deploy` does not pass it and still has the dead registry token — use the hand procedure).
Production supplier `zovocard` (env production) created and enabled through the admin API; probe green (auth, IP
allowed, card + direct permissions). **Vendor wallet balance at launch: 6.27 USD.** Production webhook endpoint
verified with a self-signed event (signed 200, forged 401).
**`redeem.kesiv.com` moved to production:** new Cloudflare Origin CA certificate for `shop` + `redeem`
(id `15699080…`, valid to 2041; key and old files in `~/.secrets/prod-certs/`, old set in `old-*`), per-hostname
authenticated origin pull enabled for `redeem.kesiv.com`, origin rule port 8088, DNS A → 141.98.198.150. The page
and the proxy answer against the production vendor (an unknown code gets `code_unavailable`). The demo is back on
`redeem.localhost`; the dev box's `/etc/caddy/sites/redeem.kesiv.com.conf` was removed (backup in
`/root/demo-backups/`).
**Owner to do before selling supplier products:** top up the ZovoCard wallet; set the production webhook callback
to `https://shop.kesiv.com/api/v1/webhooks/zovocard` in the vendor console (signing key = `ZOVOCARD_WEBHOOK_SECRET`);
create the products and their sources (plan / card product, price, cost estimate, our funding card for direct
products) in the console; rotate the ZovoCard production API key and webhook key and `APP_OWNER_PASSWORD` (all
were pasted in chat); file the security review report. Still open: encrypted backups and a restore drill (#28);
c4 disk at 88 %.

**Production deploys 2026-11-16 (owner's explicit go-ahead, by hand).** (1) `24a64fb`: dark mode #6 + Telegram
language #7, no migration. (2) `eed9972`: optional two-factor login #10, migration `0022a_user_totp` (additive;
FORCE RLS verified on c4). `prod.sh smoke` all pass after each; api / web / delivery report `eed9972`.
Rollback images on c4: `goods24-{api,web}:rollback-24a64fb` (before #10; drop table `user_totp` is not needed
to run it) and `:rollback-d881dfa`. Before (2), two-factor login was walked through in a real browser on the
demo (set up, sign in with a code, turn off). Hand procedure, still valid (`scripts/prod.sh deploy` has a dead
registry token): tag rollback images from the running `:main`, `compose pull api web`, `run --rm migrate`,
`up -d --wait`, `scripts/prod.sh smoke`. c4 root disk is at 87 %: old rollback images can be pruned.
**Backups: the nightly off-host copy had been failing since 2026-11-08** (the dev-box cron ran
`scripts/prod.sh backup`, which refuses until #28 is done; c4 itself kept making its 03:30 set). Fixed:
`scripts/pull-prod-backups.sh` (read-only on c4, checksum-verified, keeps 14 sets) is what the cron runs now;
the 2026-11-16 03:30 set is in `/root/kesiv-backups/nightly-20261116T033001Z` (`pg_restore --list`: 43 tables).
Backups are still unencrypted and there has been no restore drill (#28, owner).

**Optional two-factor login (2026-11-16, PR `feat/totp-2fa`, ADR-039, migration `0022a_user_totp`).** Buyers
and staff can turn on an authenticator app (TOTP). Login for an enrolled account is password → pending token
→ code (`POST /auth/login/2fa`); ten single-use recovery codes; admins can reset another user's second
factor (Users page). Buyer app: second step on the login page (also after Google sign-in), My account →
Settings → Two-factor login. Console: second step on the login page, user menu → Two-factor login. Tests:
`tests/test_twofactor.py`, `tests/test_totp_core.py`. Not built: "require 2FA for staff". Security-reviewer (2026-11-16): pass with notes, no bypass of the second factor; its High finding (race in
the wrong-code limiter) and the Medium ones (password reset keeps a squatter's enrolment; admin could reset
another admin) are fixed, the rest are recorded as accepted risks in ADR-039. **Needs a human review before
merge (auth path, AGENTS.md §5.3); the UI was typechecked by CI only and never seen in a browser.** When v12 reaches `main`, `0023_procurement` must revise `0022a_user_totp`.
**Hand-off 2026-11-16 (this block supersedes the ones below).**

- **v12 M2 is merged into `feat/v12-procurement`** (PR #2, squash `bfbd21c`, CI green incl. E2E on the same
  tree). Work on v12 continues on `feat/v12-procurement` (branch from it; this worktree,
  `.claude/worktrees/agent-af3aa371765135692`, now has it checked out). `feat/v12-procurement` contains `main`
  up to `faaa5bf`; it is **not** in `main` yet (waits for M3 and the M1 exit gate, AGENTS.md §1.11).
- **Audit trail.** Re-audit findings N1–N8 and the `request_hash` rule fixed (`eca5b8b`..`1887c6c`). A second
  payment-auditor pass closed all of them except the write-side half of N3, which it failed (**F1, High**:
  "plan not offered after an ambiguous write" could clear `maybe_sent` after the supplier had issued a code,
  then buy again elsewhere). That half was removed (`560896f`): the fresh-catalog pre-check stays, an
  ambiguous write keeps the same-key reconcile and never fails over. F1's fix was not re-audited (it deletes
  the path, as the auditor proposed). Auditor notes left open (Low, M3): no separate alert when the supplier
  refuses the void of a refunded order's code (`_apply_void` puts the job back to `fulfilled`); pre-check
  retries use the submit retry budget; the catalog pre-check and the submit share one 120 s lease.
- **Demo (`demo.kesiv.com`) runs the merged M2 build** (CI images of run 43789343563 loaded as
  `goods24-{api,web}:m2-demo`; previous ones kept as `:demo-prev`; migration `0027`; checkout
  `/opt/24goods-demo` on branch `demo-m2` = `origin/feat/v12-procurement`). New there: the **procurement
  process and the `mockzovo` simulator run** (`env/procurement.env`, `env/mockzovo.env`, Redis ACL user
  `procurement`, `APP_PROCUREMENT_URL/TOKEN` in `api.env`); the API verifies webhooks with **both** sandbox
  keys (key-level first, then general). Backups: `/root/demo-backups/demo-20261116T*.dump`,
  `deploy/env.bak-20261116T*`. Checked in a browser on the demo: buyer home, product, login and order lookup
  in dark and light, console Procurement and Suppliers pages in dark — all render correctly. Not exercised by
  hand: an `upstream_cdk` purchase on the demo (the E2E smoke covers it in CI).
- **Production (c4) runs `eed9972`** (2026-11-16, by hand with the owner's go-ahead: dark mode #6, Telegram
  language #7, two-factor login #10 with migration `0022a_user_totp`; `prod.sh smoke` all pass). Rollback
  images on c4: `goods24-{api,web}:rollback-24a64fb`, `:rollback-d881dfa`. Details and the hand procedure are in
  the top block of `docs/progress.md` on `main`. Nightly off-host backup pull fixed there too
  (`scripts/pull-prod-backups.sh`).
- **Redemption site (spec §8, §8.1): merged** (PR #13, `196ace4`). Public `/redeem/*` proxy on its own listener
  in the procurement process (`app/procurement/redeem.py`, off unless `APP_REDEEM_ENABLED=1`), `redeem` compose
  network, Caddy site for `REDEEM_HOST`, static bilingual page in `deploy/redeem/` (no framework, strict CSP, no
  storage), mock vendor endpoints, `tests/test_redeem.py` (60), E2E scenario `v12_redeem`. Security-reviewer:
  pass with notes; fixed: result is POST only, body deadline, explicit `APP_REDEEM_SUPPLIER_ENV` in prod, poll
  back-off, the page forgets input on leave. Accepted / open: the shared per-step budget (vendor limits per
  egress IP) can be exhausted from a few addresses — add a Cloudflare rate-limit rule on `/redeem/*` when the
  host goes live; no CAPTCHA on preview; the vendor's real preflight / result field names are unverified
  (shapers in `redeem.py` may need adjusting on the first real redemption); `/recover-subscription` is not
  proxied. **To go live it needs a DNS name** (`redeem.<domain>` in Cloudflare → the origin) and
  `REDEEM_HOST` + the `APP_REDEEM_*` lines (see `deploy/bootstrap.sh`).
- **`https://redeem.kesiv.com` is live (2026-11-16) on the DEMO stack against the vendor SANDBOX**, not
  production: real customer codes cannot be redeemed there until v12 is in production. Setup: Cloudflare DNS
  `redeem.kesiv.com` → this box (proxied, zone mode Full strict); TLS ends at the box's own Caddy
  (`/etc/caddy/sites/redeem.kesiv.com.conf`, Cloudflare Origin CA wildcard certificate, Cloudflare-only),
  which forwards to the demo stack on `127.0.0.1:18888`; demo `deploy/.env` `REDEEM_HOST=redeem.kesiv.com`.
  A "flexible" (plain HTTP to origin) rule was NOT used: the page carries account credentials. The demo's
  procurement process now talks to the **real ZovoCard sandbox** (`env/procurement.env`; backup
  `env.bak-20261116T10*`) instead of the simulator: **put the simulator lines back after the sandbox expires
  on 2026-11-18 10:57** (`ZOVOCARD_SANDBOX_APP_ID=mock`, `_APP_SECRET=sk_mock_ok`,
  `_BASE_URL=http://mockzovo:8005/openapi/v1`). Because every request reaches the demo's Caddy from the box's
  Caddy, per-IP limits there collapse into one bucket (stricter, acceptable for a demo).
  **Verified against the real sandbox through that host with a real session text:** preview, preflight
  (`preflight_token`, `payment_country`, `payment_currency`, `subscription_recovery_required`), redeem
  (`status: succeeded`), result 404 (as documented for the sandbox); no credential in any log. Two defects
  found and fixed that way: a real session text is ~37 KB and was refused with 413 (#16: session 64 KiB, body
  96 KiB, Caddy 128 KB); the order id is `data.order.id` and was dropped (this PR). Still unverified: the
  result shape (the sandbox never has one) and a non-mock redemption.
- **Demo** runs the v12 branch incl. two-factor login and the redemption site (CI images loaded by hand).
  `user_totp` was created by hand there (it was already past 0022a). The mock supplier row is enabled.
- **M3 part 1 (`upstream_vcc`, virtual cards per order): merged** (PR #15, `985a589`, migration `0028`, spec §17).
  Adapter (open → optional top-up → read card), engine with every M2 rule, the card sealed as one secret
  `pan----MM/YY----cvv` through the existing vault path, 3DS one-time codes to the card owner (Redis TTL + SSE,
  never stored; `GET /orders/{ref}/card-otp`), ledger links by card id, console (card mode, source form, masked
  card in jobs, admin-only card paste) and buyer UI (code panel on the order page), en + zh-CN.
  Payment-auditor: first pass failed it (refund of a delivered card left it live; a replayed open could make
  a second card); F1–F11 fixed; re-audit pass with notes; the four notes fixed (not audited again).
  Security-reviewer: pass with notes; fixed: the card fingerprint no longer derives from the card data
  (`lookup_fp` NULL), card paste is admin-only, the leak test sweeps every table.
  **Verified against the real ZovoCard sandbox on the demo (2026-11-16):** a card order was bought (10 USD,
  product `USMAB01`), sealed, delivered and revealed (16 / 5 / 3 characters, mask equal to the console's);
  after an after-sales refund the job went to `voided` and the supplier reports the card `DELETED` with
  balance 0. Not verified: the recharge-pending 400, a real `card_3ds_otp` event, real fees (the sandbox
  charges none), the buyer and console screens in a browser (typechecked by CI only).
- **M3 part 2 (`upstream_direct`, the supplier subscribes the buyer's own account): merged** (PR #18 `c64a052`,
  then #19 and #20 from the sandbox test; migrations `0029`, `0030`; spec §18). The buyer's account session is
  sealed at checkout with a SEPARATE procurement keypair (`credvault.py` seals in the API; `credopen.py` opens,
  procurement only; table `procurement_credentials`, FORCE RLS, `app_api` cannot read the blob), opened only for
  the supplier call, deleted when the order ends (engine sweep at the start of every pass, 5 h cap) with a
  database backstop (`app_api` may delete rows older than 6 h; checkout pauses with 503 `direct_unavailable` if
  that ever fires). Preflight account-state checks, quote bound, funding-card checks (ours, ACTIVE, uses left, one
  order in flight per card, never a card sold to a buyer), async order with polling, slow poll of held orders,
  ledger by kind, console (mode, source form, job view, admin-only hand fulfil, refund acknowledgement) and buyer
  UI (account-session sheet at purchase, order status, re-supply). Payment-auditor: first pass failed it (H1
  funding card could be a buyer's card; H2 failover after a possible send); fixed with M1–M8; re-audit pass with
  notes; N1–N6 fixed (not audited again). Security-reviewer: pass with notes, all fixed.
  **Verified end to end on the demo against the real ZovoCard sandbox (2026-11-16/17):** funding card opened in
  the sandbox (`PP5259RC`, id 257662, 50 USD), direct product `gpt/plus` sold on the demo with the owner's test
  session → supplier order 50675 completed → order `delivered`, buyer sees `s***@…` + `gpt/plus`, delivery text
  `gpt/plus----<masked account>----<supplier order>`, cost 20 USD recorded, stored session deleted, no credential
  in any log. Two sandbox quirks found and handled for the host `sandbox.zovocard.com` ONLY (production
  unchanged): orders must be sent WITHOUT the preflight token (#19), and the sandbox reports different synthetic
  accounts in preflight and order (#20). One extra sandbox order (50674) was placed directly by the orchestrator
  while diagnosing; sandbox money only. Not verified: a real quote (the sandbox returns `quotes: null`),
  idempotency-key retention, production behaviour, the new screens in a browser.
- **Demo setup added for M3 part 2:** `deploy/secrets/cred_{public,private}_keys.json` generated on the demo
  (kid `c20261116-72…`), `APP_CRED_*` lines in `api.env` / `procurement.env` (backups `env.bak-20261116T2*`).
  The sandbox test product is set to draft.
- **Browser check (2026-11-17, headless Chrome, 390 px, light + dark):** buyer direct product → buy sheet ("one per
  order") → "Connect your account" sheet → delivered direct order with the account panel; console direct job
  detail. Fixed from it (#22): step numbers in the account sheet, no "Your codes / Reveal codes" box on a direct
  order. The console CSP error seen there is Cloudflare's injected bot-detection script being blocked (also on
  production; harmless). Both sandbox test products on the demo are set to draft.
- **Production deploy of `main` (`7951c4c` = #14 two-factor hint fix + docs) was refused by the session
  permission system on 2026-11-17**; production stays on `eed9972`. Today's backup is pulled
  (`/root/kesiv-backups/nightly-20261117T033001Z`). Hand procedure unchanged (tag `rollback-eed9972` first).
- **Next, in order:** (1) **after 2026-11-18 10:57** run `/opt/24goods-demo/deploy/sandbox-off.sh` on the demo host
  (puts procurement back on the simulator, backs up `env/`; not in git); (2) look at the M3 screens (cards, direct) in a browser; (3) v12 to `main` and
  production wait for the M1 exit gate (independent security review, AGENTS.md §1.11) — production will also
  need the credential keypair (§18.5) and the procurement process; (4) sandbox webhook: still no vendor delivery
  observed since 01:39 UTC on 2026-11-16.
- **Re-audit verdict (2026-11-15, head `2895860`): pass with notes — no Critical/High, no path to a double
  purchase or double delivery.** Original findings: H1, M1, M2, M4, M5, L1, L2, L4, L7 closed; H2, M3, L5
  partly; L3 and L6 deferred (`TODO(v12-M3)`). New findings and the decided fixes (paths under
  `backend/app/`):
  - **N1 (Medium)** the recovery of a closed job (`maybe_sent`, no `upstream_ref`) replays the key and can buy
    for a refunded order, and skips the kill switches (`procurement/engine.py` `_offer_any`,
    `_lease_detached`). Decision (orchestrator; **the owner may overrule**): the replay stays, because it is
    the only way to learn whether a code exists, and the cost is bounded at one issue fee per job. It must
    wait while the supplier is disabled or its `hard` breaker is open, then end in `needs_manual` with
    `recover_blocked` (maybe charged). Document in §16.
  - **N2 (Medium)** the recovery of a hand-fulfilled job can disable the code the buyer received
    (`modules/admin/procurement_jobs.py` `_schedule_recovery`, `engine.py` dvoid). Fix: record `upstream_ref`
    and write a critical audit row (`procurement.recovered_after_manual_fulfil`); never disable. Disabling
    stays an explicit staff `void`. Jobs closed by refund / fail keep the disable.
  - **N3 (Medium)** the pre-check uses the 60 s plan cache, so a just-paused plan still ends as maybe-sent
    after ~27 min (`suppliers/zovocard/supplier.py`). Fix: fresh catalog in the pre-check; after an ambiguous
    `request_failed` on a write, one fresh availability check — plan definitely not offered => `_unsent`,
    `last_check_ok=false`, fail over.
  - **N4 (Medium)** `last_balance_at` is stamped before `evaluate()`, whose errors are swallowed
    (`procurement/service.py`). Fix: stamp only after a successful evaluation (or gate on `last_eval_at`).
  - **N5 (Low)** a refund leaves `fulfilled` but undelivered units valid at the supplier. Fix: move them to
    `void_pending` (disable + void the `card_stock` row) with audit rows; delivered units untouched.
  - **N6 (Low)** the recovery's FATAL path clears `maybe_sent` on `IDEMPOTENCY_CONFLICT`; mirror the main path.
  - **N7 (Low)** a passed pre-check resets `attempts`, so a RETRYABLE submit retries forever. Do not reset.
  - **N8 (Low)** a queued, never-sent job parked behind a balance breaker never moves to a healthy source.
    Fix: fail over when another usable source exists; the job-stuck alert also fires for a long park.
  - **M5 caveat** a `maybe_sent` job with `request_hash` NULL is treated like a mismatch (`needs_manual`,
    maybe charged); a never-sent job gets its hash at first lease.
  - **N9–N11: no code change** (fail-closed `unknown_cost`, the 0027 trigger trusting `app_api`-writable job
    columns, the conservative `first_sent_at` backfill). Optional one-liner: put `unknown_cost` and the
    offending count into the breaker reason text.
  - Tests the auditor asked for: stale-cache paused plan (N3); recovery vs disabled supplier and `hard`
    breaker (N1); hand-fulfil where the recovered code is the pasted one (N2); failed evaluation after a good
    balance read (N4); unbounded RETRYABLE (N7); parked job with a healthy alternative (N8); refund with a
    fulfilled-undelivered unit (N5); recovery `IDEMPOTENCY_CONFLICT` (N6). The fresh pre-check adds one
    `GET /gpt-direct/plans` per first send: check the mock's rate limit and the e2e scenario plans.
- **Done in the 2026-11-15 session.** Audit fixes for H1, H2, M1–M5, L1, L2, L4, L5, L7 (`38754af`..
  `6469097`, migration 0027, 40 tests); admin page handles the new codes (`5531880`); `main` merged into the
  branch and procurement alerts moved to the Telegram message catalog (`2895860`). Behaviour change: a source
  cannot be switched on without a cost estimate unless the supplier reports its own USD price. On `main`:
  dark mode (#6, `3ce457e`) and the Telegram language switch (#7, `faaa5bf`), CI green, images published,
  **deployed nowhere**; dark mode has never been seen in a browser.
- **Sandbox webhook: one vendor delivery seen, none since.** 2026-11-16 01:39:10–16 UTC the demo API logged
  three `POST /api/v1/webhooks/zovocard` (one event, retries after 2 s and 4 s), all 401 "bad signature". At
  that time the demo knew only the API-key-level key; the owner has since confirmed that the keys in
  `~/.secrets/zovocard.env` match the console, so that event was almost certainly signed with the
  general-callback key. The demo now verifies with BOTH keys (self-signed events with either -> 200, forged ->
  401). The event arrived 40 s before this session's sandbox calls, so the API calls did not trigger it; three
  scripted passes (issue / disable / enable / disable; CDK ids 275–277) produced no delivery, and a packet
  capture until 06:00 UTC saw only our own test posts (capture stopped, file deleted). Conclusion so far: the
  sandbox does not send CDK events for API-driven changes; something done in the console does. Next: ask the
  owner to repeat a console action (save the webhook settings, redeem a CDK on the vendor's page) and watch
  `supplier_events` on the demo. Unverified against the real vendor until then; the engine's polling /
  reconcile path does not depend on webhooks. Rotate the signing keys (pasted in chat) before go-live.
- **TOTP 2FA (spec §13): built, PR #10 open against `main`, CI green (backend, frontend, E2E).** Branch
  `feat/totp-2fa` (worktree `.claude/worktrees/totp`). Backend, buyer UI (login second step, Settings →
  Two-factor login), console UI (login second step, user menu, Users → Reset 2FA), en + zh-CN, API contract,
  ADR-039, migration `0022a_user_totp` (**when v12 reaches `main`, `0023_procurement` must revise it**).
  Security-reviewer: pass with notes; its High (limiter race) and Medium findings are fixed, the rest are
  accepted risks in ADR-039. **Open before merge: a human review (auth path, AGENTS.md §5.3) and a look at
  the UI in a browser** (the demo runs the v12 build, which does not contain this branch). Not built:
  "require 2FA for staff".
- **Deployed:** production (c4) `d881dfa`; demo = local image `goods24-api:m2-demo` (`15d7f22`, an M2 build
  from before the audit fixes; no migration 0027). Unchanged: do not use `scripts/prod.sh deploy` until its
  registry login is fixed.
- Housekeeping: `docs/research/zovocard/` is untracked in the main checkout (vendor docs; decide whether it
  belongs in git); stale local branches `worktree-agent-*`, `fix/pay-ux`, `docs/deploy-*` can be deleted.

**Hand-off 2026-11-15 (superseded by the block above; kept for the M2 build notes and the original
audit findings).**

- **START HERE.** Branch `feat/v12-procurement-m2` (worktree `.claude/worktrees/agent-af3aa371765135692`,
  draft PR #2). Order:
  1. Check CI on the branch head (`gh run list --branch feat/v12-procurement-m2`). The run on `1430b47` had
     frontend + E2E green and the backend suite still running; the head after it adds the security fixes
     (`7eef58c`) and this note, so read the newest run. Fix what fails.
  2. Payment-auditor findings: **fixed 2026-11-15** (commits `38754af`..`6469097`, migration 0027, 40 tests in
     `tests/test_procurement_audit.py`; rules in spec §16; L3 and L6 left as `TODO(v12-M3)`). **Re-run the
     payment-auditor on the fixes** and close what it still finds before merging.
  3. Merge `main` into `feat/v12-procurement` (it is behind), take PR #2 out of draft and **squash-merge** it
     into `feat/v12-procurement` (this removes the WIP commit `68ca6df`).
  4. Redemption site (spec §8) as its own PR, after M2 is merged (owner decision).
  5. Own PRs: dark mode, Telegram notification language switch, optional TOTP 2FA (spec §13).
  Owner standing instructions (2026-11-15): merge green PRs to `main` without asking; deploy to demo and
  production once tested (tag rollback images first); English only.
- **M2 state: everything in spec §10 except the redemption site is built.** Backend (implementer, 8 commits
  `1543fcf`..`1d4a037`): admin password for production suppliers; cost ledger (`procurement/ledger.py`, one
  unique insert for webhook + `balance-logs` backfill, migration 0025); balance breaker
  (`procurement/breaker.py`, kinds `hard` / `balance`, auto-recovery, staff alert); admin API under
  `/api/v1/admin/procurement` (`/sources`, `/jobs` with requery / retry / switch / fulfil / void / fail,
  `/cost` summary / margin / unreconciled / CSV; migration 0026 grants); `POST /admin/suppliers/{id}/reset-breaker`;
  procurement + `mockzovo` in `scripts/dev.sh`, compose demo profile, `bootstrap.sh`, and
  `e2e/smoke.py::v12_procurement` (timeout-after-send, 409, 202, 503 failover, duplicate webhook, forged
  signature). Orchestrator: several webhook signing keys per environment (comma-separated), admin console
  page `ProcurementPage.vue` (jobs / sources / costs) + `api-procurement.ts`, Suppliers page password field and
  breaker reset, product dialog offers `upstream_cdk`, buyer app shows `upstream_cdk` as instant
  (`isInstantMode`), en + zh-CN strings. Not done: `docs/dev-setup.md` for the two new dev processes;
  ADR-038 update for M2; the admin UI was typechecked by CI only and not looked at in a browser.
- **Payment-auditor on M2 (2026-11-15): FAIL for real-money use; no double purchase or double delivery
  found under normal operation.** Open findings (paths under `backend/app/`):
  - **H1** a reconcile/verify call can buy after the order left `paid`, and `_fulfil` ignores order status
    (`procurement/engine.py` `_lease` order check only in the submit branches, `_fulfil`;
    `aftersales/service.py` `refund_order` never touches jobs). Refund during a reconcile => supplier paid,
    buyer refunded, code sealed to a refunded order. Fix: order check for every executing mode; `_fulfil`
    refuses a non-paid order, stores `upstream_ref`, goes `void_pending`; refund moves open jobs to
    failed / void.
  - **H2** a paused plan (HTTP 400 `request_failed`) opens the HARD breaker for the whole supplier and the
    job replays ~27 min, then is stuck as maybe-charged (`suppliers/zovocard/errors.py`, `engine.py`
    `_open_breaker`, `candidates`, `_lease`). Fix: `availability()` before the first submit; no
    supplier-wide breaker from an ambiguous `request_failed` (per-offer stop, `last_check_ok=false`).
  - **M1** staff `fulfil` / `fail` while the supplier call is in flight orphans a charged code
    (`modules/admin/procurement_jobs.py`; the engine then drops its OK result, `upstream_ref` never stored).
    Fix: refuse while the lease is live, or reconcile once to record the ref and void.
  - **M2** a stale `procure.failed` strands an order in `manual_review` with every job fulfilled
    (`delivery/worker.py` `handle_procure_failed` / `handle_procured`). Fix: ignore the event when no job is
    unresolved; deliver an order held for `procurement_failed` once all units are bound.
  - **M3** the balance breaker fails open (unknown reserve counts 0; `min_spendable` default 0; no
    evaluation when the balance read fails) and its recovery does not resume jobs that were failed over
    (`procurement/breaker.py`, `engine.py`). Fix: fail closed (cost estimate or known plan cost required to
    enable an offer; unknown cost opens the breaker; stale balance => no lease); back off queued jobs on a
    balance breaker instead of failing them over.
  - **M4** `maybe_charged` is a heuristic over `error_code`, so "switch" can reach a second supplier after
    the first may have charged (retry/requery clear the code; `offer_missing` not listed). Fix: persist a
    `maybe_sent` flag in the `_lease` commit and base switch / failover on it.
  - **M5** reconcile/verify do not compare `request_hash` (an edited offer replays another body under the
    same key) and there is no maximum age for a same-key replay. Fix: enforce the hash in every mode; add
    and document a replay age limit.
  - **Low:** L1 store `upstream_ref` before any early exit in `_fulfil` (and cap that loop); L2 void does
    not void the `card_stock` row, touch the order or allow a replacement job, and voids a maybe-charged
    job without a supplier call; L3 late-payment upstream orders in `manual_review` cannot be released;
    L4 lock order job->order->siblings (admin) vs job->siblings (engine) can deadlock; L5 ledger backfill
    stops at the first page without new rows, naive timestamps assumed UTC, `ref_id` = CDK id unverified,
    silent CSV / summary truncation, margin includes refunded orders; L6 `handle_event` pulls
    `next_poll_at` forward on a leased job (matters for M3); L7 409 `IDEMPOTENCY_CONFLICT` should be FATAL
    + maybe charged, not "still running".
  - **Test gaps:** refund or hold during a reconcile; paused plan vs breaker; staff action racing an
    in-flight call; stale `procure.failed`.
- **CI findings fixed on the way:** `ruff format` on 0025; the `procurement` Redis ACL user lacked `INCRBY`
  (the rate limiter in the probe route answered 503 in the E2E stack; `deploy/bootstrap.sh`). **An already
  bootstrapped stack (demo, later production) needs the Redis ACL line updated by hand.**
- **Security-reviewer on M2: pass with notes, no critical/high.** Fixed in `7eef58c`: password also required
  to switch an existing production supplier on (M1 of the report); `code_prefix` no longer stored from CDK
  events; production and sandbox webhook keys must differ (settings validator, >= 32 chars in prod); CSV
  currency cell guarded; mock scenario plans only in demo mode. Left as notes: manual fulfil answers 409
  `duplicate_code` (a slow existence oracle for staff, rate limited); engine/ledger queries rely on the RLS
  pin without an explicit `tenant_id` filter (defence in depth); any staff role may fulfil manually (as
  specified).
- **Sandbox (expires 2026-11-18 10:57): second pass recorded in spec §12 / §14.3.** Settled: the CDK list
  exposes no idempotency key. Not measurable there: redemption cost (mock redemption), recharge-pending 400,
  `card_3ds_otp`. **No vendor-sent webhook has reached the demo yet.** Owner saved a key-level subscription;
  its signing key is in `~/.secrets/zovocard.env` (`ZOVOCARD_SANDBOX_KEY_WEBHOOK_SECRET`) and is what the demo
  API now verifies with (`/opt/24goods-demo/deploy/env/api.env`, backup `api.env.bak-20261115b`). Open
  questions to the owner: the key-level callback URL was pasted as `demo.kestv.com` (must be
  `demo.kesiv.com`); which of the two general-callback keys is the sandbox one. When answered, re-run the four
  sandbox calls (issue, disable, enable, disable) and watch `supplier_events` on the demo. The demo still
  runs the M2 image of `d043908` (single key per environment, no procurement process).
- **Production (c4) runs `d881dfa`** (PR #3 email redesign + #88), deployed 2026-11-15 by hand; rollback images
  `goods24-{api,web}:rollback-222b03b` on c4; details in the block on `main` (#5). Do not use
  `scripts/prod.sh deploy` until its registry login is fixed (dead token, and its logout removes c4's working
  login). Production webhook callback: leave blank until v12 is deployed there. To rotate before go-live: the
  ZovoCard production API key and signing key (pasted in chat), `APP_OWNER_PASSWORD` on c4 (printed once in a
  session log).
- **CI triggers work again** on the new repository (`push` and `pull_request` start runs).

**Hand-off 2026-11-15 (session ended early on quota; read this block first).**

- **START HERE (next session).** Work on `feat/v12-procurement-m2` (worktree
  `.claude/worktrees/agent-af3aa371765135692`, draft PR #2, CI green at `d043908`). Order: (1) finish M2 per
  spec §10 — check each item against the code first: admin-password confirmation for production suppliers,
  procurement + `mock_zovocard` in `scripts/dev.sh` and the e2e smoke, manual fulfil/void/switch, cost report,
  redemption site; (2) while the sandbox lives (until 2026-11-18 10:57) settle the [I] items of §14.3 and get
  one vendor-sent webhook (redeem a sandbox CDK); (3) security-reviewer + payment-auditor on M2, squash the
  WIP commit, merge into `feat/v12-procurement`; (4) security-reviewer on PR #3 (email redesign), merge;
  (5) own PRs: dark mode, Telegram language switch, TOTP 2FA. Local test loop without the full dev stack:
  `docker start kesiv-dev-pg kesiv-dev-redis`, then pytest with `TEST_DATABASE_URL` on port 55440 and
  `TEST_REDIS_URL` on 56390 (passwords in `.dev/.secrets`), under `flock .dev/heavy.lock`; stop the two
  containers afterwards. CI on the new repo: `gh workflow run CI --ref <branch>`.
- **Demo runs the M2 branch (2026-11-15):** `/opt/24goods-demo` is on local branch `demo-m2`
  (= `origin/feat/v12-procurement-m2` at `d043908`, remote switched to the new repo), images
  `goods24-{api,web}:m2-demo` loaded from the CI artifacts of run 43674783330 (`gh run download <run> -n
  image-api -n image-web`, `docker load`, no local build), migration `0024` applied, `deploy/.env` backup
  `.env.bak-20261115`. Added: `APP_DB_PROCUREMENT_PASSWORD` (migrate.env) and
  `APP_ZOVOCARD_SANDBOX_WEBHOOK_SECRET` (api.env); a `zovocard` sandbox supplier row was inserted by SQL. The
  procurement process is **not** running there (no `env/procurement.env`, no Redis ACL user), so stored
  events stay queued. Webhook verified through Cloudflare at `https://demo.kesiv.com/api/v1/webhooks/zovocard`:
  signed -> 200 and stored once, duplicate -> 200 and not stored again, forged -> 401. That was a self-signed
  test event (`selftest:20261115-1`); a vendor-sent event has not been observed yet. To return the demo to
  `main`: restore `.env`, check out `main`, load or pull the `main` images.
- **CI triggers and publishing (settled 2026-11-15):** on the new repo no check suite is created for any
  push or PR, so `push` / `pull_request` never start workflows; only `workflow_dispatch` runs. Repository
  settings are normal (Actions enabled, workflows active, account publicly visible), so this is an
  account-level restriction on the new GitHub account (inference; nothing to configure). **Procedure:** for a
  PR `gh workflow run CI --ref <branch>`; after merging to `main`, `gh workflow run CI --ref main`, which now
  also publishes the images (#4). First publish done: `ghcr.io/zucchinistockton/goods24-{api,web}:main` and
  `:75163f3` (run 43678521886, all green). c4 has a persistent `docker login ghcr.io` with a `read:packages`
  token and can read both images (checked with `docker manifest inspect`). Production itself still runs
  the old `arifulcrypto` `:main` images; nothing was deployed. There is no `rollback-*` image on c4: tag the
  running images before the next deploy. `~/.secrets/ghcr_token` on the dev box is still the dead token;
  `prod.sh`'s transient login will fail until it is replaced (or skip it, c4 is already logged in).
- **Repository moved (2026-11-15):** the old account `arifulcrypto` is suspended (appeal filed, answer
  expected in 3–5 business days). Development continues at **`zucchinistockton/24goods-web`** (private; remote
  `origin`; the dead remote is kept as `old`). Pushed: `main` (`19c7625`), `feat/v12-procurement`,
  `feat/v12-procurement-m2`. PR #1 moves the owner/image references (`ghcr.io/zucchinistockton/...`), PR #2
  is the M2 draft. Old PR/issue numbers (#1–#89) refer to the old repository and are not migrated.
  **Not done yet:** branch protection on `main` (needs a paid plan while the repo is private); Dependabot and
  `ENABLE_CODEQL` settings; a GHCR token from the new account on c4 (`~/.secrets/ghcr_token` is the old,
  dead PAT) — **production cannot pull new images until that exists**; the demo updater's token likewise.
  PR events did not start CI on the first PRs; `gh workflow run CI --ref <branch>` does.
- **ZovoCard sandbox (account valid until 2026-11-18 10:57): probe green and one CDK scenario verified**
  (issue, same-key replay, void; spec §12). Key in `~/.secrets/zovocard.env`. Open: webhook test (needs the
  sandbox signing secret, the demo stack running and M2 deployed there), the remaining [I] items of §14.3.
- **Checked 2026-11-15:** production (c4) serves normally on the old images `ghcr.io/arifulcrypto/...:main`
  (containers up 4 days, disk 82 %); the `rollback-*` images were pruned, so **there is no local rollback
  image: tag the running images before the next deploy**. c4 has `gh` logged in as the new account but no
  Docker registry login; `prod.sh` logs in transiently with `~/.secrets/ghcr_token`, which is still the dead
  token (a new one needs `read:packages`). **No image exists under the new owner yet:** on the new repo only
  `workflow_dispatch` starts CI (0 push / pull_request runs, also for the merge of #1), and the GHCR push job
  runs only on a push event to `main`. Likely an account-level restriction (inference): verify the account
  email, then push to `main` again; otherwise allow the push job on a manual run. The demo stack on the dev
  box is stopped (exited 2 days ago). Repo-local git identity now `zucchinistockton` (noreply address).
- **Branches (local):** `main` is 2 behind `origin/main` (`19c7625`, which already contains pay-ux #88;
  `fix/pay-ux` has no diff against it and can be deleted). v12 M1 is merged into **`feat/v12-procurement`**
  (#89, `a00b58e`), **not into `main`**. M2 lives on `feat/v12-procurement-m2` in the worktree
  `.claude/worktrees/agent-af3aa371765135692`.
- **v12 M2 state (updated 2026-11-15):** draft PR #2 on the new repo; **full CI green** (run 43674783330:
  backend suite incl. `tests/test_procurement_m2.py`, frontend, Semgrep, gitleaks, images, E2E smoke). Fixed
  on the way: 0024 lacked an UPDATE policy for the outbox claim (`procurement_claim`; `SELECT ... FOR UPDATE`
  must pass one, so the engine saw no work), a late non-terminal webhook status no longer replaces
  `consumed`/`disabled`, four test corrections. `68ca6df` is still a WIP snapshot: squash before merge.
  **Still open for M2 (spec §10), verify each against the code before building:** admin-password confirmation
  for production suppliers, procurement + `mock_zovocard` in `scripts/dev.sh` and the e2e smoke, manual
  fulfil/void/switch, cost report, redemption site; then security-reviewer + payment-auditor. PR #3 (email
  redesign, CI green) touches `app/core` and waits for the security-reviewer pass before merge.
- **Done this session:** `app/suppliers/gpt_session.py` (`0bfcd4e`) — offline pre-check of a pasted ChatGPT
  session, 18 unit tests green, not wired to any endpoint yet. Live checks (spec §15): test session valid,
  ipcook SOCKS5 exits work. Credentials saved: `~/.secrets/gpt-test-session.json`, `~/.secrets/ipcook.env`.
- **Queue (owner, 2026-11-15), each its own PR, none started:**
  1. Dark mode for the buyer app and console, following the system theme (`prefers-color-scheme`); validate on
     the demo.
  2. Optional TOTP 2FA (`feat/totp-2fa`, spec §13).
  3. Telegram notifications: English by default, language (en / zh-CN) selectable in the admin console,
     texts through i18n.
  4. Email templates: redesign the outgoing emails (clearer subjects, formatted HTML body with a plain-text
     part, key information first). Owner confirmed 2026-11-15: a design/content upgrade, no new behaviour.
  5. Second GPT supplier adapter built on the open-source payment protocol
     (`crhywun/gpt-subscribe-studio-1`; owner fork `arifulcrypto/gpt-subscribe-studio-dev`, local `/opt/gss/src`)
     plus the browser-session-injection path, as a backup to ZovoCard. **Future work, parked** (owner,
     2026-11-15: payment instrument and billing countries undecided; spec §15).
  6. ~~ZovoCard sandbox probe~~ done (see above).

**v12 procurement M1 — built (2026-11-14, branch `feat/v12-procurement-m1`, ADR-038, migration
`0023_procurement`):** `app/suppliers/` (protocol, ZovoCard read-only client with the status/error-code
table, mock + manual suppliers), `app/procurement/` (health loop, internal probe API on :8004, CLI
`python -m app.procurement probe --supplier zovocard --env sandbox`), compose profile `procurement` with its
own env file / egress network / DB role / Redis user, admin **Suppliers** page with "Test connection",
`app/devtools/mock_zovocard.py`. No order reaches a supplier yet. Next: sandbox key -> run the probe (spec
§12), then M2. Existing hosts: add `APP_DB_PROCUREMENT_PASSWORD` to `env/migrate.env` (or `ALTER ROLE
app_procurement LOGIN PASSWORD ...`), generate `env/procurement.env` and the API's `APP_PROCUREMENT_URL` /
`APP_PROCUREMENT_TOKEN` before enabling the profile (bootstrap does this for new hosts).

**v12 procurement — in progress (2026-11-14, branch `feat/v12-procurement`, spec
`docs/review-v12-procurement.md`, ADR-038):** multi-supplier procurement and upstream fulfilment
(`upstream_cdk` / `upstream_vcc` / `upstream_direct`), first supplier ZovoCard. New process
`backend/app/procurement/` (egress, vault public key only) and adapter layer `backend/app/suppliers/`;
delivery keeps its boundary. Production key verified read-only (allowlist set, balance/plans/products OK);
sandbox waits for a support ticket, so development runs on the mock supplier. Vendor docs and response
samples are local only: `docs/research/zovocard/` (git-ignored, public repo). Secrets:
`~/.secrets/zovocard.env`. **No production write calls and no real funds before the security review.**
Order of work: M1 (adapter + probe + process + admin Suppliers page) -> M2 (CDK, webhook, ledger, manual
ops, redemption site) -> M3 (VCC, 3DS, direct top-up). Side PRs: `fix/pay-ux-reco` (USDT/Alipay pay UX,
exclude-from-recommendations flag), `feat/totp-2fa` (optional TOTP, spec §13). Status of each: see the
spec's §10 and the open PRs.
**Production deploy 2026-11-15: `d881dfa` (pay-page fixes #88, email redesign #3; no migration).**
Images `ghcr.io/zucchinistockton/goods24-{api,web}:main`; c4 `.env` now points at the new owner (backup
`.env.bak-20261115`). Rollback images kept on c4: `goods24-api:rollback-222b03b`, `goods24-web:rollback-222b03b`
(to roll back: set `API_IMAGE` / `WEB_IMAGE` to them, `compose up -d --wait`). Deployed by hand (sync, pull,
migrate, up) because `prod.sh deploy` logs in with `~/.secrets/ghcr_token` (dead) and then logs c4 out of its
working registry login: fix `prod.sh` or replace the token before using it again. `prod.sh smoke`: all pass.
`push` and `pull_request` events start CI again on the new repository (seen on #3 and on `main`).
PR #3 security review: pass, two low findings fixed (a code can no longer reach a subject; test send brand).

**Email redesign (2026-11-15, branch `feat/email-design`):** every outgoing mail (verification code, password
reset, order paid, order delivered, and the admin test send) is now a complete HTML document with a
plain-text part: site-name header, the admin-editable body, one focal block (the code in a large box; for
orders a summary table — order number, item, amount, status — and a "View order" button), a safety note and a
footer; light/dark via `prefers-color-scheme`, table layout with inline styles, no remote assets. New default
subjects and bodies in en / zh-CN (codes are not placed in subjects). Templates stay editable in Settings;
stored values that equal the previous built-in text follow the new defaults (`_LEGACY`), real edits are kept.
No migration, no API change. `tests/test_email_layout.py`. Not done: a rendered preview in the console.

**Production deployed 2026-11-10 16:24 UTC: `222b03b`** (v11 #82 + #83 + #84, migrations 0020–0022) from
`5f709d3`. Pre-deploy backups (v1 script, sha256-verified off-host copies, `pg_restore --list` OK);
rollback images `goods24-{api,web}:rollback-5f709d3`. Smoke all pass; order / payment / card / ledger
counts identical to the pre-deploy baseline (ledger sum 0); FORCE RLS intact on the new and touched
tables; the existing service record got ref `SV-3EUADH`.
**Incident (same day, ~2–3 min downtime):** the first attempt (`cd90d33`) failed at 0022 — FORCE RLS
also binds the migrate role, so the `ref` backfill saw no rows and `SET NOT NULL` failed; compose had
already removed the running api/web, so the site returned 521 until api/delivery/web were rolled back
to `5f709d3` (DB at 0021, additive; data unchanged). Fixed in #84: data steps run with FORCE lifted
inside the transaction, a regression test migrates a DB with pre-existing rows, and `prod.sh deploy`
now runs the migration as a one-off **before** recreating containers. Rule for future migrations:
any data backfill on a FORCE-RLS table must lift FORCE (see 0022 `_owner_sees_all_rows`).
Follow-ups (owner): Cloudflare rewrites `Cache-Control` to a 4 h browser TTL (`/sw.js` is `no-cache` at
the origin; harmless for service-worker updates); old rollback images on c4 can be pruned (disk 87 %).

**v11 features (2026-11-10, branch `feat/v11-features`, PR #82, spec `docs/review-v11-features.md`,
ADR-037, migrations `0020_product_variants`, `0021_chat_v11`, `0022_v11_content_ops`):** product
variants (own price / floor / stock / terms, disjoint stock pools, variant quotes, move pre-variant
cards); Telegram-style chat (replies, vertical context menu, sticky pinned bar, sender edits with
audited history, typing indicator; "Quote in draft" removed); buyer chime + desktop notifications
(admin defaults) and console notifications through a service worker (work while minimised, default
on); announcements (unread badge, list, mark all read, one-time prompt); manual service records
without order/customer (`SV-XXXXXX` ref, reminders); pin + drag sort for products and categories with
a default storefront sort; 16 generic SVG avatars; recently viewed moved from Home to My account.
Cold payment-auditor, security-reviewer and i18n-mobile-reviewer passes: no Critical/High; all
Medium/Low findings fixed in the PR. Not built: Web Push with the browser closed, per-variant images,
two-dimension option matrices.

**Build toolchain (2026-11-10, branch `fix/node-lts-typescript-pins`, supersedes Dependabot #74/#78):**
web image build stage and CI on **Node 26 Active LTS** (EOL 2029-04-30) instead of 22; pnpm installed
with npm from `packageManager` (Node ≥ 25 images have no Corepack); **TypeScript 6.0.3** (newest that
vue-tsc 3.3.14 supports; TS 7 waits for vue-tsc). Dependabot ignores `node` 27.x until it is LTS
(2027-10-20) and `typescript` 7.x until vue-tsc supports it. Build tooling only: the runtime `caddy`
stage and the app code are unchanged, so no deploy is required for it. Dev box still runs Node 20 (EOL):
install Node 26 there when convenient.

**Production deployed 2026-11-10 UTC: `5f709d3`** (owner-approved) from `5fb2575`: Dependabot #75
(vue-i18n 11.4.17), #76 (vitest 5.1.0, dev) and #77 (@types/node 26, dev). Frontend dependencies only: no
backend change, no migration. `scripts/prod.sh smoke` all pass; api/delivery/web report revision
`5f709d3`. **No pre-deploy backup was taken for this deploy** (the remote backup command was not
permitted in that session; latest sets: `predeploy-20261110T045019Z` and the nightly). Rollback: pin
`API_IMAGE`/`WEB_IMAGE` to the `:5fb2575e53a16c246431641e0425ea0927030d2f` tags (docs/production.md
"Rollback"). Not merged: #74 (node 25 image has no corepack, web image build fails; 25 is not an LTS
line) and #78 (TypeScript 7 breaks vue-tsc: `./lib/tsc` is no longer exported). c4 root disk is at 86 %.

**Production deployed 2026-11-10 04:52 UTC: `5fb2575`** (#73, issue #72 remediation, migration
`0019_attempt_closed`, owner-approved) from `1528264`. Pre-deploy backup with the installed v1 script,
sha256-verified off-host copy, `pg_restore --list` OK (41 tables). Rollback: retag
`goods24-{api,web}:rollback-1528264` to `:main` and `up -d` (0019 is an index only; no DB restore
needed). postgres/redis were recreated once for the digest pins (same images). Smoke all pass; order,
payment, attempt and ledger counts identical before/after (ledger sum 0); job passes succeed under the
new lock; API -> BEpusdt 200. Follow-up: the origin sends a strong ETag for the SEO HTML but
Cloudflare strips it unless the zone's "Respect Strong ETags" is enabled (owner decision).

**Issue #72 remediation (2026-11-10, branch `fix/issue-72`, one PR):** every held order and payment
attempt now has an exit (ADR-035): USDT refusal no longer locks the order (B1); admin "release hold"
accepts a verified payment or closes an unpaid held order after a fresh re-query (B2); attempts reach
a terminal `closed` state (B3, migration `0019_attempt_closed`, additive); a second trade on a refunded
order alerts staff (S12); redirect gateways are healthy only after a real payment/self-test (A3); dead
delivery jobs alert staff and the Operations overview shows open attempts and paid-undelivered
auto-card orders (A4); the periodic jobs take a Redis lock (A1); email change needs a code sent to the
new address (S6, ADR-036); tenant-scoped primary-key lookups + Semgrep rule (S10); ledger property and
concurrent-settlement tests (A8); clearer buyer copy for held orders (B4). Supply chain (S4/S11):
SECURITY.md, CODEOWNERS, secret scanning + push protection, Dependabot alerts/security updates (npm,
docker, docker-compose ecosystems added), private vulnerability reporting, CodeQL enabled
(`ENABLE_CODEQL=true`, free while the repo is public — turn it off if it goes private), Semgrep image
and all base images pinned by digest, `pnpm audit --prod` blocking at high.
**Still open (owner):** S1 independent review, S2 backups/restore drill (#28), S3 credential rotation
(#30), S5 public vs private repository. Not done in code: S7 (seal buyer input / manual delivery
text), S8 (staff TOTP), S9 (separate media origin), A5–A7, A9 — see #72 for the reasons.

**Production deployed 2026-11-09 01:30 UTC: `a8160cb`** (#50 + #51, owner-approved) from `e1fb9e7`;
no migrations. Pre-deploy backup with the installed v1 script, sha256-verified off-host copy in
`/root/kesiv-backups/predeploy-20261109T012730Z/`, `pg_restore --list` OK (40 tables). Rollback:
retag `ghcr.io/arifulcrypto/goods24-{api,web}:rollback-e1fb9e7` to `:main` and `up -d`. Smoke all
pass; api->BEpusdt 200. Demo also on `a8160cb`.

**Gateway egress (2026-11-09, `feat/egress-sticky-relay`):** ZPay API calls pick the path per gateway
host in the app — sticky last-success between direct and a configured relay (`APP_ZPAY_RELAYS`),
fallback only on connect failures; replaces the c4 DNAT + 5-minute DNS timer.

**Several EPay gateways (spec v10, `feat/epay-gateway-routing`, migration `0018_gateway_routing`):**
priority + sticky failover across enabled gateways (`ZPayRefused` / `ZPayUnreachable` fail over,
ambiguous outcomes do not), admin Settings -> Payment priority list / enabled switch / routing mode
/ last OK + failure, and a test-payment dialog with a server-rendered QR (`payment_tests`, ledger
`test_receipts`). See ADR "several EPay gateways" in docs/architecture.md. Not deployed yet.

**Production deployed 2026-11-09 08:22 UTC: `84b4dc0`** (#64, migration 0017). **Xiaodou EPay:** c4's route
into mainland China is lossy (two-way tests vs a Tencent GZ VPS); API traffic now goes through a
TLS-passthrough relay on the dev box (docs/production.md "Xiaodou EPay relay"); the ZPay client
now parses Xiaodou's 4xx JSON replies (404 = order not found) instead of failing them.

**#58 quick items (2026-11-09, `fix/issue-58-quick`):** policy pages name no governing-law
jurisdiction (owner: no Singapore for now) and carry no contact address; `SiteFooter`
("Kesiv · Contact: support@kesiv.com") is the single place, now also on article pages. USDT order
X Layer -> Arbitrum One -> TRC20. Admin Orders: payment-method column + filter (`channel`, `none`).
Unpaid-order cap default 2; order creation throttled per account (3/60 s) and per IP (10/300 s,
looser for carrier NAT) on buy-now, checkout and quote accept, with one high-severity
`risk.order_flood_detected` audit row per identity and window (`orders/riskctl.py`). Declined:
EPay amount jitter (see #58 comment).

**Production deployed 2026-11-09 04:00 UTC: `42de235`** (#53 policy pages/help + #56 subscriptions,
migration 0014; owner-approved). Backup `/root/kesiv-backups/predeploy-20261109T035854Z/` (sha256
match, 40 tables); rollback images `:rollback-a8160cb`. **Cloudflare Redirect Rules (#54):**
`kesiv.com`/`www` -> `https://shop.kesiv.com{path}` (302, query kept) and `http://shop` -> https
(301). HTTP->HTTPS lives at Cloudflare, so the origin's `http_redirect` from #53 was reverted
(a plaintext hit on :8088 fails again, as `prod.sh smoke` expects).

**Integrity v8 (2026-11-09, `fix/integrity-v8`, spec `docs/review-v8-integrity.md`, issue #59 A-D/F/G):**
auto-card stock is reserved per order (no oversell of the last card), admins can requeue a held
order or refund a paid-undelivered one, buyers can report `not_received` after the delivery ETA;
every refund needs a reference (the staff confirm the money went back) shown on the order;
subscription refunds take back only the refunded periods; open after-sales tickets hold reseller
markup (`disputed_minor`); `orders.terms` snapshots purchase terms; manual stock release returns
only what was reserved (`orders.stock_reserved`). Migration `0015_order_integrity` (additive).
Not built: provider refund APIs (#55), vaulting manual delivery text (#59 E / #8).

**Subscriptions v7 (2026-11-09, `feat/subscriptions`, spec `docs/review-v7-subscriptions.md`):**
products can be marked as subscriptions (`products.subscription`); after-sales defaults to one
calendar month (`products.after_sales_months`, tenant `after_sales_default {unit,value}`; legacy hours
still honoured); job `sync_subscriptions` turns delivered subscription orders into services (new or
renewed, audited as `system`); Services page shows order no./product/buyer/source/days left,
renewal candidates and a date picker instead of pasted UUIDs; buyers see their service period on
the order. Migration `0014_subscriptions` (additive). Not built: buyer self-service renew, recurring
charge. Follow-up: (refunded renewals now shorten the service, see Integrity v8); with two open services for one buyer+product the job
skips the order and logs it for staff.

**Policy pages v2 + help center (2026-11-09, `docs/legal-help-sg`):** Terms / Privacy /
After-sales / Platform rules rewritten (venue positioning, Singapore law, generic payment and
sign-in wording, no hosting/vendor details; footer = brand + law + support@kesiv.com), help guide
rewritten and 6 general help articles added (EN + zh-CN); Help page translates category names,
reloads on language change, links the policies; site footer on Help/Me. Prod Caddy answers
plain-HTTP hits on :8088 with a 308 to https (was a 400 from Cloudflare for `http://shop.kesiv.com`).
See `docs/legal/README.md`.

**Legal text (2026-11-09, `docs/legal-terms-privacy`):** real Terms of service + Privacy policy
replace the template banner (#48 §5, #40); see `docs/legal/README.md` for the approach and the
open governing-law decision.

**CI/local speed-up (2026-11-09, `ci/faster-pipeline`).** Measured on its own PR run: backend tests 616 s -> 107 s. Before: ~16 min, backend pytest
616 s on the critical path, then image builds waited on it (web 119 s, mostly a single-threaded
xcaddy compile), then E2E. Changes:

- `create_app` reuses a once-per-process analysed route table (`_route_table`). FastAPI's
  `include_router` re-analysed every endpoint (~0.5 s) and the suite builds one app per test:
  the same 41 tests went 40.8 s -> 15.0 s locally. Each test still gets its own app/middleware.
- Images build in parallel with the tests (nothing is pushed until `publish`, which now needs
  backend + frontend + e2e + semgrep + gitleaks), with a buildx GHA layer cache; the web
  Dockerfile installs dependencies from manifests before copying sources. A weekly
  `OS_PATCH_EPOCH` build arg keeps cached `apk/apt upgrade` layers from going stale.
- A `changes` job skips backend/frontend/image/E2E work a PR does not touch (pushes to `main`
  always run everything). The image matrix skips per step, never at job level, so its
  per-matrix required checks still report.
- Backend installs with uv (`uv pip install --system`), pytest prints `--durations=15`.
- `scripts/dev.sh check`: changed-files-only local loop under `.dev/heavy.lock`.
**Issues #48 / #49 triage + fixes (2026-11-09 UTC, `fix/issues-48-49`).** #49 is the more accurate
review; where they disagree #49's reading of the code was right. Fixed, with regressions:

- **49 A** cart `buyer_input` (may hold a session token) no longer touches `localStorage`: only
  ids/qty/ticks persist, legacy values are purged on load, and inputs are dropped on sign-out or an
  account switch (guest -> signed in keeps them: same person, same tab).
- **49 B** list summaries now carry `buyer_input` (null = off), so Home/Search quick buy shows the
  form; BuySheet treats a missing field as unknown and fetches the product before allowing submit.
- **49 C** one stock rule (`lib/stock.ts`) for row/card/detail/quick buy: null unlimited, 0 sold
  out, positive caps quantity -- manual stock 0 no longer shows an enabled Buy.
- **49 D** catalog feed, Home strips/categories and My Account's recent strip refetch on a language
  switch; responses from the old locale are discarded.
- **48 §1.1** Vant's icon CSS `//at.alicdn.com` woff fallback is stripped at build time and the
  build now **fails** if any emitted CSS loads a remote `url()` or mentions alicdn (both SPAs).
- **48 §1.2** a 428 on a non-catalog route (My Account, chat, cart) shows an inline access banner
  instead of a silent failure; reads retry after clearance. No blanket gate over recovery routes.
- **48 §2.1/2.2/2.3** one manual-delivery badge + one note per card (BuySheet badge only); admin
  Services page padding; `/me/settings` (language, media prefs, credentials, delete account; closes
  #38) and zh `自动（浏览器）` -> `自动`.

Not done, deliberately: SSE `withCredentials: true` (same-origin EventSource already sends
cookies; #49 is right); Turnstile `connect-src data:` (unverified console line); random payment
tail and USDT rate buffer (no provider evidence; need a pricing decision, AGENTS §1.3); legal
banner removal (needs approved text, #40); agent skill (exists: `skills/24goods-admin`). Image
thumbnails for product grids are a real win but need the public-read vs CPU-admission split #49
describes -- separate change. **Owner:** disable Cloudflare Web Analytics automatic setup for
shop.kesiv.com in the dashboard (our API token has no RUM scope); do not whitelist the beacon.

**USDT receiving wallets replaced on production (2026-11-09 00:36 UTC, owner request).** BEpusdt on
c4: Arbitrum One and X Layer -> `0x5bde040754d531a6c25027845adc99b2d8100741`, TRON TRC20 ->
`TTAWAWPXTouM5Pztv6Tu5dhqFZXJtSh1N2`. The owner supplied X Layer as `XKO5bde…`; `XKO` is OKX's
display prefix for the same EVM `0x` address. Done with zero waiting BEpusdt orders; SQLite
backup first (`/opt/kesiv/backups/bepusdt-prewallet-20261109T003615Z.db`). The old wallets
(`0xe2dc…653c`, `TQkeo…vMu5`) are no longer watched -- any late transfer to them must be
reconciled by hand.

**v6 blockers closed + owner UX round (2026-11-08 UTC).** Read [HANDOFF-v6.md](HANDOFF-v6.md) for the
implementation inventory. Since that checkpoint, on `feat/issues-25-30-hardening`:

- Both Medium re-review blockers are fixed with regressions that fail without the fix:
  USDT-only cancellation no longer falls through to an unconfigured legacy EPay gateway (the legacy
  key, not the current channel, gates the drain, so an old EPay URL stays drainable after a switch);
  and a durably committed settlement now travels with unresolved evidence
  (`UnresolvedEvidence.settled`) so its broadcast and duplicate-payment alert still fire while
  cancellation keeps failing closed. Admin Operations shows translated event families.
- Owner UX round: four-tab storefront (Home / Messages / Cart / My Account) with Orders moved under
  My Account plus `?tab=` deep links; presence defaults to busy for support and offline for admin
  (never online); CS quick replies (built-in bilingual starters + tenant-scoped, RLS-enforced
  `cs_reply_templates`, personal vs admin-only shared); staff browsing the shop are told which
  account is in the way and sent to the console inbox instead of buyer chat (`/chat` is buyer-only).

Read-only security-reviewer and payment-auditor passes were run on those commits; both found the
first cut of the cancel fix **fail-open** (it cancelled over an unverifiable legacy pay URL) and
both findings are fixed, with regressions that fail against the fail-open version:

- `cancel.py` now gates on the payable's own channel: with no usable legacy key an EPay payable
  raises 503 (fail closed) and only a USDT payable cancels. While the key is present the legacy
  query still runs for USDT, so an EPay URL issued before a switch stays drainable.
- `reconcile.py` now carries committed outcomes out of **every** post-commit failure (`_carrying`),
  not only the unresolved/unavailable flags, and no longer re-queries an already-settled original
  (which replayed the duplicate-payment audit trail and re-broadcast on every tick).
- `cancel_group`'s emit is best-effort so a failed broadcast cannot turn the 503 into a 500; a
  teammate's private reply template answers 404 rather than confirming its id.

**The reviews above were NOT independent** (same session wrote and reviewed). A cold-started
re-review on Opus 5.5 (2026-11-08) found more on the money path, which `sec/legacy-pay-url-marker`
fixed in **#44 (`ad14f01`, merged)** after full CI and a second cold auditor pass that found no remaining cancel-over-money path (ADR-027):

1. Cancel inferred "could a legacy URL exist?" from the current channel / key / provider history.
   Fail-open after a channel switch, a provider cutover, or full de-configuration (#32); and a
   fail-closed false positive -- every EPay cancel 503'd for a tenant on durable providers with no
   env key. Fixed with a durable `legacy_pay_urls` record (migration 0013, backfilled).
2. Reconciliation carried committed outcomes only through `Unavailable`; any other exception, or a
   `wait_for` timeout, lost the broadcast and duplicate alert permanently (#33). Now carried
   through every exception and collected in a caller-owned sink.
3. The attempt was marked settled only after the settlement committed, so a crash in between
   replayed the duplicate audit row and alert. The mark now rides in the settlement's commit.
4. Merging outcomes kept one duplicate trade, dropping a refund alert when three originals paid.

Not affected: **production** (`7f36926`) predates all of this code -- the defects were never live.

Verified on the demo after deploying `ad14f01` (migration `0013`): FORCE RLS restored on `orders` and
`checkouts`; `legacy_pay_urls` RLS-forced, `app_api` = `INSERT, SELECT` only; as `app_api` an insert
is idempotent, other tenants see nothing, `UPDATE`/`DELETE` are refused, a cross-tenant insert
violates the policy. **The backfill processed zero rows there** (the demo had no pending payables), so
it is first exercised for real on production: record the pending-payable count before deploying and
check it equals the marker count after.

Also 2026-11-08: the demo had SEO indexing **on** (mock products and a mock gateway indexable on the
owner's domain); turned off in the DB and it survives redeploys (`seed` never touches it). robots.txt
now refuses training crawlers only; AI search indexers and user-triggered fetchers are allowed (#43).

**Production deployed 2026-11-08 18:42 UTC: `e1fb9e7`** (owner-approved), from `7f36926`, applying
migrations `0010`-`0013`. Pre-deploy backup was taken with the installed v1 script, validated with
`pg_restore --list`, and copied off-host with matching sha256 to
`/root/kesiv-backups/predeploy-20261108T181022Z/`. Rollback images are pinned on c4 as
`goods24-{api,web}:rollback-7f36926`; all four migrations are additive (new tables only), so
**rollback = retag those images, no DB restore**. Verified: revisions `e1fb9e7`, alembic `0013`,
FORCE RLS on all payment tables, `scripts/prod.sh smoke` all pass, `via` gone + CORP present
(`Caddyfile.prod` had drifted from `Caddyfile` -- fixed in #46), `/ai.txt` live, robots refuses
training crawlers only, api->BEpusdt HTTP 200, all 14 background jobs succeeded post-deploy.
Prod had **zero** pending payables, so the 0013 backfill processed no rows (as on the demo).

**`main` is branch-protected** (7 required checks, enforced for admins, linear history). Merge with
`gh pr merge --squash --auto`.

**Backup trap (fixed in code, not on c4):** c4 still runs the **unencrypted v1** backup script from
cron; `deploy/backup.sh` is v2 (age) and c4 lacks `age` and both recipient files. `prod.sh backup` /
`install-backup-cron` used to overwrite the working cron script with v2 and silently break nightly
backups; `install_backup_script` now refuses until c4 is ready. Installing age + recipients is the
owner-only part of #28 -- until then backups contain plaintext secrets.

**Next (owner):** the small live USDT test transfer on https://shop.kesiv.com (now on current code);
#28 age recipients; #30 rotate the Xiaodou secret/PID.

**Merged and deployed (2026-11-08 UTC).** The above landed as PR #31 (`b2e7a1b`) with full CI
green; two follow-up PRs merged on top:

- **#37 → `4d079c9`** — container memory was reported as "Unknown / Unknown" because the overview
  collector read only the cgroup **v2** paths and this host is still on **v1** (older LXD, #6). Now
  falls back to the v1 hierarchy, treats v1's `LONG_MAX` "unlimited" sentinel as unknown rather
  than an ~8 EiB budget, and keeps v2 precedence. Verified live in the running container: **161 MB
  / 192 MB**. Also: the admin Operations page had no horizontal padding because its root carried
  only `.operations`, not the shared `.page` class.
- **#41 → `fed2927`** — the repository had **no LICENSE at all**. Adds a proprietary
  all-rights-reserved LICENSE (prohibited uses, machine-learning use, payment integrity, logging
  as evidence, good-faith security-research carve-out), `/ai.txt`, `robots.txt` refusal for 20
  training/answer-synthesis agents (search crawlers deliberately still allowed, with a test), a
  one-time console copyright notice in both SPAs, and `docs/legal/README.md`.
- **#42 (open)** — a 422 showed only "Please check your input" while the API was already sending
  `"<field>: <constraint>"`; `toastError` discarded it. Now appended, for the `validation` code
  only, so log-oriented prose from other codes cannot reach a toast.

**Deliberately not built:** DevTools detection/blocking, requested by the owner. Everything shipped
to a browser is already on the visitor's machine and `curl` fetches the bundle without executing
any defence, so it stops nobody who is actually copying the work while breaking assistive
technology, hanging some mobile browsers, resembling malware to scanners, and locking staff out of
diagnosing a customer's payment problem. Reasoning in `docs/legal/README.md` and `notice.ts`.

**Also deliberately not built:** VPN/proxy/VPS and "genuine location" detection (#39). The console's
existing wording — IP and headers are untrusted hints, location is not inferred — is correct and
should not be weakened into a verdict staff act on.

**Still not established:** the independent pentest before real money (M1 gate) has not happened,
and the owner has stated an intent to make a small live USDT transfer anyway. No key rotation. The
Xiaodou merchant secret and PID should still be treated as exposed (#30).

_Last updated 2026-11-08 UTC._

- *(Superseded 2026-11-10: production has since been deployed several times and runs
  `1528264` / migration `0018`, see "Current state" at the top.)* **Prod** https://shop.kesiv.com (c4, IIJ Japan) ran **`7f36926`** (v5.2 + #23), verified
  2026-11-08 via `scripts/prod.sh status`. That was **15 commits behind `main`**, missing migrations
  `0010_payment_attempts`, `0011_operations_renewals`, `0012_cs_reply_templates`. `0010` matters
  most: `PaymentAttempt` is the durable attempt binding the USDT reconcile path depends on, so real
  USDT testing on prod today exercises v5.2, not the current pipeline. **Real BEpusdt v1.24.3 is
  already running there** (compose profile `usdt`) with the owner's three wallets configured —
  (replaced 2026-11-09: Arbitrum One and X Layer share `0x5bde…0741`, TRC20 is `TTAWA…1N2`). Nothing to configure; the
  gap is the deploy. c4 has ~1.8 GB RAM free vs this box's ~180 MB.
- **Demo** https://demo.kesiv.com runs `:main` — updated **manually** via
  `/opt/24goods-demo/deploy/demo-update.sh`; there is **no** `goods24-demo-update` systemd timer on
  this host (checked 2026-11-08), so it only moves when invoked. `seed` is additive — no deletes —
  but it writes an agent status only when none exists, so a stale seeded row persists across
  deploys (the demo's support agent was left reading "online" until corrected directly in the DB).
  USDT here is the **simulator** `mockusdt`, by design; real wallets stay on c4 so two services
  never watch one address set.
  Logins: `/opt/24goods-demo/deploy/secrets/demo-credentials.txt`.
  Demo config set 2026-11-07: rate limit 120/60 s, Telegram on (owner chat), Alipay + 3 USDT channels on
  (USDT = simulator `mockusdt`; env in `deploy/env/{api,mockusdt}.env`), SEO indexing **off**.
- **Done 2026-11-07** (all with security-reviewer / payment-auditor / i18n-mobile-reviewer passes, findings fixed):
  - v5.1: §1 staff reprice (#11), §2 storefront clarity (#12), §6 product auto-fill + inline cards (#13).
  - v5.2 (`docs/review-v5.2.md`): §1 staff access — admin login Turnstile, staff IP allowlist, no 429 for
    staff (#14); §10 Telegram notifications (#15); bilingual README + Cloudflare plan (#16); §7 USDT via
    BEpusdt, ADR-023 (#17); IP/user blocking (#18); reseller subdomains + custom domains app side (#19);
    §2 manual stock default 1 + §3 Ctrl+V paste (#20); §5 agent API tokens + Claude skill (#21);
    §6 SEO crawler HTML, sitemap, robots, 404, ADR-024 (#22).
- **Owner decisions / actions open:**
  1. Cloudflare changes for reseller subdomains + custom domains: `docs/cloudflare-reseller-domains.md`
     (proposal, not applied). Then: Settings → Resellers → custom domains (zone id, token with
     "SSL and Certificates: Edit").
  2. Prod domain: apex `kesiv.com` vs `shop.kesiv.com` as the main host (the other 301s).
  3. Real USDT: start BEpusdt on prod (profile `usdt`), set the Arbitrum/X Layer/TRC20 addresses in its
     console, copy its token into api.env (docs/production.md). Independent pentest before real money (M1 gate).
  4. Staff IPs for Settings → Security → staff IP allowlist. Telegram bot token rotation is the owner's call.
  5. Cloudflare 5-second challenge / IP-list sync for blocked IPs: Cloudflare settings + token permission.
- **Next (code):** v5.2 §8 optional entry check (Turnstile on entry), category pages for SEO, Telegram for
  manual-delivery completion, persisting USDT trade ids on the order (cancel-time re-query without Redis).
- **Security/payment review 2026-11-08 UTC (analysis only, fixes pending; [issue #26](https://github.com/arifulcrypto/24goods-web/issues/26)):** see
  [source findings and acceptance criteria](research/security-payment-audit-2026-11-08.md) and
  [upstream research](research/security-payment-references-2026-11-08.md). Priority: BEpusdt native
  callback HTTP 200 failure acknowledgements suppress retries; sensitive order-input copies in chat
  escape retention; busy SSE streams defer session revocation checks. Also track OTP atomic consumption,
  challenge configuration/action binding, order-input editing, resource admission budgets and durable
  payment attempts. This review does not satisfy the independent pentest gate; no business code changed.
- **Working rules:** AGENTS.md §7 (2 GB box: full suite in CI only, ≤ 2 agents, dev stack down when idle).

### Original follow-up requirements (see implementation status below; 2026-11-08 UTC)

Tracking: [#27 payments](https://github.com/arifulcrypto/24goods-web/issues/27),
[#28 operations/recovery](https://github.com/arifulcrypto/24goods-web/issues/28),
[#29 service reminders](https://github.com/arifulcrypto/24goods-web/issues/29).

- [v6 payment/operations proposal](review-v6-payments-ops.md): API-direct QR with explicit provider
  capabilities and truthful domain/privacy limits; immutable provider/attempt configuration; active
  ownership/permission/settlement cross-checks; optional server-enforced payment challenge with bounded
  self-hosted difficulty; callbacks/recovery stay interactive-challenge-free.
- Lightweight server-admin overview, structured operation/login/system/task/payment events, trusted
  proxy IP and approximate location/browser metadata, bounded alerts, key/software review reminders
  and optional static canary detection; no root panel or Docker/LXD socket in the API.
- [New source observations B1–B5](research/payment-qr-ops-2026-11-08.md): backup script has no archive
  encryption/whole-set manifest/job lock, excludes BEpusdt state, and does not establish cross-store
  consistency/PITR/restore proof; edge query redaction also needs review. Actual remote setup is unverified.
- [GCSA/Web3/runtime research](research/web3-ops-risks-2026-11-08.md): original @GCSAHK reports,
  signed-intent/valid-credential abuse, token/chain/finality checks, and separate PHP2Shell/LXD/runc
  advisories. No current host/wallet compromise is claimed.
- Staff-managed monthly-service entitlements and email/TG expiry reminders, separate from unpaid-order
  expiry; no automatic recurring debit. Reminder scheduling is idempotent; external sends can repeat
  after an uncertain outcome, so use stable IDs and bounded retries.
- Documentation review caught and corrected the unsafe blanket restore restart and an exactly-once
  notification overclaim. Implementation and independent security review remain pending.

## Phase: **live MVP in production** (since 2026-11-06) → gate still open: **M1 independent security review** (#72 S1)

### Active owner-authorized implementation: issues #25–#30

- Feature branch `feat/issues-25-30-hardening`; PR must remain unmerged and no production deploy.
- #30 progress display (branch `feat/progress`): step model, supplier timeline and buyer/redemption progress, review-v12 §22, ADR-043 (migration 0034; no grant). Realtime: procurement publishes ids only on `rt:procprogress`, the API relays `order_progress`; **existing deployments must add `&rt:procprogress` and `+publish` to the `procurement` user in `users.acl` and reload Redis** (exact line in ADR-043).
- #30: owner-supplied Xiaodou v1.0.0 reference package reviewed/imported under
  `docs/vendor/xiaodou`; English index and integrity manifest; live merchant credentials excluded.
  Supplied credentials select the MD5 EPay compatibility contract, not the separate native RSA API.
- Core fixes implemented with targeted regressions: BEpusdt retry HTTP statuses, buyer-input chat
  references/legacy redaction, bounded SSE revocation/subscriptions, atomic OTP consumption, challenge
  fail-closed/action/host checks, optional input editing and upload admission. Order-field encryption
  remains separately tracked by #8; current input configuration is explicit, not snapshotted.
- EPay implementation: immutable encrypted gateway versions, fresh administrator authentication,
  explicit merchant-ownership acknowledgement, durable reservations, original-provider notify/query
  routing, bounded DNS-pinned HTTPS, typed local QR and reconciliation. Alipay is the enabled-method
  scope supplied by the owner; native RSA and unapproved payment methods are not inferred available.
- USDT: durable encrypted original-provider binding and address/amount/trade history, PostgreSQL
  recovery after Redis loss, fail-closed unknown creation/cancellation, receiver mismatch holds.
- Buyer/staff UX: backend-owned input capabilities, no synthetic input fallback, browser Auto locale,
  bounded sender-only 180-second recall, shared pins/copy/quoted draft, private low-data thumbnails,
  default upload compression and local quality preferences.
- Operations/services: platform-admin overview and sanitized events; staff service activation,
  versioned periods and verified once-only renewal, durable opt-in staff SMTP/TG reminders. No debit.
- Recovery: encrypted complete-set maintenance backup, separate vault recovery recipient, writer
  fencing, lock, manifest, atomic completion and verified offsite receipt; simulated failure tests.
  See [operations-v6.md](operations-v6.md). Operator tools include age (new operational dependency).
- Entry/payment gates are integrated. First security/payment/mobile review found concrete blockers;
  follow-up patches and regressions are recorded in [review record](reviews/v6-implementation.md).
  Final-revision CI and re-review are pending.
  No deployment, live provider credential use, real payment or production backup/restore was performed.
  See [implementation matrix](implementation-v6.md) for concrete completion evidence and limits.

## Done

- v8b (#58 §3-§5, `docs/review-v8b-cs.md`): every order attaches/creates the buyer's CS conversation (order card + system messages for all orders), staff alerts for order created/paid/cancelled/expired with per-agent toggles, payment-method tag in the order drawer, user and staff avatars (`PUT/DELETE /me/avatar`, `GET /avatars/{id}`, migration `0016_user_avatars`).
- v9 (#63, `docs/review-v9-payments-ux.md`): a gateway that is provably unreachable (DNS/connect error) fails the attempt cleanly (`channel_unreachable`, payable stays payable) while read timeouts stay `unknown`; admin `POST /admin/payment-attempts/{id}/resolve` (not_paid) for stuck attempts, `POST /admin/payment-gateways/{id}|bepusdt/test` self-test + `GET /admin/payment-gateways-health`, reconciler backoff (2 min first hour, then hourly) and a 24 h critical alert, checkout member orders carry the chosen channel, urgent desk/Telegram notice for paid manual orders + buyer `POST /orders/{ref}/expedite` (30 min, max 3), USDT confirmation tip, site-wide visit gate, announcement `display`/`require_ack` with per-user acknowledgement (`/me/announcement-ack`, migration `0017_payments_v9`). Declined: random Alipay tail (§4). Note: the tenant's manual delivery note says rush orders are not accepted; tenant data was not changed.
- v5 product import backend: `core/safefetch.py` (SSRF-safe, IP-pinned), `catalog/importer/` parsers + gmailbuy/maitg/ka8 adapters, `admin/importer.py` endpoints, offline fixture tests.

### 2026-11-05: M0 bootstrap
- Research by four parallel agents; evaluation in `docs/analysis.md`.
- Card vault, EPay verifier, CI security gates, AGENTS.md, project review agents.

### 2026-11-05: v2 review + MVP-0 build
- **Decisions** (`docs/review-v2.md`, ADR-009…014):
  - Static Vite SPAs instead of Nuxt SSR.
  - SSE instead of Centrifugo.
  - Built-in CS Inbox instead of Chatwoot (for now).
  - Voice calls dropped.
  - ZPay Alipay v1 first.
  - One domain.
  - Pull-based deployment.
- **Backend** (agent-built, orchestrator-verified):
  - FastAPI modular monolith with all MVP-0 endpoints from `docs/api-contract.md`.
  - Alembic schema: FORCE RLS, `app_api` / `app_delivery` roles with column grants, ledger Σ=0 trigger.
  - Separate delivery service: outbox, `SKIP LOCKED` card claim, internal reveal with circuit breaker.
  - Full ZPay notify pipeline.
  - Gated mock ZPay, seed data, keygen.
- **Frontend:**
  - `web` (Vant 4, mobile-first, EN/zh-CN): home, search, product, chat with product/quote/order cards, image and voice clips, orders, reveal, me.
  - `admin` (Element Plus): dashboard, products, inventory import, orders (price change, manual delivery), CS Inbox, ledger, audit.
  - 77 KB gzip initial JS.
- **Reviews run, findings fixed:**
  - *i18n-mobile-reviewer.* 4 Medium fixed: safe asset/pay URLs, contrast, focus ring.
  - *payment-auditor.*
    - FAIL #12 (timeout budget) fixed.
    - FAIL #13 (replay guard could lose a payment on crash) fixed.
    - Re-query trade_no/out_trade_no matching added.
    - Very late payments are now recorded.
    - Mock gated to dev/test.
    - https required for the query base in prod.
  - *security-reviewer.*
    - H1 (secret sprawl) fixed: per-service env files and Redis ACL users.
    - M2 (proxy IP) fixed: fixed edge IP set as the trusted proxy.
    - M6 (edge part) fixed: Caddy headers are defaults only.
    - App-side fixes M1/M3/M4/M5/M6/L1/L5/L8/L9: see the commit log.
- **Dependencies:**
  - Pillow pinned to 13.0.0 on Python ≥3.11 (GHSA-78g4-xqwv-q3g8 and 3 CVEs).
  - Uploads restrict decoders to the sniffed format.
- **Deploy:**
  - Compose stack sized for a 2 GB host.
  - Non-superuser schema owner; internal backend network with no egress.
  - `bootstrap.sh` generates all secrets.
  - `demo-update.sh` with a systemd timer.
- **CI:** backend tests against real PG and Redis; frontend checks; Semgrep; gitleaks (scoped allowlist); images + Trivy; **full-stack E2E smoke test**; GHCR publish only after E2E passes.
- **E2E smoke (v3):** also covers site/articles/recommendations, instant account via PoW, multi-field delivery, order lookup, after-sales (replace + refund), staff card find/void/replace, `file` messages and `/me` self-service; the frontends were reconciled against the real API (see `docs/api-contract.md`).

### 2026-11-06: v3 storefront
- **Built** (`docs/review-v3.md`, ADR-015…019; API in `docs/api-contract.md`):
  - list storefront with generated covers and the Kesiv brand config;
  - announcement modal, recently viewed, recommendations;
  - instant accounts with self-hosted PoW;
  - order/card lookup by access code;
  - multi-field delivery with redeem URL and usage notes;
  - articles CMS and fixed pages;
  - chat `.txt` file messages;
  - after-sales (replace / refund record / reject / resolve) with auto-confirm;
  - staff card management (fingerprint find, void, replace, batches, expiry);
  - `/me` self-service including account deletion.
- **Reviews and fixes:** a payment audit and a security review of the above were done; all findings are fixed with tests:
  - replacement card ids are validated, and the staff actor is recorded on replacement;
  - a duplicate notify after a refund is acknowledged quietly;
  - delivered-order windows are backfilled in migration 0002;
  - refund is admin-only inside the service, and opening a ticket bumps the order version (auto-confirm race);
  - instant accounts have no email, and the per-network cap is 200/day with a PoW ramp;
  - lookup lockout is per `order_no:ip` plus a global per-order cap, and a correct code is never blocked;
  - markdown images are same-origin only;
  - card find is audited and limited to 30/h;
  - the 3rd replacement needs an admin;
  - account deletion redacts chat messages, deletes uploads and clears ticket text;
  - `APP_LOOKUP_KEY` is required in prod;
  - `test_message_rate_limit` is now deterministic (clock pinned).

### 2026-11-06: local dev environment
- `scripts/dev.sh` runs the whole stack in one command: Python 3.12 venv via uv, dev PG + Redis containers, migrate + seed, then API, delivery and mockpay with reload, and both Vite apps.
- Everything binds to 127.0.0.1. You open it through an SSH tunnel, see `docs/dev-setup.md`.
- `scripts/dev.sh test` is a shortcut for the backend checks plus the frontend lint, typecheck and tests.
- `e2e/smoke.py --dev` runs the full E2E against the dev stack; it passes.

### 2026-11-06: production deployment support
- Same images and base compose as the demo; `deploy/docker-compose.prod.yml` + `deploy/Caddyfile.prod` add the production edge: Cloudflare (proxied) -> `:8088` HTTPS with an Origin CA cert, Authenticated Origin Pulls required, real visitor IP from `CF-Connecting-IP` (Cloudflare ranges only), `/admin` and `/api/v1/admin/*` limited to `ADMIN_ALLOW_IPS`, no mockpay. ADR-020.
- `deploy/bootstrap.sh` has `MODE=prod` (real ZPay from an env file, `APP_ENV=prod`, secure cookies, admin-only credentials file).
- Seed production mode (`APP_SEED_DEMO=false`): tenant, one admin (`APP_SEED_ADMIN_EMAIL`), pages, tutorials, site settings and a disabled announcement; no demo users, products, cards or orders. Idempotent, never overwrites admin edits.
- `scripts/prod.sh`: sync, bootstrap, deploy, status, logs, backup (+ nightly cron, 14 generations) and read-only smoke checks. See `docs/production.md`.

### 2026-11-06: commerce v4 backend (branch `feat/v4-be`)
- Spec: [review-v4.md](review-v4.md); ADR-021; API in `api-contract.md` ("Commerce v4 additions").
- **Done (server side):**
  - Buyer-paid channel surcharge (ceil, default 30 permille for Alipay), order `subtotal/fee/fee_rate`, ledger `surcharge_revenue` and refund reversal.
  - Cart quote, combined checkout, checkout pay/cancel, checkout notify path sharing one settlement helper, checkout expiry, payables cap.
  - Buyer/staff cancel with gateway re-query first; staff close needs a reason.
  - Product "order information" popup config, `orders.buyer_input` (never audited), PATCH input, buyer chat message after payment.
  - Support welcome/quick questions in site settings, `GET /site/payment-channels`.
  - Staff `product_card` messages built by the server.
  - Markdown product descriptions (`description_html`), idempotent seed backfill, `POST /admin/content/images`.
  - Global per-IP rate limit middleware (default OFF, admin switch, fail open).
  - Tests: `backend/tests/test_commerce_v4.py` (27 tests). Legacy tests run with a 0 permille surcharge configured in the `env` fixture.
- **Follow-ups:** file a GitHub issue to encrypt `orders.buyer_input` at rest; payment-auditor and security-reviewer passes on `payments/settle.py`, `orders/cancel.py`, `orders/checkout.py`; reconcile ledger vs gateway for surcharge.

### 2026-11-06: v5 commerce backend (branch `feat/v5-be-com`)
- Promo codes (percent/fixed, limits under row lock, reserved/used/released lifecycle, largest-remainder allocation), reseller storefronts (host/header resolution in `app/core/storefront.py`, markup pricing, sign-up order, payouts, admin), ledger lines for discount/markup/sign-up/payout. Tests: `backend/tests/test_commerce_v5.py`; E2E `v5_commerce`.
- **Follow-ups:** payment-auditor pass; reseller custom domains slot into `storefront.candidate_slug`; refund after payout makes `reseller_payable` negative (clawback, no automation).

## Next

### v3 follow-up
1. [ ] Owner click-through of the v3 storefront on the demo; collect UX feedback.
2. [ ] Refund API integration (ZPay) so a refund moves money instead of only recording it; reconciliation flags refunded orders without a gateway refund.
3. [ ] Email verification before an address can be trusted (see Known issues).

### MVP-0 wrap-up
1. [ ] Owner click-through on the demo host, then collect UX feedback.

### M1: production MVP (6–8 weeks; exit gate = independent security review + restore drill)
1. [ ] Infra: measure SoftBank↔IIJ and CN→edge latency; WireGuard mesh; nftables; SSH over WireGuard only.
2. [ ] Real ZPay merchant account (¥88). Configure `APP_ZPAY_*` via SOPS. Reconciliation job. External alerting on `manual_review`, `payment.*` critical audits and breaker trips.
3. [ ] Staff TOTP + step-up for card import; admin served on an admin-only path (VPN or Cloudflare Access); email verification; canned replies.
4. [ ] OG share endpoint `/s/{id}` + `sitemap.xml`.
5. [ ] Private R2 bucket for uploads (proxied via the edge); separate media origin; server-side voice transcoding.
6. [ ] pgBackRest → US + R2; restic; automated monthly restore drill; key-compromise runbook.
7. [ ] Security review follow-ups:
   - L2: SSE re-auth on heartbeat.
   - L4: per-domain ask buckets + negative-cache invalidation.
   - L7: SCRAM verifiers in the migration.
   - Frontend Low items: touch targets, aria-labels, image dimensions, plural forms, `console.warn` lint.
8. [ ] Stock reservation at order creation, so the last card can't be sold twice into `manual_review`.
9. [ ] Independent pentest. **No real money before it passes.**

### Scheduled
- [ ] **2026-11-12:** raise `minimumReleaseAge` in `frontend/pnpm-workspace.yaml` to 10080 (7 days) and drop its `nosemgrep`.
- [ ] When an upstream `caddy:2.11-alpine` ships go-jose ≥ v4.1.6: remove the xcaddy stage in `deploy/web.Dockerfile`.

## Open questions for the owner
- [ ] **Legal name for `LICENSE`** (#40). It currently says `Kesiv`, taken from the repo's own
      branding, and `printCopyrightNotice({ owner: 'Kesiv' })` passes the same. Needs the exact
      legal name of the owning person or company.
- [ ] **Governing law and selling entity** (#40), blocking the ToS draft. The host is in Japan
      (IIJ), buyers are international, the owner's jurisdiction is unstated. This determines the
      Mainland China restriction, statutory withdrawal rights for digital goods, and refund terms.
      Anthropic's ToS text will **not** be copied; original text in a comparable structure instead.
      Needs a qualified lawyer before it governs real transactions.
- [x] ~~Approval to deploy `main` to production~~ — done; production is deployed per release with a
      pre-deploy backup and rollback images (see "Current state").
- [ ] Legal review of payments for mainland users (aggregator + later USDT).
- [ ] Which hostname of your domain to use for production. For HTTPS on the demo, point `demo.<domain>` at the demo host's IP.
- [ ] Chatwoot re-evaluation at M2: only if there are ≥3 agents, on a ≥4 GB server.

## Known issues
- Local dev Python is 3.10: Pillow there is capped at 12.3 (advisories fixed only in 13.0.0, which needs ≥3.11). CI and the images use 3.12 + Pillow 13.
- `filelock` (dev-tool transitive) advisories are advisory-only in CI.
- **Email ownership is unverified** until email verification lands (M1): anyone can set an unverified address on their account via `/me/credentials` or register. Conflicts use a generic message so addresses are not enumerable. Do not use email for account recovery or notifications of secrets yet.
- **Anonymization retains audit-log IPs** (and the anonymised order/ledger rows): account deletion redacts chat messages, deletes uploads and clears ticket text, but security audit rows keep IP addresses. The seeded privacy page says so.
- **No refund API:** refunds are recorded (order `refunded` + reversal ledger entries + audit) but the money is returned manually in the Alipay console.
- **runc 1.1 inside c4 (EOL):** the c4 LXD container runs Docker with runc 1.1 because host LXD 4.0 has an AppArmor limitation with newer runc. Upgrade host LXD to 5.21 and return to the default runc (`docs/production.md`).
- **No independent pentest/security review yet** (M1 exit gate, invariant 11). Production is live with real ZPay only under the owner's acceptance of that risk; keep order volume low until the review is done.

### v4 integration notes (2026-11-06)
- Migration 0003 backfill runs with `NO FORCE ROW LEVEL SECURITY` lifted on `orders` for the UPDATE only. With FORCE RLS even the table owner saw no rows, so the backfill silently updated nothing, and the later `SET NOT NULL` failed on existing data. The 0002 backfill had the same blind spot; it was harmless because production launched on v3.
- `scripts/dev.sh up` re-asserts the dev role passwords (`app_owner`, `app_api`, `app_delivery`) before migrating. Backend tests share the dev PG cluster and reset these cluster-wide roles to their test passwords.
- `e2e/smoke.py` gained a v4 section:
  - channel fee
  - cart quote
  - checkout paid in one payment
  - `pay_via_checkout`
  - buyer cancel and staff close
  - staff product card
  - global limiter, checking the exact 429 body

### v5 backend core (2026-11-06)
- Done: product archive/hard delete/restore/bulk; DB-backed categories with admin CRUD; settings sections + encrypted secrets (`APP_SETTINGS_KEY`, added to dev.sh and bootstrap.sh; prod.sh does not generate api.env); feature toggles; Turnstile; SMTP mailer, email codes, password reset, configurable email templates, order emails; Google sign-in; audit filters/CSV/retention; manual delivery ETA + overdue filter; tenant `defaults` applied to pending-order cap and timeout.
- Migration 0005 (`0005_v5_core_grants`): API may update its outbox rows; bounded `purge_audit_log()` function. Rebase note: the commerce agent may add its own 0005; renumber on merge.
- Next: frontend settings UI/consumers; production DNS/Turnstile hostnames; independent review (M1 gate) still open.
