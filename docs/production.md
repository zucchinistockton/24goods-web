# Production runbook (shop.kesiv.com)

## Topology

```
visitor --TLS--> Cloudflare (orange cloud, SSL Full strict)
   --Origin Rule: port 8088, HTTPS + Authenticated Origin Pulls-->
141.98.198.150:8088 --LXD nat--> c4 (10.0.43.40:8088) --> Caddy (web) --> api / SPAs
```

- c4: Ubuntu 22.04, Docker 29 + compose v2, deploy dir `/opt/kesiv/deploy` (only `deploy/` files, no source).
- Images: `ghcr.io/zucchinistockton/goods24-api:main` and `goods24-web:main` (private, pulled with a transient login).
- **Publishing (since 2026-11-15):** on the new repository `push` and `pull_request` events do not start
  workflows (no check suite is created for any push; only `workflow_dispatch` runs — an account-level
  restriction on the new GitHub account, not a repository setting). After merging to `main`, run
  `gh workflow run CI --ref main`; the `publish` job pushes `:main` and `:<sha>` when all jobs are green.
  For PRs run `gh workflow run CI --ref <branch>` (a manual run scans the whole history with gitleaks).
- Same compose as dev/demo plus `docker-compose.prod.yml` (port 8088, certs, `Caddyfile.prod`, memory limits for 4 GB: api 384m, postgres 512m). mockpay is behind `--profile demo` and never starts.
- Only `web` publishes a port. Source IPs at c4 are Cloudflare IPs; the real visitor comes from `CF-Connecting-IP` (trusted only from Cloudflare ranges, list dated in `Caddyfile.prod`).
- Admin paths (`/admin`, `/admin/*`, `/api/v1/admin/*`) return 403 unless the visitor IP is in `ADMIN_ALLOW_IPS` (default `141.98.198.150 38.175.222.75`, space-separated IPs/CIDRs, in `deploy/.env`).
- SSE: the API pings every 15 s, below Cloudflare's 100 s idle limit.
- Secrets on this box (`/root/.secrets`): `c4_ssh_pw`, `ghcr_token`, `zpay.env` (`APP_ZPAY_PID`, `APP_ZPAY_KEY`, `APP_ZPAY_GATEWAY=https://zpayz.cn`), `prod-certs/{origin.pem,origin.key,cf-origin-pull-ca.pem}`. After bootstrap: `prod-admin.txt`.

## First deploy

```bash
scripts/prod.sh sync        # deploy/ files + TLS certs -> c4:/opt/kesiv/deploy
scripts/prod.sh bootstrap   # once: MODE=prod bootstrap.sh on c4, admin login -> /root/.secrets/prod-admin.txt
scripts/prod.sh deploy      # pull images, up -d --wait (migrate -> seed -> api, delivery, web)
scripts/prod.sh smoke       # read-only checks against the public site and the origin
scripts/prod.sh backup && scripts/prod.sh install-backup-cron
```

`bootstrap` refuses to run if `/opt/kesiv/deploy/.env` exists: regenerating would orphan the vault keys and the database.
Then sign in at `https://shop.kesiv.com/admin/` from an allowlisted IP, change the admin password, review the pages and enable the announcement if wanted.

## Update

```bash
scripts/prod.sh deploy      # idempotent; sync first if deploy/ files (compose, Caddyfile.prod) changed
scripts/prod.sh smoke
```

`deploy` runs the migration as a one-off before recreating any container (a failed migration leaves the running release untouched), logs in to GHCR for the pull only (token on stdin; `docker logout` afterwards) and appends the running image ids to `/opt/kesiv/image-history.log`. Migrations run as the `migrate` one-shot before the API restarts. Seed re-runs are idempotent and do not overwrite admin edits.

Changing the admin allowlist: edit `ADMIN_ALLOW_IPS` in `/opt/kesiv/deploy/.env`, then `cd /opt/kesiv/deploy && docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env up -d web`.

## Rollback

Migrations are forward-only; roll back only to an image whose schema is compatible (restore a backup otherwise).

1. Find the previous digest: `docker image ls --digests ghcr.io/zucchinistockton/goods24-api` on c4, or the GitHub Actions run of the previous good commit.
2. Pin it in `/opt/kesiv/deploy/.env` (login to GHCR first if the image is not local):
   `API_IMAGE=ghcr.io/zucchinistockton/goods24-api@sha256:<digest>` and `WEB_IMAGE=ghcr.io/zucchinistockton/goods24-web@sha256:<digest>`.
