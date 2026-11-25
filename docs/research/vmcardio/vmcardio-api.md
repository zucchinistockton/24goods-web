# vmcardio (VM card) API: backup card issuer

Owner added vmcardio on 2026-11-25 as a backup source of virtual cards. Credentials are kept outside git:
`/root/.secrets/vmcardio.env` on the dev box. Vendor pages, copied from the public `llms.txt` index of
https://vmcardio.com/apidocuments/ on 2026-11-25, are in [`vendor/`](vendor/) (`llms.txt` = table of contents).

Legend: **[V]** verified by a call on 2026-11-25; **[D]** from the vendor docs; **[I]** inferred.

## Environments

| | Console | API base |
|---|---|---|
| Sandbox | https://sandbox.vmcardio.com/ | https://sandbox-api.vmcardio.com/ |
| Production | https://vmcardio.com/ | https://vmapi.vmcardio.com/ |

- Sandbox and production are **separate accounts with separate API keys**. The production `app_id` answers
  `400006 Invalid AppId` on the sandbox **[V]**. A sandbox account needs its own registration and customer-service
  setup **[D]** (`vendor/6664455m0.md`).
- The owner's production API account already works (token, balance, product list) **[V]**, so production API
  permission is on.

## Version: V2 only (no IP whitelist, no RSA)

- Accounts opened on or after 2026-10-30 can use **V2 only** **[D]** (`vendor/6664455m0.md`, `vendor/8987582m0.md`).
- V2: path prefix `/v2/`, plain JSON request and response, **no IP whitelist, no merchant key pair** **[D]**.
- V1 (IP whitelist + RSA-4096 encrypted `{"content": ...}`) is only for accounts that configured a merchant
  public key before 2026-10-30. Not used here.
- Called from the dev box (107.173.10.79) and from production c4 (141.98.198.150, IIJ Japan): both work, ~0.2-0.45 s
  **[V]**.

## Authentication

1. `GET /v2/getAccessToken?app_id=...&app_secret=...` -> `data.token` (36 characters) and `data.expired_time` **[V]**.
2. Every call: header `Authorization: <token>` (no `Bearer` prefix) **[D]**.

## Endpoints (V2, POST JSON unless stated)

| Path | Use |
|---|---|
| `/v2/getAccountBalance` | `balance` = main account (USD, funds stored-value "save" cards); `wallet_balance` = "share" (limit) cards **[V]** |
| `/v2/getProductCode` | card products: `bin`, `product_code`, `type` (save/share), `network`, `issuing_area`, `remaining_open_card_num` **[V]** |
| `/v2/createCard` | `product_code`, `first_name`, `last_name`, `amount` (first load of a save card), optional `label`, `card_address`, `email`, `mobile` **[D]** |
| `/v2/cardDetail` | by `vm_card_id`: returns `card_number`, `cvv`, `expire`, `available_amount`, `status` **[D]** |
| `/v2/rechargeCard` | load a save card from `balance` (synchronous, `"true"` = done) **[D]** |
| `/v2/refundCard` | card balance back to `balance` (synchronous) **[D]** |
| `/v2/freezeCard`, `/v2/deleteCard` | freeze / unfreeze, delete **[D]** |
| `/v2/cardTransaction`, `/v2/getCardList`, `/v2/getCardFlow` | transactions, card list, money movements (`flow_id` unique) **[D]** |

- Products seen on the owner's account: Hong Kong SAR MasterCard "save" (stored-value) BINs 558325, 54502000,
  52737560, 54492360, 54493747 and more, 5-10 cards left each **[V]**. Share (limit) cards are not offered **[D]**.
- **The API manages only cards created through the API.** Cards opened in the web console cannot be read, loaded or
  refunded through it **[D]**. Money sitting on console-made cards must be moved back by hand in the console.

## Idempotency

`X-Client-Request-Id` (max 64 characters, `[A-Za-z0-9-_.:]`), unique per `app_id`, kept forever. A repeat returns
`Duplicate Client Request Id` and **not** the first result. After a duplicate or a lost answer, look the result up:
card opening via `/v2/getCardList` or `/v2/cardDetail`, loads / refunds via `/v2/getCardFlow` **[D]**
(`vendor/9338753m0.md` §4). This makes a safe retry possible (unlike card.zzshu.pro).

## Webhook

- Set in console: Personal center -> Developer -> bottom of the page: webhook URL **[D]**.
- Pushes card transactions (`auth_id`, `vm_card_id`, amounts, `status`, `type`, `merchant_name`, `description`).
  Reply `{"code":0,"msg":"ok"}`, otherwise it is sent up to three more times **[D]** (`vendor/6664458m0.md`).
- Signature: header `X-VM-SIGN` = Base64 RSA-SHA256 over the raw body, verified with the **VM public key shown on
  the same console page**. Sandbox and production have different keys **[D]** (`vendor/9364102m0.md`).
- Planned URL: `https://shop.kesiv.com/api/v1/webhooks/vmcardio` (does not exist yet; set it only after it ships).

## Fees (vendor FAQ, `vendor/9338753m0.md` §1)

- Deposit fee when money enters the main account; loading a card from the main account is free.
- Card opening fee per product (console); no charge when opening fails.
- Authorization **or failed transaction**: 0.20 USD (VC102/VC110/VC113) or 0.30 USD (VC105) each.
- Merchant refund to the card: 10 % fee. Chargeback: 35 USD. No FX or cross-border fee. Card balance back to the
  account: free. Withdrawal from the account to an external wallet: 4 % + 3 USD.
- Limits: 10,000 USD per transaction, 100,000 USD per day.

## Fit for 24goods [I]

- Natural first use: a second issuer for the **dedicated card of a card.zzshu.pro order** (ADR-054), today opened on
  ZovoCard. `cardDetail` returns exactly what zzshu needs (number, CVV, expiry). Flow per order: `createCard`
  (amount = cost x (1 + margin)) -> `cardDetail` -> zzshu order -> `refundCard` leftover -> `deleteCard`.
- Second possible use: another source for VCC products (delivery mode `upstream_vcc`).
- Whether ChatGPT accepts these Hong Kong MasterCard BINs is unknown until a real attempt; each failed attempt
  costs 0.20-0.30 USD.