3. `docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env up -d --wait` (do NOT use `scripts/prod.sh deploy` while pinned: it pulls `api web`, which is fine for digests but remember to unpin).
4. Unpin (set back to `:main`) after the fix is merged and deployed.

## Backups and restore

**Review note (2026-11-08; implementation pending):** the current script below is a starting
point, not a complete verified disaster-recovery system. It does not encrypt the config/key
archive, include `bepusdt_data`, or publish a verified whole-set manifest. Permissions and SSH
transport do not replace backup encryption/off-host recovery. Cron installation and restore
success must be verified on the actual host. See [observed gaps](research/payment-qr-ops-2026-11-08.md#newly-observed-backup-and-logging-gaps)
and the [proposed recovery requirements](review-v6-payments-ops.md#7-complete-encrypted-and-recoverable-backups).
Restore drills must disable outbound notifications/callbacks and prevent duplicate delivery
until provider payments and the restored outbox are reconciled.

- `scripts/prod.sh backup` runs `/opt/kesiv/backup.sh` on c4 and copies the files to `/root/kesiv-backups/` (14 generations kept on both sides): `goods24-<ts>.dump` (`pg_dump -Fc`), `uploads-<ts>.tgz`, `config-<ts>.tgz` (`.env`, `env/`, `secrets/`: contains the vault private keys, treat as top secret; without them card stock cannot be revealed).
- `scripts/prod.sh install-backup-cron` installs `/etc/cron.d/kesiv-backup` (03:30 UTC) on c4. Backups on c4 are on the same disk: pull copies regularly with `scripts/prod.sh backup`.
- Recover on an **isolated scratch host** with the selected complete backup set, compatible
  image/schema versions and separately recovered keys. Keep API/delivery stopped and block
  external payment callbacks, SMTP/TG and outbound provider calls while restoring. If an
  operator needs controlled read-only provider reconciliation later, grant only that path.
- Recreate database ownership/RLS/role grants using the reviewed init/migration procedure;
  restore the PG archive with `pg_restore`, matching uploads, BEpusdt state and configuration
  into explicitly selected recovery volumes. Validate checksums, media references and secret
  recovery; do not point a scratch restore at the production Compose project or volumes.
- Before enabling any fulfilment, reconcile restored payments/outbox/entitlements against
  the provider's post-backup history using an authorized controlled recovery task. Existing
  background loops do not establish this recovery capability; implementing and testing it
  is part of the open release gate. The restore task itself must not deliver or notify.
- Start only the explicitly approved services after those checks and record the release,
  recovery point, RLS/grant/synthetic-vault/private-media checks and measured recovery time.
  Then re-enable callbacks/delivery/notifications in a controlled order and observe duplicate
  or late-payment handling. A blanket full-stack restart is not a safe restore completion step.
  The measured scratch-host drill remains an M1 exit gate.

## Rotating the ZPay KEY

1. Change the key in the ZPay merchant console (outstanding payment notifies signed with the old key will then fail verification; do it when no payments are pending).
2. Update `/root/.secrets/zpay.env` and edit `APP_ZPAY_KEY` in `/opt/kesiv/deploy/env/api.env` on c4 (`env/api.env` is only readable by root).
3. `cd /opt/kesiv/deploy && docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env up -d api`.
4. Make a small test purchase.

## USDT (self-hosted BEpusdt)

**Known payment reliability defect, #26:** the configured upstream native notifier treats
any HTTP 200 as acknowledgement, while the current API can return `200 fail` on temporary
query failure. Do not treat the existing body-only retry test as upstream retry proof. Repair
and verify the [ACK contract](research/security-payment-audit-2026-11-08.md#f0--bepusdt-http-200-failure-replies-permanently-acknowledge-failed-callbacks)
and durable reconciliation before enabling real-money USDT.

BEpusdt v1.24.3 (`v03413/bepusdt:0716485@sha256:0a7d85…`, pinned by digest; compose profile `usdt`) receives USDT on Arbitrum One, X Layer and
TRON. It is INTERNAL: docker network `usdt` only (shared with `api`), no published port, no Caddy route.
Wallet addresses and the API token live in its console/volume, never in git.

1. Start it: `$DC --profile usdt up -d bepusdt` (`$DC` = `docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env`).
   The first start creates the SQLite DB in the `bepusdt_data` volume. Pin the image digest after the first pull.
2. Open the console over an SSH tunnel (nothing is exposed): find the container IP
   `docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' goods24-bepusdt-1`, then
   `ssh -L 8080:<container-ip>:8080 <host>` and browse http://127.0.0.1:8080. Change the default admin
   credentials immediately (first login, see the upstream docs shown in the console).
3. In the console add the receiving wallets (public addresses, safe to keep in docs):
   - Arbitrum One and X Layer (both EVM): `0xe2dc319ed904b2f798f262e3274b0f0c60e0653c`
   - TRON (TRC20): `TQkeoASc6X5itH4MyPZPPCfLM4M62cvMu5`
   Enable the networks `usdt.arbitrum`, `usdt.xlayer`, `usdt.trc20` and check the CNY/USDT rate source.
4. System settings -> API: copy the **API token** and put it in `env/api.env` as
   `APP_BEPUSDT_API_TOKEN=...` (with `APP_BEPUSDT_URL=http://bepusdt:8080`); `$DC up -d api`.
   Only `api` gets the token. The notify URL sent to BEpusdt is `http://api:8000/api/v1/pay/notify/bepusdt`
   (`APP_BEPUSDT_NOTIFY_URL` to override).
5. Admin -> Settings -> Payments: enable the USDT channels you want and set the surcharge (default 0).
6. (2026-11-21) Crypto wallets page: also put the BEpusdt admin login in `env/api.env` as
   `APP_BEPUSDT_ADMIN_USER=…`, `APP_BEPUSDT_ADMIN_PASSWORD=…`, `APP_BEPUSDT_ADMIN_ENTRY=…` (the secret admin
   path, `admin_secure` in BEpusdt); then Admin -> Settings -> Payments -> Crypto wallets lists, adds, switches
   and removes receiving addresses for every network BEpusdt supports (USDT / USDC on X Layer, Polygon,
   Arbitrum, Base, BEP20, Solana, TON, Aptos, Plasma, ERC20, TRC20). A network shows to buyers only when its
   switch is on and it has an enabled wallet.
   A channel stays invisible to buyers until enabled.
6. Test with a small real transfer on each network before announcing it (M1 exit gate: independent review
   before real money, AGENTS.md §1.11).

Operations: BEpusdt retries a failed paid notify 10 times with back-off (2, 4, 8 ... min). An order that
reaches `manual_review` (amount or re-query mismatch, late payment) is resolved by staff; the on-chain
transaction hash is in the payment record (`block_transaction_id`). Back up the `bepusdt_data` volume with
the other volumes (it only holds addresses, settings and trade history, no private keys).

## Xiaodou EPay relay (since 2026-11-09)

c4's route into mainland China loses packets in both directions (measured against Xiaodou and a
Tencent Cloud Guangzhou VPS at the same moments; the same targets are fine from other networks).
The ZPay client therefore has a fallback path (`backend/app/payments/egress.py`):

- **Sticky route, per gateway host:** the path that worked last goes first; the other is tried
  only when its connection cannot be opened (never after a request may have been sent, so no
  duplicate create/query). Direct (from c4) is the default; a failure moves the preference to the
  relay and only a relay failure moves it back. The last working IP is remembered; when every path
  fails, the domain is resolved again and direct retried once (moved load balancer).
- **Config (c4 `deploy/env/api.env`, not in git):** `APP_ZPAY_RELAYS=pay.xiaodouap.cn=107.173.10.79:20443`.
  Empty = direct only. `APP_ZPAY_RELAY_ONLY=true` (owner 2026-11-23; c4 <-> Xiaodou is blocked in both directions):
  a host with a relay uses only the relay, with no DNS lookup on c4 and the whole connect budget (create 2.25 s per
  phase, query 2.0 s); the sticky/direct logic above then does not apply.
- **Relay DNS:** the relay resolves `pay.xiaodouap.cn` on every connection. From the dev box that lookup takes about
  1 s and sometimes 14 s (Tencent DNS from the US, TTL 60 s, no local cache), which eats the handshake budget. Pin it
  in the dev box's `/etc/hosts` (refreshed by a timer) or run a caching resolver there.
- **Relay (dev box):** `kesiv-xiaodou-relay.service` (socat :20443, accepts c4's IPv4 only via
  `range=` + ufw) dials `pay.xiaodouap.cn:443` by name. TLS is end to end between the app and
  Xiaodou; the relay forwards ciphertext. The dev box IP is on Xiaodou's merchant whitelist.
- Buyers never use the relay: their browsers open Xiaodou's pay page directly.
- Check: Admin -> Settings -> Payment -> Test now (expects `-1 订单不存在` for the random probe).
- Removal: clear `APP_ZPAY_RELAYS`, redeploy the api, stop the relay service.

## zzshu standby provider (ADR-054)

card.zzshu.pro takes ChatGPT direct orders when ZovoCard fails before any charge, or first when staff / the cool-down
put it first. Setup (production needs the owner's go-ahead; nothing here is done by the deploy itself):

1. Procurement env file (`deploy/env/procurement.env`, never in git): `ZZSHU_APP_SECRET=<API key from card.zzshu.pro>`
   (the key's points = remaining successful activations; 1 per success). Optional: `ZZSHU_BASE_URL` (default
   `https://card.zzshu.pro/api/v1/third-party`), `ZZSHU_FUNDING_SUPPLIER` (default `zovocard`: the supplier whose wallet
   opens the dedicated cards). Restart `procurement`. The procurement container reaches card.zzshu.pro and zovocard.com
   only (no other egress).
2. Console -> Procurement -> Suppliers: add a supplier, adapter `zzshu`, code `zzshu`, env `production` (a
   production supplier also asks for your password and stays disabled until `APP_PROCUREMENT_ALLOW_PRODUCTION` is set),
   then "Test connection" (reads `GET /user` points and the funding wallet).
3. Console -> Procurement -> Cards -> Card funding: `auto_recharge` ON (zzshu opens a card per order, so it needs the
   master switch and the daily cap), set `zzshu card margin` (default 20 %; the owner wants it high).
4. Sources, one per ChatGPT variant that has a zzshu plan (go, plus, pro_5x, pro_20x in PH / US / EG / CL; JP and the
   other plans cannot have one): product / variant -> supplier `zzshu`, params `product gpt`, the SAME `plan` and
   `payment_country` as the variant, cost estimate in USD (what the plan costs us; the card is loaded with it x 1.2),
   priority AFTER the ZovoCard source (for example ZovoCard 10, zzshu 20), then enable.
5. Make zzshu primary by hand: give its source a smaller priority than ZovoCard's (or disable the ZovoCard source);
   back again the same way. Automatic: 3 definite pre-charge ZovoCard failures within 60 minutes put ZovoCard's direct
   sources behind zzshu for 30 minutes (Telegram alert "ZovoCard direct orders failed before any charge repeatedly").
6. Points and limit alerts: the supplier row shows "Points: N (checked <time>)" (refreshed with the balance and on
   "Test connection"). Telegram alerts: points at or below `zzshu points alert` (Card funding, default 3, 0 = off;
   once per 6 h while low, fresh readings only); daily card funding limit 80 % used and "limit reached" (new
   top-ups wait until 00:00 UTC), once per UTC day each, only with `auto_recharge` on.
7. Watch: `procurement.precharge_failover`, `procurement.zzshu_card` (card id, last four, loaded, leftover) in the job
   timeline; `procurement.zzshu_card_unswept` = a card still holds money, look at it by hand; `create_unconfirmed` =
   the zzshu answer was lost, check the zzshu order history before anything else (the order may exist and be paid).

## vmcardio: second issuer of the zzshu dedicated card (ADR-055)

vmcardio opens the dedicated card of a zzshu order only when ZovoCard opened nothing (wallet too low, no product / slot,
vendor refusal before any card exists). Staff can swap the order. It is used for nothing else. Setup (production needs
the owner's go-ahead):

1. Procurement env file (`deploy/env/procurement.env`, never in git): `VMCARDIO_APP_ID=<app id>`,
   `VMCARDIO_APP_SECRET=<app secret>`; optional `VMCARDIO_BASE_URL` (default `https://vmapi.vmcardio.com`). Cardholder
   data used when a card is opened: `VMCARDIO_CARDHOLDER_FIRST_NAME`, `VMCARDIO_CARDHOLDER_LAST_NAME` (neutral default
   if unset) and optionally the billing address, all or none: `VMCARDIO_CARD_ADDRESS_LINE1`, `VMCARDIO_CARD_CITY`,
   `VMCARDIO_CARD_STATE`, `VMCARDIO_CARD_POSTCODE`, `VMCARDIO_CARD_COUNTRY` (default `US`; letters, digits, spaces and
   hyphens only). Set `ZZSHU_FUNDING_SUPPLIER=zovocard,vmcardio` (the console setting below overrides the order).
   Restart `procurement`. The container needs egress to `vmapi.vmcardio.com`.
2. Console -> Procurement -> Suppliers: add a supplier, adapter `vmcardio`, code `vmcardio`, env `production` (asks for
   your password, stays disabled until `APP_PROCUREMENT_ALLOW_PRODUCTION` is set; production writes also need
   `APP_PROCUREMENT_ALLOW_PRODUCTION_WRITES`), then "Test connection" (token + balance + products). Do NOT add sources
   to it. Its balance is refreshed like ZovoCard's and the "wallet below" Telegram alert covers it.
3. Console -> Procurement -> Cards -> Card funding (2FA): `zzshu card issuers (in order)` = `zovocard vmcardio` (swap them
   to make vmcardio first); `vmcardio card ranges` (default 537872 537882 532604 555659, best first: the product is
   looked up by BIN, a BIN not listed is never used); `vmcardio slots to keep` (default 1: a product with that many
   open-card slots left or fewer is skipped). The card is loaded with plan cost x (1 + `zzshu card margin`) and counts
   against the daily cap and the per-top-up maximum like a ZovoCard card.
4. Webhook: vmcardio console -> Personal center -> Developer -> webhook URL
   `https://shop.kesiv.com/api/v1/webhooks/vmcardio`. Public key: copy the VM public key shown on that page (PEM with the
   `BEGIN PUBLIC KEY` / `END PUBLIC KEY` lines) into the API env as `APP_VMCARDIO_WEBHOOK_PUBLIC_KEY` (in an env file
   write the newlines as the two characters backslash n), restart `api`. Until then events are accepted and stored as
   `verified: false`; with the key a bad signature gets 401. Either way the webhook only records the transaction
   (table `supplier_events`, kind `vmcardio_card_transaction`) and changes no order or money.
5. Watch: the job timeline and audit `procurement.zzshu_card` name the issuer (`vmcardio`), the card id (`vm:<id>`), last
   four, loaded and leftover; `procurement.zzshu_card_unswept` = the card could not be deleted, look at it in the vmcardio
   console (cards are labelled `zz-<job uuid hex>`); a job in `needs_manual` after an unresolved create: find the card
   with that label before anything else.

## SEO and Google Search Console (review-v5.2 §6, ADR-024)

- Public pages (`/`, `/p/*`, `/a/*`, `/help`) are rendered by the API into the SPA shell for crawlers;
  `/sitemap.xml` and `/robots.txt` are dynamic. Caddy needs no extra config beyond the shipped
  `Caddyfile.prod` (it routes those paths to `api:8000` and serves the internal shell as `Host: web`
  on the container-internal `:80`). The API fetches `http://web/index.html` (override: `APP_WEB_INDEX_URL`).
- Production: Settings -> SEO -> "Allow search engines to index this site" **on** (the code default).
  Demo/staging: switch it **off** (everything becomes `noindex` and `robots.txt` = `Disallow: /`).
  White-label reseller stores stay `noindex` unless "Allow indexing of white-label reseller stores" is on.
- After the first prod deploy: add the property in Google Search Console (DNS TXT verification on the
  apex), **submit `https://<domain>/sitemap.xml`**, request indexing of `/`, and check the Pages report
  for "Excluded by noindex" on anything that should rank. Bing Webmaster Tools can import the property.
- Cloudflare: do not cache HTML aggressively (the API sets `s-maxage=300` and `max-age=0`); after a deploy
  purge `/` and `/p/*` if a "cache everything" rule exists, because HTML embeds hashed asset URLs.
- Optional share image: Settings -> SEO -> social share image (e.g. an `/og.png` shipped in the web image).

## Refreshing Cloudflare IP ranges

`scripts/check-cf-ips.sh` reports drift (exit 1); the weekly `Edge drift` workflow runs it and fails when Cloudflare publishes new ranges.

`curl https://www.cloudflare.com/ips-v4; curl https://www.cloudflare.com/ips-v6`, update `trusted_proxies static ...` and the date comment in `deploy/Caddyfile.prod`, validate (`caddy validate` in the web image), sync, `up -d web`.

## Known issue: runc 1.1 inside c4

The c4 container runs Docker with the default runtime set to runc 1.1 (EOL) because host LXD 4.0 has an AppArmor limitation that breaks newer runc. No compose change is needed. Fix: upgrade the host LXD to 5.21, then return Docker's default runtime to the bundled runc and recreate the containers. Until then, treat container escapes as a risk: containers already run with `cap_drop: ALL`, read-only roots and `no-new-privileges`.
